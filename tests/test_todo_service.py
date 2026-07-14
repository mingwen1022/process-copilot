from __future__ import annotations

from app.copilots.mock_instances import _leave_gap_process
from app.copilots.todo_service import (
    WorklistItem,
    is_urgent,
    rank,
    summarize,
    track_progress,
    urgency_score,
)


def _item(**kw) -> WorklistItem:  # noqa: ANN003
    base = dict(instance_id="i", title="t", kind="approval")
    base.update(kw)
    return WorklistItem(**base)


# ——— 急件排序（确定性）———

def test_overdue_outranks_near_deadline_outranks_long_stay() -> None:
    overdue = _item(hours_to_deadline=-12)      # 已超时
    near = _item(hours_to_deadline=6)           # 临期
    stale = _item(hours_to_deadline=100, stay_hours=200)  # 久留但不临期
    assert urgency_score(overdue) > urgency_score(near) > urgency_score(stale)


def test_stuck_adds_weight() -> None:
    a = _item(kind="initiated", hours_to_deadline=None, stay_hours=10, stuck=True)
    b = _item(kind="initiated", hours_to_deadline=None, stay_hours=10, stuck=False)
    assert urgency_score(a) > urgency_score(b)


def test_rank_orders_by_urgency() -> None:
    items = [
        _item(instance_id="low", hours_to_deadline=100),
        _item(instance_id="overdue", hours_to_deadline=-5),
        _item(instance_id="near", hours_to_deadline=3),
    ]
    assert [i.instance_id for i in rank(items)] == ["overdue", "near", "low"]


def test_is_urgent_threshold() -> None:
    assert is_urgent(_item(hours_to_deadline=-1)) is True   # 超时
    assert is_urgent(_item(hours_to_deadline=24)) is True   # 临期边界
    assert is_urgent(_item(hours_to_deadline=25)) is False
    assert is_urgent(_item(hours_to_deadline=None)) is False


# ——— 汇总计数（确定性）———

def test_summarize_counts() -> None:
    items = [
        _item(kind="approval", hours_to_deadline=-1),   # 紧急
        _item(kind="approval", hours_to_deadline=3),    # 紧急
        _item(kind="approval", hours_to_deadline=100),  # 不急
        _item(kind="initiated", stuck=True),
        _item(kind="initiated", stuck=False),
    ]
    assert summarize(items) == {"approval_total": 3, "urgent": 2, "initiated_total": 2, "stuck": 1}


# ——— 进度 + ETA（确定性，复用 condition_eval + node_metrics）———

_METRICS = {"mgr": {"avg_dwell_hours": 24}, "gm": {"avg_dwell_hours": 48}, "line_leader": {"avg_dwell_hours": 12}}


def test_progress_flows_to_end_and_sums_eta() -> None:
    proc = _leave_gap_process()
    # 从部门主管、年假2天、同意 → 走到 gm(≤3→END)。剩余=[gm]，ETA=avg(mgr)+avg(gm)=72
    p = track_progress(proc, "mgr", {"结论性意见": "同意", "请假天数": 2}, _METRICS)
    assert p["remaining_node_ids"] == ["gm"]
    assert p["remaining_count"] == 1
    assert p["eta_hours"] == 72.0
    assert p["reaches_end"] is True


def test_progress_stops_at_coverage_gap() -> None:
    proc = _leave_gap_process()
    # 事假5天落进 4–7 天覆盖漏洞：在 gm 无前进路径 → 走不到 END
    p = track_progress(proc, "gm", {"结论性意见": "同意", "请假天数": 5}, _METRICS)
    assert p["remaining_node_ids"] == []
    assert p["reaches_end"] is False


def test_progress_long_leave_routes_to_line_leader() -> None:
    proc = _leave_gap_process()
    # 10 天(>7)：gm→line_leader→END
    p = track_progress(proc, "gm", {"结论性意见": "同意", "请假天数": 10}, _METRICS)
    assert p["remaining_node_ids"] == ["line_leader"]
    assert p["reaches_end"] is True


# ——— TodoCopilotService（列表汇总/排序 + 审批协助 + 进度）———

def test_service_worklist_ranks_and_summarizes() -> None:
    from app.copilots.todo_service import TodoCopilotService

    w = TodoCopilotService().worklist()
    # 2 张卡住：覆盖漏洞(inst_gap) + 选不到人(my_leave_orggap)
    assert w["summary"] == {"approval_total": 5, "urgent": 2, "initiated_total": 7, "stuck": 2}
    assert w["items"][0]["instance_id"] == "ap_expense_wang"  # 超时的差旅报销最急


def test_service_approval_assist_flags_content_risk() -> None:
    from app.copilots.todo_service import TodoCopilotService

    a = TodoCopilotService().approval_assist("ap_expense_wang")
    # 两类金额出入：酒店超制度标准 + 报销总额与明细合计不符
    assert a["found"] and len(a["content_risk"]) == 2
    rule_ids = {r["rule_id"] for r in a["content_risk"]}
    assert "expense.hotel_normal" in rule_ids  # 酒店 1000 > 非一线 800
    assert "expense.total_matches_itemized" in rule_ids  # 报销总额 9200 ≠ 明细合计 8600
    assert any("800" in r["message"] for r in a["content_risk"])
    assert a["actions"] == ["批准", "驳回并说明", "要求补充"]  # human-in-the-loop


def test_service_approval_assist_non_expense_no_content_risk() -> None:
    from app.copilots.todo_service import TodoCopilotService

    assert TodoCopilotService().approval_assist("ap_seal_zhao")["content_risk"] == []


def test_service_progress_reuses_ops_context_and_detects_gap() -> None:
    from app.copilots.ops_service import OpsCopilotService
    from app.copilots.todo_service import TodoCopilotService

    svc = TodoCopilotService(ops_service=OpsCopilotService())
    p = svc.progress("inst_gap")  # 事假5天卡在覆盖漏洞
    assert p["trackable"] and p["reaches_end"] is False
    assert p["current_node_name"] == "部门总经理审批"


def test_service_progress_does_not_500_on_unknown_instance() -> None:
    """回归：progress() 以前直接 get_instance() 会对未知 id 抛 KeyError；现在应
    优雅降级成 trackable=False，不管 id 是不是已经桥接过。"""
    from app.copilots.ops_service import OpsCopilotService
    from app.copilots.todo_service import TodoCopilotService

    svc = TodoCopilotService(ops_service=OpsCopilotService())
    assert svc.progress("totally_unknown_id") == {"trackable": False}


# ——— ensure_ops_instance：把待办队列条目桥接成真正的 ops 实例 ———

def test_ensure_ops_instance_bridges_approval_item_with_can_submit_decision_true() -> None:
    """ap_expense_wang 的 node_id 是 "dept_manager_approve"——现造一个带这个 node_id
    的流程给 lookup 返回，验证 TodoCopilotService 确实把 WorklistItem 的
    workflow_definition_id/node_id/form_values 原样传给了 ops.ensure_instance，
    且 kind=approval 时 can_submit_decision 正确传 True。"""
    from app.copilots.ops_service import OpsCopilotService
    from app.copilots.todo_service import TodoCopilotService
    from data.schema import ProcessDefinition

    def lookup(wdid: str) -> ProcessDefinition:
        assert wdid == "wfd_expense_v1"
        return ProcessDefinition.model_validate(
            {
                "meta": {"process_id": "EXPENSE-001", "process_name": "费用报销流程", "version": "V1.0.0",
                         "responsible_dept": "计划财务部", "description": "测试用",
                         "applicant_scope": "全员", "entry_point": "OA系统"},
                "form_fields": [],
                "flow_nodes": [
                    {"node_id": "dept_manager_approve", "node_name": "部门主管审批", "is_draft": False,
                     "handler": {"mode": "单选-单人处理", "source": "本部门", "role": "部门主管", "source_field": None},
                     "submit_paths": [{"path_name": "流程结束", "condition": "结论性意见=同意", "target_node_id": "END"}]},
                ],
                "attachments": None, "roles": None,
            }
        )

    ops = OpsCopilotService(process_lookup=lookup)
    svc = TodoCopilotService(ops_service=ops)
    assert svc.ensure_ops_instance("ap_expense_wang") is True
    inst = ops.get_instance("ap_expense_wang")
    assert inst.can_submit_decision is True
    assert inst.context.current_node_id == "dept_manager_approve"
    assert inst.context.form_values.get("报销总额") == 9200  # WorklistItem 的真实表单值原样传入
    # 发起人有部门归属 → 部门主管环节能解析到真人（不再"处理人为空"）
    assert inst.context.approver_resolution is not None
    assert inst.context.approver_resolution.is_empty is False


def test_ensure_ops_instance_is_noop_for_items_already_in_ops_mock_library() -> None:
    from app.copilots.ops_service import OpsCopilotService
    from app.copilots.todo_service import TodoCopilotService

    ops = OpsCopilotService()
    svc = TodoCopilotService(ops_service=ops)
    assert svc.ensure_ops_instance("inst_gap") is True  # 本来就在 ops mock 库里
    assert ops.get_instance("inst_gap").can_submit_decision is False  # 未被覆盖成 True


def test_ensure_ops_instance_returns_false_for_unknown_item_id() -> None:
    from app.copilots.ops_service import OpsCopilotService
    from app.copilots.todo_service import TodoCopilotService

    svc = TodoCopilotService(ops_service=OpsCopilotService())
    assert svc.ensure_ops_instance("not_a_real_item") is False


def test_ensure_ops_instance_returns_false_without_ops_service_injected() -> None:
    from app.copilots.todo_service import TodoCopilotService

    svc = TodoCopilotService(ops_service=None)
    assert svc.ensure_ops_instance("ap_expense_wang") is False


# ——— reset_item：演示重置，撤销修改回到最初模拟状态 ———

def test_reset_item_restores_original_worklist_values_after_confirmed_mutation() -> None:
    """桥接来的单据被 confirm 改过表单值之后，reset_item 应该照着 WorklistItem 的原始
    表单值重新桥接——不是"改过"的值，是最初模拟数据里的值。"""
    from app.copilots.ops_agent import OpsDecision
    from app.copilots.ops_service import OpsCopilotService
    from app.copilots.todo_service import TodoCopilotService
    from data.schema import ProcessDefinition

    class _StubAgent:
        def decide(self, context, user_message, *, ops_rules="", conversation_context="", can_submit_decision=False, org_data=None):  # noqa: ANN001
            return OpsDecision(reply="", category="data_fix", field_name="报销总额", field_value="1")

    def lookup(wdid: str) -> ProcessDefinition:
        assert wdid == "wfd_expense_v1"
        return ProcessDefinition.model_validate(
            {
                "meta": {"process_id": "EXPENSE-001", "process_name": "费用报销流程", "version": "V1.0.0",
                         "responsible_dept": "计划财务部", "description": "测试用",
                         "applicant_scope": "全员", "entry_point": "OA系统"},
                "form_fields": [
                    {"seq": 1, "field_name": "报销总额", "required_stages": ["all"], "visible_stages": ["all"],
                     "editable_stages": ["draft"], "component_type": "数字"},
                ],
                "flow_nodes": [
                    {"node_id": "dept_manager_approve", "node_name": "部门主管审批", "is_draft": False,
                     "handler": {"mode": "单选-单人处理", "source": "本部门", "role": "部门主管", "source_field": None},
                     "submit_paths": [{"path_name": "流程结束", "condition": "结论性意见=同意", "target_node_id": "END"}]},
                ],
                "attachments": None, "roles": None,
            }
        )

    ops = OpsCopilotService(agent=_StubAgent(), process_lookup=lookup)
    svc = TodoCopilotService(ops_service=ops)
    svc.ensure_ops_instance("ap_expense_wang")
    ops.propose("ap_expense_wang", "改成1元")
    ops.confirm("ap_expense_wang")
    assert ops.get_instance("ap_expense_wang").context.form_values["报销总额"] == 1  # 确认已改动

    assert svc.reset_item("ap_expense_wang") is True
    assert ops.get_instance("ap_expense_wang").context.form_values["报销总额"] == 9200  # 回到最初模拟值
    assert ops.has_pending("ap_expense_wang") is False


def test_reset_item_returns_false_for_unknown_item_id() -> None:
    from app.copilots.ops_service import OpsCopilotService
    from app.copilots.todo_service import TodoCopilotService

    svc = TodoCopilotService(ops_service=OpsCopilotService())
    assert svc.reset_item("not_a_real_item") is False


def test_reset_item_returns_false_without_ops_service_injected() -> None:
    from app.copilots.todo_service import TodoCopilotService

    svc = TodoCopilotService(ops_service=None)
    assert svc.reset_item("ap_expense_wang") is False


def test_reset_all_only_counts_items_that_actually_rebridge() -> None:
    """没注入 process_lookup 时，桥接来的条目（大多数）重置后没法重新现算 InstanceContext，
    只有本来就在 ops mock 库里的（inst_gap）能复位——reset_all 如实按实际复位数计数，
    不谎报"全部成功"。真实场景下（真的 process_lookup，见 API 层测试）能全部复位。"""
    from app.copilots.ops_service import OpsCopilotService
    from app.copilots.todo_service import TodoCopilotService

    svc = TodoCopilotService(ops_service=OpsCopilotService())  # 未注入 process_lookup
    assert svc.reset_all() == 1  # 只有 inst_gap（出厂 fixture）复位成功


def test_reset_all_returns_zero_without_ops_service_injected() -> None:
    from app.copilots.todo_service import TodoCopilotService

    svc = TodoCopilotService(ops_service=None)
    assert svc.reset_all() == 0


# ——— 待办清单助手（列表级导航/问答）———

class _StubTodoAgent:
    """按顺序吐预设 TodoAnswer，替代真实 LLM。记录看到的 worklist/message 供断言。"""

    def __init__(self, answers) -> None:  # noqa: ANN001
        self._answers = answers
        self.calls = []

    def answer(self, worklist, message, history=None):  # noqa: ANN001
        self.calls.append({"worklist": worklist, "message": message, "history": history})
        return self._answers[min(len(self.calls) - 1, len(self._answers) - 1)]


def test_ask_answers_list_level_question_without_target() -> None:
    from app.copilots.todo_service import TodoAnswer, TodoCopilotService

    agent = _StubTodoAgent([TodoAnswer(reply="有 2 单卡住：事假5天、病假9天。", target_instance_id=None)])
    svc = TodoCopilotService(agent=agent)
    r = svc.ask("我有几单卡住")
    assert r["reply"] == "有 2 单卡住：事假5天、病假9天。"
    assert "target_instance_id" not in r
    # agent 拿到的是确定性排好的 worklist（含 summary + 已排序 items）
    assert agent.calls[0]["worklist"]["summary"]["stuck"] >= 1


def test_ask_returns_valid_target_for_open_intent() -> None:
    from app.copilots.todo_service import TodoAnswer, TodoCopilotService

    agent = _StubTodoAgent([TodoAnswer(reply="这就为你打开差旅报销。", target_instance_id="ap_expense_wang")])
    svc = TodoCopilotService(agent=agent)
    r = svc.ask("帮我打开差旅报销那单")
    assert r["target_instance_id"] == "ap_expense_wang"
    assert r["target_title"]  # 带上标题供前端按钮展示


def test_ask_drops_hallucinated_target_not_in_worklist() -> None:
    """模型偶尔吐一个清单里不存在的 id（占位符/瞎猜）——不能直接信，要校验后丢弃，
    否则前端会给出一个点了打不开的跳转按钮。"""
    from app.copilots.todo_service import TodoAnswer, TodoCopilotService

    agent = _StubTodoAgent([TodoAnswer(reply="好的。", target_instance_id="not_a_real_item")])
    svc = TodoCopilotService(agent=agent)
    r = svc.ask("打开那单")
    assert "target_instance_id" not in r  # 无效 id 被丢弃


def test_ask_empty_message_returns_guidance_without_calling_agent() -> None:
    from app.copilots.todo_service import TodoCopilotService

    agent = _StubTodoAgent([])
    svc = TodoCopilotService(agent=agent)
    r = svc.ask("   ")
    assert "哪几单最急" in r["reply"]
    assert agent.calls == []  # 空输入不打 LLM
