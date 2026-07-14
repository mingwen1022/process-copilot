"""合成事件日志生成器（分析侧闭环 Phase 1）。

在 ProcessDefinition 上做 path sampler：按真实 submit_paths 条件路由，人为选择点
（同意/退回）按概率抽样，处理人经 resolve_role/org_seed 派真人。注入 6 类带
ground-truth 标签的病灶，供后续检出率评测使用。

设计上对齐真实 EOA 事件日志的现实约束（见 doc/产品功能全景.md）：
- 只有一个 enter/leave 停留区间，不拆等待/处理；
- outcome/是否人工介入统一由 action + action_category 承载；
- 支持"多人派单 + 抢办"（HandlerMode 的 PREEMPT 系列）；
- 允许实例不办结（进行中/终止），不強求每条实例都走到 END。

复用已有能力，不重复造轮子：
- app.runtime.condition_eval.evaluate_condition 做路由条件判定；
- app.org_knowledge.resolve_role + app.runtime.handler_resolver 做处理人解析。

线下人工介入病灶（MANUAL_INTERVENTION）忠实镜像真实 OA 看板的建模方式：
真实数据里"人工运维介入"不是给某个正常环节打标，而是一条独立的"后台运维操作"
任务行（处理人是 OA 运维账号，带自己的下送路径 path_name），插在某个正常环节
之后。本生成器同样把它建成一条独立的伪环节事件（node_id=_OPS_NODE_ID），
紧跟在被介入的那个真实环节事件之后——"运维前环节"不是一个独立字段，而是
按事件顺序取同一实例里紧邻的前一条事件的 node_name（与真实看板"按运维操作
上一个环节统计"的口径一致），Phase 2 指标层按相邻关系推导即可，无需扩 schema。
"""

from __future__ import annotations

import random
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from app.io_utils import load_process_definition
from app.org_knowledge import ResolvedUser, load_org_seed, resolve_role
from app.runtime.condition_eval import evaluate_condition
from app.runtime.handler_resolver import resolve_node_assignees
from data.schema import (
    ActionCategory,
    CaseRecord,
    CaseStatus,
    DefectSpec,
    DefectType,
    DwellParam,
    EventRecord,
    HandlerMode,
    ProcessDefinition,
    ScenarioConfig,
)

__all__ = ["OPS_NODE_ID", "generate_event_log", "load_process_definition"]

_APPLICANT_POSITION_CODES = {"STAFF", "GROUP_SUPERVISOR", "SUPERVISOR"}
_DEFAULT_DWELL = DwellParam(mu=0.5, sigma=0.7)  # 约 1.6 小时中位数的兜底分布
_DRAFT_DWELL_HOURS_MU = -2.0  # 起草提交本身很快，中位数约几分钟
_DRAFT_DWELL_HOURS_SIGMA = 0.8

# 后台运维操作伪环节：真实看板里处理人是专门的 OA 运维账号，不在正常花名册里。
OPS_NODE_ID = "ops_intervention"
_OPS_PSEUDO_NODE = SimpleNamespace(node_id=OPS_NODE_ID, node_name="后台运维操作")
_OPS_ACCOUNTS = [
    ResolvedUser(
        user_id="ops_yggl_001", name="行政办公室秘书", dept_id=None,
        dept_name="行政运维中心", position_code=None, title="OA运维专员", source="synthetic_ops",
    ),
    ResolvedUser(
        user_id="ops_yggl_002", name="OA流程管理员", dept_id=None,
        dept_name="行政运维中心", position_code=None, title="OA运维专员", source="synthetic_ops",
    ),
]


def generate_event_log(
    scenario: ScenarioConfig,
    *,
    process: ProcessDefinition | None = None,
    org_data: dict[str, Any] | None = None,
) -> list[CaseRecord]:
    process = process or load_process_definition(scenario.process_target_path)
    org_data = org_data or load_org_seed(scenario.org_seed_path)
    rng = random.Random(scenario.random_seed)

    applicant_pool = _build_applicant_pool(org_data)
    if not applicant_pool:
        raise ValueError("org_seed 中没有可作为申请人的员工（STAFF/SUPERVISOR/GROUP_SUPERVISOR）")

    arrival_times = _sample_arrival_times(scenario, rng)
    cases: list[CaseRecord] = []
    for index, arrival in enumerate(arrival_times):
        case_id = f"{scenario.scenario_id}-{index + 1:05d}"
        initiator = rng.choice(applicant_pool)
        case_attributes = _sample_case_attributes(scenario, rng, arrival)
        case_defects = _roll_case_level_defects(scenario, rng)
        case = _generate_case(
            scenario=scenario,
            process=process,
            org_data=org_data,
            rng=rng,
            case_id=case_id,
            initiator=initiator,
            arrival=arrival,
            case_attributes=case_attributes,
            case_defects=case_defects,
        )
        cases.append(case)
    return cases


# ──────────────────────────────────────────────
# 申请人 / 到达 / 案例属性抽样
# ──────────────────────────────────────────────

def _build_applicant_pool(org_data: dict[str, Any]) -> list[ResolvedUser]:
    pool: list[ResolvedUser] = []
    for assignment in org_data.get("position_assignments", []):
        if not assignment.get("is_primary"):
            continue
        if assignment.get("position_code") not in _APPLICANT_POSITION_CODES:
            continue
        resolved = resolve_role(org_data, "起草人", applicant_user_id=assignment["user_id"])
        if resolved:
            pool.append(resolved[0])
    return pool


def _sample_arrival_times(scenario: ScenarioConfig, rng: random.Random) -> list[datetime]:
    start = datetime.fromisoformat(scenario.start_date)
    end = datetime.fromisoformat(scenario.end_date) + timedelta(days=1)
    total_days = max(1, (end - start).days)

    day_weights: list[float] = []
    for offset in range(total_days):
        day = start + timedelta(days=offset)
        weight = scenario.arrival.rate_per_day
        if "month_end" in scenario.arrival.peak_multiplier:
            next_day = day + timedelta(days=1)
            if next_day.month != day.month:
                weight *= scenario.arrival.peak_multiplier["month_end"]
        day_weights.append(max(weight, 0.01))

    days = list(range(total_days))
    arrivals: list[datetime] = []
    for _ in range(scenario.case_count):
        day_offset = rng.choices(days, weights=day_weights, k=1)[0]
        day_start = start + timedelta(days=day_offset)
        seconds_into_workday = rng.uniform(9 * 3600, 18 * 3600)
        arrivals.append(day_start + timedelta(seconds=seconds_into_workday))
    arrivals.sort()
    return arrivals


def _sample_case_attributes(scenario: ScenarioConfig, rng: random.Random, arrival: datetime) -> dict[str, Any]:
    attrs: dict[str, Any] = {"发起时间": arrival.strftime("%Y-%m-%d %H:%M:%S")}
    if scenario.leave_type_weights:
        types = list(scenario.leave_type_weights.keys())
        weights = list(scenario.leave_type_weights.values())
        leave_type = rng.choices(types, weights=weights, k=1)[0]
        low, high = scenario.leave_days_range.get(leave_type, (1, 3))
        leave_days = rng.randint(low, high)
        start_date = arrival + timedelta(days=rng.randint(1, 14))
        end_date = start_date + timedelta(days=max(0, leave_days - 1))
        attrs.update({
            "请假类型": leave_type,
            "请假天数": leave_days,
            "开始日期": start_date.strftime("%Y-%m-%d"),
            "结束日期": end_date.strftime("%Y-%m-%d"),
        })
    for field_name, (low_n, high_n) in scenario.numeric_case_attributes.items():
        attrs[field_name] = round(rng.uniform(low_n, high_n), 2)
    return attrs


# ──────────────────────────────────────────────
# 病灶预抽样（案例级：SLA_BREACH / ILLEGAL_SKIP / MANUAL_INTERVENTION / ZOMBIE_INSTANCE）
# ──────────────────────────────────────────────

_CASE_LEVEL_DEFECT_TYPES = {
    DefectType.SLA_BREACH,
    DefectType.ILLEGAL_SKIP,
    DefectType.MANUAL_INTERVENTION,
    DefectType.ZOMBIE_INSTANCE,
}


def _roll_case_level_defects(scenario: ScenarioConfig, rng: random.Random) -> dict[str, DefectSpec]:
    active: dict[str, DefectSpec] = {}
    for defect in scenario.injected_defects:
        if defect.type not in _CASE_LEVEL_DEFECT_TYPES:
            continue
        if rng.random() < defect.affected_case_ratio:
            active[defect.defect_id] = defect
    return active


def _passage_defects(scenario: ScenarioConfig, defect_type: DefectType, node_id: str) -> list[DefectSpec]:
    return [
        defect
        for defect in scenario.injected_defects
        if defect.type == defect_type and defect.target_node_id == node_id
    ]


# ──────────────────────────────────────────────
# 单个实例的轨迹生成
# ──────────────────────────────────────────────

def _generate_case(
    *,
    scenario: ScenarioConfig,
    process: ProcessDefinition,
    org_data: dict[str, Any],
    rng: random.Random,
    case_id: str,
    initiator: ResolvedUser,
    arrival: datetime,
    case_attributes: dict[str, Any],
    case_defects: dict[str, DefectSpec],
) -> CaseRecord:
    events: list[EventRecord] = []
    labels_seen: set[str] = set()

    zombie_defect = next(
        (d for d in case_defects.values() if d.type == DefectType.ZOMBIE_INSTANCE), None
    )
    # 固定冻结在第一个审批环节（task_order=2，draft 之后必然存在）而非随机挑选，
    # 否则短案例可能在随机选中的 freeze 序号之前就已经正常结束，病灶就"抽中了但没生效"。
    zombie_freeze_order = 2 if zombie_defect else None
    manual_defect = next(
        (d for d in case_defects.values() if d.type == DefectType.MANUAL_INTERVENTION), None
    )
    manual_apply_order = rng.randint(2, 4) if manual_defect else None

    clock = arrival
    current_node_id = "draft"
    task_order = 0
    resubmit_count = 0
    case_status = CaseStatus.COMPLETED
    closed_at: datetime | None = None

    while True:
        task_order += 1
        node = process.get_node_by_id(current_node_id)
        if node is None:
            break

        if zombie_defect and task_order == zombie_freeze_order:
            dwell_hours = _sample_dwell_hours(scenario, current_node_id, rng)
            enter = clock
            events.append(
                EventRecord(
                    event_id=f"{case_id}-ev{task_order}",
                    case_id=case_id,
                    task_order=task_order,
                    node_id=node.node_id,
                    node_name=node.node_name,
                    resource_user_id=initiator.user_id if node.is_draft else None,
                    resource_name=initiator.name if node.is_draft else None,
                    resource_dept_id=initiator.dept_id if node.is_draft else None,
                    resource_dept_name=initiator.dept_name if node.is_draft else None,
                    enter_time=enter,
                    leave_time=None,
                    dwell_seconds=None,
                    action="待处理（未办结）",
                    action_category=ActionCategory.OTHER,
                    target_node_id=None,
                    injected_defect_labels=[zombie_defect.defect_id],
                )
            )
            labels_seen.add(zombie_defect.defect_id)
            case_status = CaseStatus.IN_PROGRESS
            closed_at = None
            break

        if node.is_draft:
            dwell_seconds = max(60.0, rng.lognormvariate(_DRAFT_DWELL_HOURS_MU, _DRAFT_DWELL_HOURS_SIGMA) * 3600)
            enter = clock
            leave = enter + timedelta(seconds=dwell_seconds)
            next_path = node.submit_paths[0]
            events.append(
                EventRecord(
                    event_id=f"{case_id}-ev{task_order}",
                    case_id=case_id,
                    task_order=task_order,
                    node_id=node.node_id,
                    node_name=node.node_name,
                    resource_user_id=initiator.user_id,
                    resource_name=initiator.name,
                    resource_dept_id=initiator.dept_id,
                    resource_dept_name=initiator.dept_name,
                    enter_time=enter,
                    leave_time=leave,
                    dwell_seconds=dwell_seconds,
                    action="提交申请" if resubmit_count == 0 else "退回后重新提交",
                    action_category=ActionCategory.ROUTE_FORWARD,
                    target_node_id=next_path.target_node_id,
                )
            )
            clock = leave
            current_node_id = next_path.target_node_id
            continue

        # 审批环节
        assignees = resolve_node_assignees(
            org_data, process, node, initiator_user_id=initiator.user_id, form_values={}
        )
        winner = assignees[0] if assignees else None

        dwell_hours = _sample_dwell_hours(scenario, node.node_id, rng)
        defect_labels: list[str] = []

        for defect in _passage_defects(scenario, DefectType.SLOW_NODE, node.node_id):
            if rng.random() < defect.affected_case_ratio:
                dwell_hours *= float(defect.params.get("dwell_multiplier", 3.0))
                defect_labels.append(defect.defect_id)

        sla_defect = next(
            (d for d in case_defects.values() if d.type == DefectType.SLA_BREACH and d.target_node_id == node.node_id),
            None,
        )
        if sla_defect is not None:
            sla_days = scenario.assumed_sla_days.get(node.node_id)
            if sla_days:
                dwell_hours = max(dwell_hours, sla_days * 24 * rng.uniform(1.3, 2.2))
            defect_labels.append(sla_defect.defect_id)

        enter = clock
        leave = enter + timedelta(seconds=dwell_hours * 3600)

        base_p_agree = scenario.decision_probabilities.get(node.node_id, 0.9)
        p_agree = base_p_agree
        for defect in _passage_defects(scenario, DefectType.HIGH_RETURN_RATE, node.node_id):
            if rng.random() < defect.affected_case_ratio:
                p_agree = 1.0 - float(defect.params.get("return_probability", 0.3))
                defect_labels.append(defect.defect_id)
        agree = rng.random() < p_agree

        if not agree and resubmit_count >= scenario.max_resubmit_loops:
            events.append(
                _build_event(
                    case_id, task_order, node, winner, enter, leave, dwell_hours * 3600,
                    action="达到最大重提次数-系统终止",
                    category=ActionCategory.TERMINATED_TIMEOUT,
                    target_node_id=None,
                    defect_labels=defect_labels,
                )
            )
            labels_seen.update(defect_labels)
            case_status = CaseStatus.TERMINATED
            closed_at = leave
            break

        context = {**case_attributes, "结论性意见": "同意" if agree else "不同意"}
        chosen_path = _select_path(node, context)
        target_node_id = chosen_path.target_node_id if chosen_path else None
        action_name = chosen_path.path_name if chosen_path else "无匹配路径-系统终止"

        illegal_defect = next(
            (
                d
                for d in case_defects.values()
                if d.type == DefectType.ILLEGAL_SKIP and d.target_node_id == node.node_id
            ),
            None,
        )
        # 只在"审批人同意但本该继续升级"的场景下才算违规跳级；
        # agree=False（本轮是退回起草）时绝不覆盖——否则会把一次合法的拒绝
        # 篡改成提前结束，这既不是"违规跳级"的语义，也会让退回决策本身失真。
        if illegal_defect is not None and agree and target_node_id not in (None, "END"):
            forced_target = illegal_defect.params.get("force_target_node_id", "END")
            if forced_target != target_node_id:
                target_node_id = forced_target
                action_name = process.get_node_name(forced_target)
                defect_labels.append(illegal_defect.defect_id)

        category = _categorize(target_node_id, chosen_path)
        dwell_seconds = (leave - enter).total_seconds()
        group_events = _build_task_group_events(
            case_id, task_order, node, assignees, winner, enter, leave, dwell_seconds,
            action_name, category, target_node_id, defect_labels,
        )
        events.extend(group_events)
        labels_seen.update(defect_labels)
        clock = leave

        is_manual = manual_defect is not None and task_order == manual_apply_order
        if is_manual:
            task_order += 1
            ops_account = rng.choice(_OPS_ACCOUNTS)
            ops_enter = clock
            ops_dwell_seconds = rng.uniform(24, 72) * 3600
            ops_leave = ops_enter + timedelta(seconds=ops_dwell_seconds)
            events.append(
                _build_event(
                    case_id, task_order, _OPS_PSEUDO_NODE, ops_account, ops_enter, ops_leave, ops_dwell_seconds,
                    action=action_name,
                    category=ActionCategory.MANUAL_INTERVENTION,
                    target_node_id=target_node_id,
                    defect_labels=[manual_defect.defect_id],
                )
            )
            labels_seen.add(manual_defect.defect_id)
            clock = ops_leave

        if target_node_id is None:
            case_status = CaseStatus.TERMINATED
            closed_at = leave
            break
        if target_node_id == "END":
            case_status = CaseStatus.COMPLETED
            closed_at = leave
            break
        if target_node_id == "DRAFT":
            resubmit_count += 1
            current_node_id = "draft"
            continue
        current_node_id = target_node_id

    return CaseRecord(
        case_id=case_id,
        flow_code=process.meta.process_id,
        flow_name=process.meta.process_name,
        process_version=process.meta.version,
        case_status=case_status,
        initiator_user_id=initiator.user_id,
        initiator_name=initiator.name,
        initiator_dept_id=initiator.dept_id,
        initiator_dept_name=initiator.dept_name,
        case_attributes=case_attributes,
        created_at=arrival,
        closed_at=closed_at,
        events=events,
        injected_defect_ids=sorted(labels_seen),
    )


def _sample_dwell_hours(scenario: ScenarioConfig, node_id: str, rng: random.Random) -> float:
    param = scenario.node_dwell_params.get(node_id, _DEFAULT_DWELL)
    return max(0.05, rng.lognormvariate(param.mu, param.sigma))


def _select_path(node, context: dict[str, Any]):
    for path in node.submit_paths:
        if evaluate_condition(path.condition, context).matched:
            return path
    return None


def _categorize(target_node_id: str | None, chosen_path) -> ActionCategory:
    if target_node_id is None:
        return ActionCategory.TERMINATED_TIMEOUT
    if target_node_id == "END":
        return ActionCategory.END
    if target_node_id == "DRAFT":
        return ActionCategory.RETURN
    return ActionCategory.ROUTE_FORWARD


def _build_event(
    case_id: str,
    task_order: int,
    node,
    resource: ResolvedUser | None,
    enter: datetime,
    leave: datetime,
    dwell_seconds: float,
    *,
    action: str,
    category: ActionCategory,
    target_node_id: str | None,
    defect_labels: list[str],
) -> EventRecord:
    return EventRecord(
        event_id=f"{case_id}-ev{task_order}",
        case_id=case_id,
        task_order=task_order,
        node_id=node.node_id,
        node_name=node.node_name,
        resource_user_id=resource.user_id if resource else None,
        resource_name=resource.name if resource else None,
        resource_dept_id=resource.dept_id if resource else None,
        resource_dept_name=resource.dept_name if resource else None,
        enter_time=enter,
        leave_time=leave,
        dwell_seconds=dwell_seconds,
        action=action,
        action_category=category,
        target_node_id=target_node_id,
        injected_defect_labels=list(defect_labels),
    )


def _build_task_group_events(
    case_id: str,
    task_order: int,
    node,
    assignees: list[ResolvedUser],
    winner: ResolvedUser | None,
    enter: datetime,
    leave: datetime,
    dwell_seconds: float,
    action_name: str,
    category: ActionCategory,
    target_node_id: str | None,
    defect_labels: list[str],
) -> list[EventRecord]:
    """单人处理直接产出一条；多选-抢办产出赢家1条+其余"已被抢办"；
    多选-并行处理（会签）产出每位经办人各1条，动作/结果一致（简化：不建模个体分歧）。"""
    mode = node.handler.mode if node.handler else HandlerMode.SINGLE
    if not assignees or mode == HandlerMode.SINGLE:
        return [
            _build_event(
                case_id, task_order, node, winner, enter, leave, dwell_seconds,
                action=action_name, category=category, target_node_id=target_node_id,
                defect_labels=defect_labels,
            )
        ]

    if mode in (HandlerMode.MULTI_PREEMPT, HandlerMode.ALL_PREEMPT):
        events = [
            _build_event(
                case_id, task_order, node, winner, enter, leave, dwell_seconds,
                action=action_name, category=category, target_node_id=target_node_id,
                defect_labels=defect_labels,
            )
        ]
        for loser in assignees:
            if winner is not None and loser.user_id == winner.user_id:
                continue
            events.append(
                _build_event(
                    case_id, task_order, node, loser, enter, leave, 0.0,
                    action=f"已被{winner.name if winner else '他人'}抢办",
                    category=ActionCategory.PREEMPTED_LOST,
                    target_node_id=None,
                    defect_labels=[],
                )
            )
        return events

    # 多选/全选-并行处理（会签）：所有人各出一条，结果一致
    return [
        _build_event(
            case_id, task_order, node, member, enter, leave, dwell_seconds,
            action=action_name, category=category, target_node_id=target_node_id,
            defect_labels=defect_labels if member.user_id == (winner.user_id if winner else None) else [],
        )
        for member in assignees
    ]
