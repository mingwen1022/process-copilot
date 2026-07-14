"""流程助手（我的流程页 · 待办副驾）——确定性核心。

心法：**判对错全代码，LLM 只组织语言**。本模块只放确定性件——
- 急件排序：按 SLA 剩余/超时 + 停留时长 算 urgency 分（审批人侧）。
- 汇总计数：待批/紧急/发起/卡住 的确定性计数。
- 进度追踪 + ETA：从当前环节按字段值 evaluate_condition 走剩余路径 + 历史 avg_dwell 求和（发起人侧）。

审批协助的材料/合规复用 task_inspection；内容风险(值阈值)见 P2。详见 doc/流程助手-待办副驾-设计.md。
"""

from __future__ import annotations

from typing import Any, Optional

from pydantic import BaseModel, Field

from app.runtime.condition_eval import evaluate_condition
from data.schema import ProcessDefinition


class WorklistItem(BaseModel):
    instance_id: str
    title: str
    kind: str = Field(description="approval（待我审批）或 initiated（我发起的）")
    process_name: str = ""
    current_node_name: str = ""
    hours_to_deadline: Optional[float] = Field(default=None, description="距处理期限小时数；负=已超时；None=无期限")
    stay_hours: float = Field(default=0.0, description="当前环节已停留小时")
    stuck: bool = False
    stuck_reason: str = Field(default="", description="卡住的确定性根因短标签（如 覆盖漏洞/选不到人/填错数据），供列表行展示，不猜")
    category: str = Field(default="", description="流程分类，供审批协助按类检索内容风险规则")
    form_values: dict[str, Any] = Field(default_factory=dict, description="表单填报值，供审批协助内容风险比对")
    workflow_definition_id: Optional[str] = Field(default=None, description="有真实流程定义时填，供详情页读取审批意见/提交路径")
    node_id: Optional[str] = Field(default=None, description="配合 workflow_definition_id 定位当前环节；无实例上下文/已办结留空")
    initiator_user_id: str = Field(default="u_it_app_staff", description="发起人 org id——桥接成 ops 实例时据此解析各环节处理人，缺省用有部门归属的员工，避免处理人解析为空")
    uploaded_materials: list[str] = Field(default_factory=list, description="已上传的附件类型名；桥接后供在办体检判断按条件必传附件缺没缺")
    approval_history: list[dict[str, Any]] = Field(default_factory=list, description="已走完环节留下的审批意见记录（node_id/node_name/actor_user_id/opinion/comment），桥接后供实例详情页展示、供运维副驾 history_fix 订正")


# ——— 急件排序（确定性）———

def urgency_score(item: WorklistItem) -> float:
    """越大越急。确定性:超时>临期>久留;卡住加权。不让 LLM 猜谁急。"""
    score = 0.0
    d = item.hours_to_deadline
    if d is not None:
        if d < 0:
            score += 1000 + min(-d, 240)        # 已超时:最高档 + 超时越久越前
        elif d <= 24:
            score += 500 + (24 - d) * 5          # 24h 内临期
        else:
            score += max(0.0, 100 - d)           # 越远越不急
    score += min(item.stay_hours, 240) * 0.5     # 久留加权
    if item.stuck:
        score += 300
    return score


def rank(items: list[WorklistItem]) -> list[WorklistItem]:
    return sorted(items, key=urgency_score, reverse=True)


def is_urgent(item: WorklistItem) -> bool:
    """确定性紧急判定:已超时 或 24h 内临期。"""
    d = item.hours_to_deadline
    return d is not None and d <= 24


# ——— 汇总计数（确定性）———

def summarize(items: list[WorklistItem]) -> dict[str, int]:
    approvals = [i for i in items if i.kind == "approval"]
    initiated = [i for i in items if i.kind == "initiated"]
    return {
        "approval_total": len(approvals),
        "urgent": sum(1 for i in approvals if is_urgent(i)),
        "initiated_total": len(initiated),
        "stuck": sum(1 for i in initiated if i.stuck),
    }


# ——— 进度追踪 + ETA（确定性，复用 condition_eval + node_metrics）———

def _avg_dwell_hours(node_metrics: dict[str, Any], node_id: str) -> float:
    m = node_metrics.get(node_id) or {}
    val = m.get("avg_dwell_hours") if isinstance(m, dict) else getattr(m, "avg_dwell_hours", None)
    return float(val) if val is not None else 0.0


def track_progress(
    process: ProcessDefinition,
    current_node_id: str,
    form_values: dict[str, Any],
    node_metrics: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    """从当前环节按字段值走剩余前进路径(退回边不算前进)，到 END 为止；ETA=当前+剩余各环节历史平均停留之和。"""
    node_metrics = node_metrics or {}
    remaining: list[str] = []
    node_id: Optional[str] = current_node_id
    seen: set[str] = set()
    reached_end = False
    while node_id and node_id not in seen:
        if node_id == "END":
            reached_end = True
            break
        seen.add(node_id)
        node = process.get_node_by_id(node_id)
        if node is None:
            break
        nxt: Optional[str] = None
        for p in node.submit_paths:
            if p.target_node_id in ("DRAFT",):  # 退回起草不算前进
                continue
            if evaluate_condition(p.condition, form_values).matched:
                nxt = p.target_node_id
                break
        if nxt is None:
            break  # 卡住：当前环节无前进路径（如覆盖漏洞）
        if nxt == "END":
            reached_end = True
            break
        remaining.append(nxt)
        node_id = nxt

    eta_hours = _avg_dwell_hours(node_metrics, current_node_id) + sum(
        _avg_dwell_hours(node_metrics, n) for n in remaining
    )
    return {
        "current_node_id": current_node_id,
        "remaining_node_ids": remaining,
        "remaining_count": len(remaining),
        "eta_hours": round(eta_hours, 1),
        "reaches_end": reached_end,
    }


# ——— demo 数据：审批人收件箱 + 差旅标准规则 + 环节历史停留 ———

_DEMO_NODE_METRICS: dict[str, Any] = {
    "mgr": {"avg_dwell_hours": 24}, "gm": {"avg_dwell_hours": 48}, "line_leader": {"avg_dwell_hours": 12},
}


LEAVE_DEFINITION_ID = "wfd_leave_request_v1"  # 与 slice1_service.LEAVE_WORKFLOW_DEFINITION_ID 同值
EOA140_DEFINITION_ID = "wfd_eoa140_v1"  # 与 slice1_service.EOA140_WORKFLOW_DEFINITION_ID 同值（P2 补运行轴）
EXPENSE_DEFINITION_ID = "wfd_expense_v1"  # 与 slice1_service.EXPENSE_WORKFLOW_DEFINITION_ID 同值（P3 报销全套）
SEAL_DEFINITION_ID = "wfd_seal_v1"  # 与 slice1_service.SEAL_WORKFLOW_DEFINITION_ID 同值
PROCUREMENT_DEFINITION_ID = "wfd_procurement_v1"  # 与 slice1_service.PROCUREMENT_WORKFLOW_DEFINITION_ID 同值

# 有部门归属的员工（信息技术部应用开发部 · 王嘉树）——作为发起人时各环节"本部门主管/
# 总经理/条线领导"都能解析到真人，避免详情页出现"处理人为空"的假卡点。
_STAFF = "u_it_app_staff"
# 无部门归属的幽灵账号——专门用来演示"选不到人"（组织缺口）：条线分管领导环节解析为空。
_GHOST = "u_ghost_no_dept"


def build_mock_worklist() -> list[WorklistItem]:
    """demo 用工作清单——每张单都能桥接成完整 InstanceContext（字段/处理人/附件齐全），
    点进去有数据、可被 agent 测试。分两组：

    待我审批（当前环节处理人，可给结论）——含 1 张差旅报销 3 类金额出入（超标/总额不符/缺附件）；
    我发起的（发起人视角，问进度/找运维）——含 停留过久、覆盖漏洞卡住、选不到人、会签并行 等场景。
    """
    return [
        # ========== 待我审批（approval，can_submit_decision=True）==========
        # ① 差旅报销：三类金额出入叠加——酒店 1000>非一线 800 上限；报销总额 9200≠明细合计
        #    8600；超标却未上传"情况说明"附件。审批助手应把三条都标出来。已超时=最急。
        WorklistItem(instance_id="ap_expense_wang", title="差旅报销 · 王工 · ¥9,200", kind="approval",
                     process_name="费用报销流程", current_node_name="部门主管审批",
                     hours_to_deadline=-12, stay_hours=60, category="财务费用",
                     initiator_user_id=_STAFF, uploaded_materials=["发票"],
                     form_values={"报销人": "王嘉树", "所属部门": "信息技术部应用开发部", "费用类型": "差旅费",
                                  "出差城市": "非一线", "酒店每晚": 1000,
                                  "费用明细": "7/1-7/3 酒店 3 晚 3000 元；机票往返 4200 元；市内交通 1400 元",
                                  "报销总额": 9200, "收款账户": "6222 **** 1234", "报销事由": "北京出差对接客户"},
                     workflow_definition_id=EXPENSE_DEFINITION_ID, node_id="dept_manager_approve"),
        # ② 用印申请：材料齐、无金额风险——正常可批（对照组）。临期。
        WorklistItem(instance_id="ap_seal_zhao", title="用印申请 · 赵工 · 合同章", kind="approval",
                     process_name="用印申请流程", current_node_name="法务审核", hours_to_deadline=6, stay_hours=20,
                     initiator_user_id="u_admin_staff", uploaded_materials=["用印文件"],
                     form_values={"申请人": "赵文博", "所属部门": "综合管理部行政部",
                                  "印章类型": "合同章", "用印事由": "与供应商签订年度采购框架合同"},
                     workflow_definition_id=SEAL_DEFINITION_ID, node_id="legal_review"),
        # ③ 请假·年假2天：材料齐、路径清晰——正常可批。
        WorklistItem(instance_id="ap_leave_sun", title="请假 · 孙工 · 年假2天", kind="approval",
                     process_name="员工请假申请流程", current_node_name="部门主管审批", hours_to_deadline=48, stay_hours=4,
                     initiator_user_id=_STAFF,
                     form_values={"申请人": "孙悦", "所属部门": "信息技术部应用开发部", "请假类型": "年假",
                                  "开始日期": "2026-07-13", "结束日期": "2026-07-14", "请假天数": 2,
                                  "请假事由": "家庭旅行", "工作交接人": "王嘉树", "联系电话": "138****2013"},
                     workflow_definition_id=LEAVE_DEFINITION_ID, node_id="dept_supervisor"),
        # ④ 采购申请：金额未达大额门槛，受理人处理——正常。
        WorklistItem(instance_id="ap_proc_zhou", title="采购申请 · 周工 · ¥48,000", kind="approval",
                     process_name="采购申请流程", current_node_name="采购受理", hours_to_deadline=72, stay_hours=6,
                     initiator_user_id=_STAFF,
                     form_values={"申请人": "周霖", "所属部门": "信息技术部应用开发部",
                                  "采购事项": "开发测试服务器 2 台", "采购金额": 48000,
                                  "采购说明": "项目组测试环境扩容，采购中端机架式服务器两台"},
                     workflow_definition_id=PROCUREMENT_DEFINITION_ID, node_id="procurement_intake"),
        # ⑤ 请假·事假1天：小事假，部门主管即可结束——正常。
        WorklistItem(instance_id="ap_leave_wu", title="请假 · 吴工 · 事假1天", kind="approval",
                     process_name="员工请假申请流程", current_node_name="部门主管审批", hours_to_deadline=None, stay_hours=1,
                     initiator_user_id=_STAFF,
                     form_values={"申请人": "吴桐", "所属部门": "信息技术部安全部", "请假类型": "事假",
                                  "开始日期": "2026-07-11", "结束日期": "2026-07-11", "请假天数": 1,
                                  "请假事由": "个人事务", "工作交接人": "孙悦", "联系电话": "138****2015"},
                     workflow_definition_id=LEAVE_DEFINITION_ID, node_id="dept_supervisor"),
        # ========== 我发起的（initiated，can_submit_decision=False）==========
        # ⑥ 覆盖漏洞卡住：事假5天落在部门总经理环节的 4–7 天空档，无前进路径。桥接到 ops
        #    fixture（专用的带漏洞流程），详情页看诊断/授权，不看提交路径（合成流程无发布定义）。
        WorklistItem(instance_id="inst_gap", title="请假 · 事假5天（卡住）", kind="initiated",
                     process_name="员工请假申请流程", current_node_name="部门总经理审批", stay_hours=96, stuck=True,
                     stuck_reason="覆盖漏洞",
                     form_values={"申请人": "王嘉树", "所属部门": "信息技术部应用开发部", "请假类型": "事假",
                                  "开始日期": "2026-07-14", "结束日期": "2026-07-18", "请假天数": 5,
                                  "请假事由": "家中有事", "工作交接人": "李承宇", "联系电话": "138****2010"}),
        # ⑦ 停留过久：培训费报销卡在财务初审已 5 天（远超均值），流转正常但太慢——发起人可
        #    问运维"为什么这么久 / 能不能催办 / 能不能跳过"。
        WorklistItem(instance_id="my_expense_slow", title="培训费报销 · ¥62,000（停留过久）", kind="initiated",
                     process_name="费用报销流程", current_node_name="财务初审", stay_hours=120,
                     initiator_user_id=_STAFF, uploaded_materials=["发票"],
                     form_values={"报销人": "王嘉树", "所属部门": "信息技术部应用开发部", "费用类型": "培训费",
                                  "费用明细": "团队外部技能培训课程费 62000 元", "报销总额": 62000,
                                  "收款账户": "6222 **** 9012", "报销事由": "团队年度技能培训"},
                     workflow_definition_id=EXPENSE_DEFINITION_ID, node_id="finance_review"),
        # ⑧ 选不到人（组织缺口）：病假9天走到条线分管领导环节，发起人无部门归属→解析不到审批
        #    人。发起人可请运维改派/挂接。
        WorklistItem(instance_id="my_leave_orggap", title="请假 · 病假9天（选不到人）", kind="initiated",
                     process_name="员工请假申请流程", current_node_name="条线分管领导审批", stay_hours=80, stuck=True,
                     stuck_reason="选不到人",
                     initiator_user_id=_GHOST, uploaded_materials=["证明材料"],
                     form_values={"申请人": "（待补部门归属）", "所属部门": "（离职交接中，暂无归属）",
                                  "请假类型": "病假", "开始日期": "2026-07-06", "结束日期": "2026-07-14",
                                  "请假天数": 9, "请假事由": "住院治疗", "工作交接人": "王嘉树", "联系电话": "138****2099"},
                     workflow_definition_id=LEAVE_DEFINITION_ID, node_id="line_leader"),
        # ⑨ 正常流转：年假2天在部门主管环节——发起人问进度，或试探"能不能退回改一下"。
        WorklistItem(instance_id="my_leave_normal", title="请假 · 年假2天", kind="initiated",
                     process_name="员工请假申请流程", current_node_name="部门主管审批", stay_hours=20,
                     initiator_user_id=_STAFF,
                     form_values={"申请人": "王嘉树", "所属部门": "信息技术部应用开发部", "请假类型": "年假",
                                  "开始日期": "2026-07-11", "结束日期": "2026-07-12", "请假天数": 2,
                                  "请假事由": "调休休息", "工作交接人": "李承宇", "联系电话": "138****2010"},
                     workflow_definition_id=LEAVE_DEFINITION_ID, node_id="dept_supervisor"),
        # ⑩ 会签/并行：EOA140 走到对口部门专员分发，按"对口部门"字段并行派给多个会签专员——
        #    与请假的单线审批形成对比，可让 agent 解释多人会签怎么走。
        WorklistItem(instance_id="eoa140_case_2", title="子公司年度关联交易备案（会签中）", kind="initiated",
                     process_name="子公司重大事项审批备案流程", current_node_name="对口部门专员分发", stay_hours=10,
                     initiator_user_id=_STAFF,
                     form_values={"标题": "关于XX子公司年度关联交易备案", "编号": "EOA140-2026-0045",
                                  "起草人": "王嘉树", "起草时间": "2026-07-06", "部门": "计划财务部",
                                  "联系电话": "138****2010", "任务类型": "子公司备案类", "任务名称": "年度关联交易备案",
                                  "对口部门": ["风险管理部", "计划财务部", "法律合规部"], "重要程度": "较高",
                                  "要求完成日期": "2026-07-25"},
                     workflow_definition_id=EOA140_DEFINITION_ID, node_id="counterpart_dispatch"),
        # ⑪ 复杂多环节审批：EOA140 增资扩股走到子公司高管审批——正常在途。
        WorklistItem(instance_id="eoa140_case_1", title="子公司增资扩股事项审批", kind="initiated",
                     process_name="子公司重大事项审批备案流程", current_node_name="子公司高管审批", stay_hours=30,
                     initiator_user_id=_STAFF,
                     form_values={"标题": "关于XX子公司增资扩股的重大事项审批", "编号": "EOA140-2026-0031",
                                  "起草人": "王嘉树", "起草时间": "2026-07-05", "部门": "战略发展部",
                                  "联系电话": "138****2010", "任务类型": "子公司审批类", "任务名称": "增资扩股事项审批",
                                  "对口部门": ["战略发展部", "法律合规部"], "重要程度": "很高", "要求完成日期": "2026-07-20"},
                     workflow_definition_id=EOA140_DEFINITION_ID, node_id="subsidiary_executive_approval",
                     approval_history=[
                         {"node_id": "subsidiary_internal_review", "node_name": "子公司内部人员审核",
                          "actor_user_id": "u_it_app_supervisor", "action": "同意", "opinion": "同意",
                          "comment": "增资方案已核实，资金来源与出资比例材料齐全，符合子公司内部审核要求，同意上报子公司高管审批。",
                          "occurred_at": "2026-07-06T14:20:00"},
                     ]),
        # ⑫ 正常小额报销：办公用品在部门主管环节——正常在途（对照组）。
        WorklistItem(instance_id="expense_normal", title="办公用品报销 · ¥1,280", kind="initiated",
                     process_name="费用报销流程", current_node_name="部门主管审批", stay_hours=6,
                     initiator_user_id=_STAFF, uploaded_materials=["发票"],
                     form_values={"报销人": "王嘉树", "所属部门": "信息技术部应用开发部", "费用类型": "办公用品",
                                  "费用明细": "文具及打印耗材采购 1280 元", "报销总额": 1280,
                                  "收款账户": "6222 **** 5678", "报销事由": "部门日常办公用品采购"},
                     workflow_definition_id=EXPENSE_DEFINITION_ID, node_id="dept_manager_approve"),
    ]


def _travel_rules():
    """差旅标准 VALUE_THRESHOLD 规则（demo：代码经"检索"后传入 evaluator，retrieve-then-read）。"""
    from data.schema import AtomicRule, CheckKind, RuleDimension, RuleProvenance, RuleRequirement

    prov = RuleProvenance(source_doc="差旅费报销标准", clause="第3条")
    return [
        AtomicRule(rule_id="expense.hotel_normal", title="酒店住宿标准（非一线）", dimension=RuleDimension.COMPANY_POLICY,
                   statement="非一线城市酒店住宿每晚不超过 800 元", check_type="deterministic",
                   applicability_condition="出差城市∉一线",
                   requirement=RuleRequirement(kind=CheckKind.VALUE_THRESHOLD,
                                               params={"field": "酒店每晚", "op": "<=", "threshold": 800,
                                                       "message": "酒店 1000/晚 超差旅标准（非一线 800/晚上限）"}),
                   provenance=prov),
        AtomicRule(rule_id="expense.hotel_tier1", title="酒店住宿标准（一线）", dimension=RuleDimension.COMPANY_POLICY,
                   statement="一线城市酒店住宿每晚不超过 1000 元", check_type="deterministic",
                   applicability_condition="出差城市=一线",
                   requirement=RuleRequirement(kind=CheckKind.VALUE_THRESHOLD,
                                               params={"field": "酒店每晚", "op": "<=", "threshold": 1000,
                                                       "message": "酒店超一线城市标准 1000/晚"}),
                   provenance=prov),
    ]


# ——— 待办清单助手（列表级导航/问答）———
# 列表页的对话大脑：基于确定性算好的两组数据（急件排序 + 汇总计数）回答"哪些急/几单卡住/
# 某单什么情况"这类清单层面的问题，或识别"打开某一单"的意图并给出跳转。判急/计数仍是确定性
# 的（rank/summarize），LLM 只做理解意图 + 组织语言 + 从清单里挑出用户指的那一单，不编造。
# 具体单据的办理/排障不在这里，点进单据交给"在办副驾"（ops）。
#
# 概念边界（易混淆，故显式标注）：只有"待我审批"是真正的待办任务；"我发起的"是用户自己
# 起草、当前在别人手里流转的单子，用户只是跟踪进度，不是用户的待办——不能把两者合并计数
# 成一个"待办总数"，也不能说"我发起的"单子是"待处理"。见 _todo_system_prompt/_todo_user_prompt。


class TodoAnswer(BaseModel):
    reply: str = Field(description="基于清单数据的自然语言回答，只依据给出的数据，不编造没有的单据或数字；"
                       "不把「我发起的」在途单据说成待办/待处理")
    target_instance_id: Optional[str] = Field(
        default=None,
        description="若用户想打开/处理某一具体单据，填它的 instance_id（必须是清单里真实存在的）；"
        "只是问汇总/清单层面的问题（哪些急、几单卡住等）则留空，不要瞎猜或填占位符",
    )


def _todo_system_prompt() -> str:
    return """你是企业 OA「我的流程」页的待办清单助手。给你两组性质不同的单据，**不要把它们混为一谈**：

1.【待办任务】"待我审批"的单子——需要用户处理，是真正意义上的"待办"。
2.【我发起的·仅供跟踪】用户自己发起、当前在别人手里流转的单子——用户只是关心进度，**不是用户的待办
   任务**，不需要用户做任何事。用户问"我有几单待办/要处理"时，只该数第 1 组；问"我发起的怎么样了"
   才涉及第 2 组。回答里提到第 2 组时用"在途""进度"这类词，不要说成"待处理""待办"。

给你的清单已经按急件顺序排好、并标好状态（是否紧急/是否卡住/停留多久）。你的职责：基于这份清单回答
清单层面的问题——哪些最急、有几单卡住/卡在哪、某类单子有哪些、某一单现在什么情况。只依据清单里的
数据作答，不要编造清单里没有的单据、数字或结论。判急/计数已经是确定性算好的，直接引用，不要自己
重新排序或猜。

若用户想"打开/处理/去看"某一具体单据，从清单里找出那一单，把它的 instance_id 填进 target_instance_id
（必须是清单里真实存在的 id）；只是问汇总/清单层面的问题就把 target_instance_id 留空。

清单里每条前面方括号里的 id（如 [inst_gap]）是给你定位用的内部标识，**回答正文里绝对不要出现这个 id**，
提到某一单一律用它的中文标题（如"事假5天""差旅报销·王工"）。

注意边界：具体单据怎么审批、卡住怎么排障，是点进单据后由「在办副驾」处理的，不在你这里做。用户问到
具体处置动作时，可以引导他点开那一单（填 target_instance_id）交给在办副驾，不要自己替他做决定。
用简洁中文回答，不用英文双引号。"""


def _render_todo_history(history: Optional[list[dict[str, str]]]) -> str:
    if not history:
        return ""
    lines = []
    for h in history[-6:]:
        role = "用户" if h.get("role") == "user" else "助手"
        content = (h.get("content") or "").strip()
        if content:
            lines.append(f"{role}：{content[:300]}")
    return "\n".join(lines)


def _todo_item_line(it: dict[str, Any]) -> str:
    tags = []
    if it.get("urgent"):
        tags.append("紧急")
    if it.get("stuck"):
        tags.append(f"卡住（{it.get('stuck_reason') or '待排障'}）")
    htd = it.get("hours_to_deadline")
    if htd is not None:
        tags.append(f"距处理期限{round(htd)}h" if htd >= 0 else f"已超时{round(-htd)}h")
    node = it.get("current_node_name") or ""
    tag_text = f" · {'、'.join(tags)}" if tags else ""
    return f"  - [{it.get('instance_id')}] {it.get('title')}（{it.get('process_name')}） · 当前在「{node}」{tag_text}"


def _todo_user_prompt(worklist: dict[str, Any], message: str, history: Optional[list[dict[str, str]]]) -> str:
    summary = worklist.get("summary", {})
    items = worklist.get("items", [])
    approvals = [it for it in items if it.get("kind") == "approval"]
    initiated = [it for it in items if it.get("kind") != "approval"]
    approval_text = "\n".join(_todo_item_line(it) for it in approvals) or "（无）"
    initiated_text = "\n".join(_todo_item_line(it) for it in initiated) or "（无）"
    summary_text = (f"待办任务 {summary.get('approval_total', 0)} 项（{summary.get('urgent', 0)} 项紧急）；"
                    f"我发起的在途单据 {summary.get('initiated_total', 0)} 单（仅跟踪，非待办，{summary.get('stuck', 0)} 单卡住）。")
    hist = _render_todo_history(history)
    hist_block = f"\n\n最近对话（理解指代，如「那第二单呢」）：\n{hist}" if hist else ""
    return (
        f"概况：{summary_text}\n\n"
        f"【待办任务】待我审批 · 已按急件顺序排列：\n{approval_text}\n\n"
        f"【我发起的 · 仅供跟踪进度，不是待办】：\n{initiated_text}"
        f"{hist_block}\n\n用户当前这句：{message}"
    )


class TodoCopilotAgent:
    def __init__(self, model: Any | None = None) -> None:
        from app.models.bedrock import create_bedrock_chat_model
        from app.models.text import is_legacy_text_generator

        self.model = model or create_bedrock_chat_model()
        self.structured_model = (
            None
            if is_legacy_text_generator(self.model)
            else self.model.with_structured_output(TodoAnswer, method="function_calling", include_raw=True)
        )

    def answer(self, worklist: dict[str, Any], message: str, history: Optional[list[dict[str, str]]] = None) -> Optional[TodoAnswer]:
        if self.structured_model is None:
            return None
        response = self.structured_model.invoke(
            [
                {"role": "system", "content": _todo_system_prompt()},
                {"role": "user", "content": _todo_user_prompt(worklist, message, history)},
            ]
        )
        return _coerce_todo_answer(response)


def _coerce_todo_answer(response: Any) -> Optional[TodoAnswer]:
    parsed: Any = None
    parsing_error: Any = None
    if isinstance(response, dict):
        parsed = response.get("parsed")
        parsing_error = response.get("parsing_error")
    elif isinstance(response, TodoAnswer):
        parsed = response
    else:
        parsed = getattr(response, "parsed", None)
        parsing_error = getattr(response, "parsing_error", None)
    if parsing_error or parsed is None:
        return None
    if not isinstance(parsed, TodoAnswer):
        try:
            parsed = TodoAnswer.model_validate(parsed)
        except Exception:  # noqa: BLE001
            return None
    return parsed


class TodoCopilotService:
    """流程助手：列表级汇总/排序 + 单据级审批协助/进度。判对错全确定性；LLM 措辞层在 API 上层。"""

    def __init__(
        self,
        ops_service: Any | None = None,
        node_metrics: Optional[dict[str, Any]] = None,
        agent: Any | None = None,
        model: Any | None = None,
    ) -> None:
        self._items = {i.instance_id: i for i in build_mock_worklist()}
        self._ops = ops_service           # 复用 ops 实例做进度追踪（有 context）
        self._node_metrics = node_metrics or _DEMO_NODE_METRICS
        self._agent = agent               # 列表级问答 agent（惰性构造，避免无凭证环境建 Bedrock）
        self._model = model

    def _get_agent(self) -> TodoCopilotAgent:
        if self._agent is None:
            self._agent = TodoCopilotAgent(model=self._model)
        return self._agent

    def ask(self, message: str, history: Optional[list[dict[str, str]]] = None) -> dict[str, Any]:
        """列表级导航/问答：基于确定性排好的待办清单回答，或识别"打开某一单"的意图给出跳转。"""
        clean = (message or "").strip()
        if not clean:
            return {"reply": "问我：哪几单最急、我有几单卡住、帮我打开某一单——具体单据的办理/排障点进去交给在办副驾。"}
        answer = self._get_agent().answer(self.worklist(), clean, history)
        if answer is None:
            return {"reply": "待办助手暂不可用（模型未就绪）。"}
        result: dict[str, Any] = {"reply": answer.reply}
        # 校验 target_instance_id 是清单里真实存在的，模型偶尔会吐占位符/瞎猜，不能直接信
        tid = answer.target_instance_id
        if tid and tid in self._items:
            result["target_instance_id"] = tid
            result["target_title"] = self._items[tid].title
        return result

    def worklist(self) -> dict[str, Any]:
        items = list(self._items.values())
        ranked = rank(items)
        return {
            "summary": summarize(items),
            "items": [{**i.model_dump(), "urgent": is_urgent(i), "urgency_score": round(urgency_score(i), 1)}
                      for i in ranked],
        }

    def ensure_ops_instance(self, item_id: str) -> bool:
        """把这条待办队列条目桥接成一个真正的 ops 实例（InstanceContext），供 inspect/
        diagnose/propose/confirm/discard 之后直接按 item_id 复用同一套 harness——"办理"
        （审批协助）和"排障"（运维）在这之后就是同一份数据、同一个决策空间，不用两条腿。
        幂等：条目本来就在 ops mock 库里（"我发起的"那几条）时是 no-op。返回是否可诊断。"""
        item = self._items.get(item_id)
        if item is None or self._ops is None:
            return False
        inst = self._ops.ensure_instance(
            item_id,
            workflow_definition_id=item.workflow_definition_id,
            node_id=item.node_id,
            form_values=item.form_values,
            title=item.title,
            process_name=item.process_name,
            can_submit_decision=(item.kind == "approval"),
            initiator_user_id=item.initiator_user_id,
            uploaded_materials=item.uploaded_materials,
            approval_history=item.approval_history,
        )
        return inst is not None

    def reset_item(self, item_id: str) -> bool:
        """演示重置：撤销运维/在办副驾对这条单据做过的任何修改（数据订正/结论提交/跳转等），
        回到最初模拟状态。WorklistItem 本身是不可变的静态快照（每次进程启动都从
        build_mock_worklist() 现造），天然就是"最初状态"的定义——重置不用另存一份，直接让
        ops 把桥接出来的实例摘掉/出厂 fixture 现算复原，再照这条静态条目重新桥接一遍即可。"""
        item = self._items.get(item_id)
        if item is None or self._ops is None:
            return False
        self._ops.reset_instance(item_id)
        return self.ensure_ops_instance(item_id)

    def reset_all(self) -> int:
        """演示重置：一键把待办队列里所有单据都撤销修改、回到最初模拟状态——供演示前
        或演示间隙"清一遍现场"，不用逐条点、也不用重启进程。返回实际复位成功的条数。"""
        if self._ops is None:
            return 0
        return sum(1 for item_id in self._items if self.reset_item(item_id))

    def progress(self, instance_id: str) -> dict[str, Any]:
        inst = None
        if self._ops:
            try:
                inst = self._ops.get_instance(instance_id)
            except KeyError:
                inst = None
        ctx = getattr(inst, "context", None) if inst else None
        if ctx is None:
            return {"trackable": False}
        p = track_progress(ctx.process, ctx.current_node_id, ctx.form_values, self._node_metrics)
        names = {n.node_id: n.node_name for n in ctx.process.flow_nodes}
        p["current_node_name"] = names.get(p["current_node_id"], p["current_node_id"])
        p["remaining_node_names"] = [names.get(n, n) for n in p["remaining_node_ids"]]
        p["eta_days"] = round(p["eta_hours"] / 24, 1)
        p["trackable"] = True
        return p

    def approval_assist(self, item_id: str) -> dict[str, Any]:
        """审批协助（决策支持）：内容风险(值vs制度阈值 + 总额/明细一致性) + human-in-the-loop 提示。材料/合规见 ops.inspect。"""
        from app.copilots.content_risk import check_content_risk, check_expense_total_consistency

        item = self._items.get(item_id)
        if item is None or item.kind != "approval":
            return {"found": False}
        content_risk: list[dict[str, Any]] = []
        if item.category == "财务费用":
            # 三类金额出入：①超制度阈值（酒店/晚）②总额与明细不符——都是确定性核对
            content_risk = check_content_risk(item.form_values, _travel_rules())
            content_risk += check_expense_total_consistency(item.form_values)
        return {
            "found": True,
            "item_id": item_id,
            "title": item.title,
            "content_risk": content_risk,
            "actions": ["批准", "驳回并说明", "要求补充"],
            "note": "副驾只做摘要与风险标记；批准/驳回由你点按钮确认（human-in-the-loop）。",
        }
