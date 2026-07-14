"""确定性指标层（分析侧闭环 Phase 2）。

吃一份合成事件日志（CaseRecord 列表），产出结构化指标 json。不含任何 LLM，
纯确定性计算——同一份日志任何时候重算，结果完全一致。维度参照真实 OA 效能
看板（doc/产品功能全景.md 模块7/8 规划）：总览 / 环节 / 路径 / 人工介入 /
资源 / 发起，字段命名与看板对照：

- 总览："正常办结流程平均处理时长"→overview.avg_case_duration_hours；
  "平均办结流程单流转环节数"→overview.avg_node_visits；
  "人工运维介入比例"→overview.manual_intervention_case_ratio。
- 环节："各环节平均处理时长"→node_metrics[node_id].avg_dwell_hours；
  "各环节平均处理频次"→node_metrics[node_id].avg_visits_per_case；
  "最后环节停滞情况分析"→overview.avg_last_node_dwell_hours。
- 路径：返工率、变体分布、违规跳级（结构化 conformance 校验，见下）。
- 人工介入：按"运维前环节"（相邻事件推导，不是独立字段）+ 下送路径分布。
- 资源/发起：对齐"处理人维度"/"部门发起维度"/"每月发起趋势"。

违规跳级（conformance check）是真正的重算，不是照抄注入病灶标签：从事件的
action_category 反推"结论性意见"（RETURN→不同意，END/ROUTE_FORWARD→同意），
用 app.runtime.condition_eval 在该环节 submit_paths 上重新求值，跟事件实际
记录的 target_node_id 比较——不一致即为一次结构性违规。这样即使不看
injected_defect_labels，这层指标本身就具备检测能力（评测时可以对照 gold
核实检出率，而不是同义反复）。
"""

from __future__ import annotations

import json
from collections import Counter, defaultdict
from pathlib import Path
from statistics import mean
from typing import Any

from app.generation.event_log_generator import OPS_NODE_ID
from app.runtime.condition_eval import evaluate_condition
from data.schema import ActionCategory, CaseRecord, CaseStatus, ProcessDefinition

_RETURN_OPINION = "不同意"
_FORWARD_OPINION = "同意"


def load_case_records(path: str | Path) -> list[CaseRecord]:
    lines = Path(path).read_text(encoding="utf-8").splitlines()
    return [CaseRecord.model_validate_json(line) for line in lines if line.strip()]


def compute_metrics(
    cases: list[CaseRecord],
    *,
    process: ProcessDefinition | None = None,
    assumed_sla_days: dict[str, float] | None = None,
) -> dict[str, Any]:
    assumed_sla_days = assumed_sla_days or {}
    return {
        "overview": _overview_metrics(cases),
        "node_metrics": _node_metrics(cases, assumed_sla_days),
        "path_metrics": _path_metrics(cases, process),
        "manual_intervention": _manual_intervention_metrics(cases),
        "resource_metrics": _resource_metrics(cases),
        "origination_metrics": _origination_metrics(cases),
    }


# ──────────────────────────────────────────────
# 总览
# ──────────────────────────────────────────────

def _overview_metrics(cases: list[CaseRecord]) -> dict[str, Any]:
    total = len(cases)
    status_counts = Counter(case.case_status.value for case in cases)
    completed = [c for c in cases if c.case_status == CaseStatus.COMPLETED]

    durations_hours = [(c.closed_at - c.created_at).total_seconds() / 3600 for c in completed if c.closed_at]
    node_visit_counts = [sum(1 for e in c.events if e.node_id != OPS_NODE_ID) for c in completed]
    last_node_dwells = [
        _real_events(c)[-1].dwell_seconds / 3600
        for c in completed
        if _real_events(c) and _real_events(c)[-1].dwell_seconds is not None
    ]
    manual_case_count = sum(1 for c in cases if any(e.node_id == OPS_NODE_ID for e in c.events))

    return {
        "case_count": total,
        "case_status_counts": dict(status_counts),
        "completion_rate": _safe_div(len(completed), total),
        "avg_case_duration_hours": _safe_mean(durations_hours),
        "avg_node_visits": _safe_mean(node_visit_counts),
        "avg_last_node_dwell_hours": _safe_mean(last_node_dwells),
        "manual_intervention_case_count": manual_case_count,
        "manual_intervention_case_ratio": _safe_div(manual_case_count, total),
    }


def _real_events(case: CaseRecord):
    return [e for e in case.events if e.node_id != OPS_NODE_ID]


# ──────────────────────────────────────────────
# 环节维度
# ──────────────────────────────────────────────

def _node_metrics(cases: list[CaseRecord], assumed_sla_days: dict[str, float]) -> dict[str, Any]:
    dwells_by_node: dict[str, list[float]] = defaultdict(list)
    visits_by_node: Counter = Counter()
    returns_by_node: Counter = Counter()
    arrivals_by_node: Counter = Counter()
    cases_touching_node: dict[str, set[str]] = defaultdict(set)
    sla_hits_by_node: Counter = Counter()
    sla_eligible_by_node: Counter = Counter()

    for case in cases:
        for event in case.events:
            if event.node_id == OPS_NODE_ID:
                continue
            visits_by_node[event.node_id] += 1
            cases_touching_node[event.node_id].add(case.case_id)
            arrivals_by_node[event.node_id] += 1
            if event.dwell_seconds is not None:
                dwells_by_node[event.node_id].append(event.dwell_seconds / 3600)
            if event.action_category == ActionCategory.RETURN:
                returns_by_node[event.node_id] += 1
            sla_days = assumed_sla_days.get(event.node_id)
            if sla_days is not None and event.dwell_seconds is not None:
                sla_eligible_by_node[event.node_id] += 1
                if event.dwell_seconds / 3600 <= sla_days * 24:
                    sla_hits_by_node[event.node_id] += 1

    result: dict[str, Any] = {}
    for node_id, visits in visits_by_node.items():
        touched_cases = len(cases_touching_node[node_id])
        entry: dict[str, Any] = {
            "visits": visits,
            "avg_dwell_hours": _safe_mean(dwells_by_node[node_id]),
            "avg_visits_per_case": _safe_div(visits, touched_cases),
            "return_rate": _safe_div(returns_by_node[node_id], arrivals_by_node[node_id]),
        }
        if sla_eligible_by_node[node_id]:
            entry["sla_achievement_rate"] = _safe_div(sla_hits_by_node[node_id], sla_eligible_by_node[node_id])
        result[node_id] = entry
    return result


# ──────────────────────────────────────────────
# 路径维度：返工率 / 变体 / 违规跳级（conformance check）
# ──────────────────────────────────────────────

def _path_metrics(cases: list[CaseRecord], process: ProcessDefinition | None) -> dict[str, Any]:
    rework_case_count = sum(
        1 for c in cases if any(e.action_category == ActionCategory.RETURN for e in c.events)
    )
    variant_counts: Counter = Counter()
    for case in cases:
        variant = tuple(e.node_id for e in _real_events(case))
        variant_counts[variant] += 1
    top_variants = [
        {"path": list(variant), "count": count}
        for variant, count in variant_counts.most_common(10)
    ]

    violations = _conformance_violations(cases, process) if process is not None else []

    return {
        "rework_case_count": rework_case_count,
        "rework_rate": _safe_div(rework_case_count, len(cases)),
        "distinct_variant_count": len(variant_counts),
        "top_variants": top_variants,
        "conformance_violation_count": len(violations),
        "conformance_violations": violations,
    }


def _reconstruct_opinion(category: ActionCategory) -> str | None:
    if category == ActionCategory.RETURN:
        return _RETURN_OPINION
    if category in (ActionCategory.END, ActionCategory.ROUTE_FORWARD):
        return _FORWARD_OPINION
    return None


def _conformance_violations(cases: list[CaseRecord], process: ProcessDefinition) -> list[dict[str, Any]]:
    """重新用 evaluate_condition 求一次"这个环节按案例属性+推断出的结论性意见
    应该走哪条路径"，跟事件实际记录的 target_node_id 比对；不一致记一条违规。
    不依赖 injected_defect_labels，是真正独立的重算。"""
    violations: list[dict[str, Any]] = []
    for case in cases:
        for event in case.events:
            if event.node_id == OPS_NODE_ID or event.target_node_id is None:
                continue
            node = process.get_node_by_id(event.node_id)
            if node is None or not node.submit_paths:
                continue
            opinion = _reconstruct_opinion(event.action_category)
            if opinion is None:
                continue
            context = {**case.case_attributes, "结论性意见": opinion}
            expected_target = None
            for path in node.submit_paths:
                if evaluate_condition(path.condition, context).matched:
                    expected_target = path.target_node_id
                    break
            if expected_target is not None and expected_target != event.target_node_id:
                violations.append(
                    {
                        "case_id": case.case_id,
                        "event_id": event.event_id,
                        "node_id": event.node_id,
                        "expected_target_node_id": expected_target,
                        "actual_target_node_id": event.target_node_id,
                    }
                )
    return violations


# ──────────────────────────────────────────────
# 人工介入维度
# ──────────────────────────────────────────────

def _manual_intervention_metrics(cases: list[CaseRecord]) -> dict[str, Any]:
    total_ops_events = 0
    by_prior_node: Counter = Counter()
    down_path_distribution: Counter = Counter()

    for case in cases:
        for index, event in enumerate(case.events):
            if event.node_id != OPS_NODE_ID:
                continue
            total_ops_events += 1
            prior_node_name = case.events[index - 1].node_name if index > 0 else "（无前置环节）"
            by_prior_node[prior_node_name] += 1
            down_path_distribution[event.action] += 1

    return {
        "total_ops_events": total_ops_events,
        "by_prior_node": dict(by_prior_node),
        "down_path_distribution": dict(down_path_distribution),
    }


# ──────────────────────────────────────────────
# 资源维度
# ──────────────────────────────────────────────

def _resource_metrics(cases: list[CaseRecord]) -> dict[str, Any]:
    visits: Counter = Counter()
    dwells: dict[str, list[float]] = defaultdict(list)
    names: dict[str, str] = {}

    for case in cases:
        for event in case.events:
            if event.node_id == OPS_NODE_ID or not event.resource_user_id:
                continue
            visits[event.resource_user_id] += 1
            names[event.resource_user_id] = event.resource_name or event.resource_user_id
            if event.dwell_seconds is not None:
                dwells[event.resource_user_id].append(event.dwell_seconds / 3600)

    return {
        user_id: {
            "name": names[user_id],
            "visits": count,
            "avg_dwell_hours": _safe_mean(dwells[user_id]),
        }
        for user_id, count in visits.most_common()
    }


# ──────────────────────────────────────────────
# 发起维度
# ──────────────────────────────────────────────

def _origination_metrics(cases: list[CaseRecord]) -> dict[str, Any]:
    by_dept: Counter = Counter(c.initiator_dept_name or "（未知部门）" for c in cases)
    by_attribute: dict[str, Counter] = defaultdict(Counter)
    monthly_trend: Counter = Counter()

    for case in cases:
        for key, value in case.case_attributes.items():
            by_attribute[key][str(value)] += 1
        monthly_trend[case.created_at.strftime("%Y%m")] += 1

    return {
        "by_department": dict(by_dept),
        "by_case_attribute": {key: dict(counter) for key, counter in by_attribute.items()},
        "monthly_trend": dict(sorted(monthly_trend.items())),
    }


# ──────────────────────────────────────────────
# 工具函数
# ──────────────────────────────────────────────

def _safe_mean(values: list[float]) -> float | None:
    return round(mean(values), 4) if values else None


def _safe_div(numerator: float, denominator: float) -> float | None:
    return round(numerator / denominator, 4) if denominator else None
