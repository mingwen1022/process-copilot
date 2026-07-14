from __future__ import annotations

import pytest

from app.copilots.diagnostics import build_instance_context
from app.copilots.mock_instances import build_mock_instances
from app.copilots.ops_agent import FieldUpdate, OpsDecision, _system_prompt, _user_prompt, build_actions_from_decision
from app.copilots.ops_service import OpsCopilotService
from app.org_knowledge import load_org_seed
from app.tools.instance_actions import JumpToNode, ReassignApprover, UpdateApprovalHistoryOpinion, UpdateFieldValue
from data.schema import ProcessDefinition


class StubAgent:
    """按顺序吐预设决策，替代真实 LLM（沿用项目里注入 stub agent 的测试范式）。"""

    def __init__(self, decisions: list[OpsDecision]) -> None:
        self._decisions = decisions
        self.seen: list[str] = []
        self.seen_history: list[str] = []
        self.seen_can_submit_decision: list[bool] = []

    def decide(self, context, user_message, *, ops_rules="", conversation_context="", can_submit_decision=False, org_data=None):  # noqa: ANN001
        self.seen.append(user_message)
        self.seen_history.append(conversation_context)
        self.seen_can_submit_decision.append(can_submit_decision)
        return self._decisions[min(len(self.seen) - 1, len(self._decisions) - 1)]


class _StubIndex:
    """空检索——让 _retrieve_ops_rules 回退到内联规则，避免测试打 Bedrock 嵌入。"""

    def semantic_search(self, query, *, k=6):  # noqa: ANN001
        return []


def _service(decisions: list[OpsDecision]) -> tuple[OpsCopilotService, StubAgent]:
    agent = StubAgent(decisions)
    return OpsCopilotService(agent=agent, knowledge_index=_StubIndex()), agent


# ——— 枚举字段的 options 要出现在给运维副驾看的 prompt 里——真实复现过：用户问"印章类型
# 还有什么可选"，process 里 options 数据明明都在，却因为 prompt 只带了当前值、没带候选值
# 列表而答不出来。———

def _process_with_seal_type_field() -> ProcessDefinition:
    return ProcessDefinition.model_validate({
        "meta": {"process_id": "T-SEAL", "process_name": "测试_用印流程", "version": "V1.0.0",
                 "responsible_dept": "行政部", "description": "prompt 单测用",
                 "applicant_scope": "全员", "entry_point": "OA系统"},
        "form_fields": [
            {"seq": 1, "field_name": "印章类型", "required_stages": ["draft"], "visible_stages": ["all"],
             "editable_stages": ["draft"], "component_type": "下拉单选",
             "options": ["公章", "合同专用章", "法人章"]},
            {"seq": 2, "field_name": "用印事由", "required_stages": ["draft"], "visible_stages": ["all"],
             "editable_stages": ["draft"], "component_type": "多行文本"},
        ],
        "flow_nodes": [
            {"node_id": "draft", "node_name": "起草", "is_draft": True, "handler": None,
             "submit_paths": [{"path_name": "送法务", "condition": None, "target_node_id": "legal"}]},
            {"node_id": "legal", "node_name": "法务审核", "is_draft": False,
             "handler": {"mode": "单选-单人处理", "source": "本部门", "role": "法务专员", "source_field": None},
             "submit_paths": [{"path_name": "流程结束", "condition": None, "target_node_id": "END"}]},
        ],
        "attachments": None, "roles": None,
    })


def test_user_prompt_includes_enum_field_options() -> None:
    org = load_org_seed()
    ctx = build_instance_context(
        instance_id="i-seal", process=_process_with_seal_type_field(), current_node_id="draft",
        form_values={"印章类型": "公章", "用印事由": "签合同"}, org_data=org, initiator_user_id="u_it_app_staff",
    )
    prompt = _user_prompt(ctx, "印章类型还有什么可选", "", can_submit_decision=False, org_data=org)
    assert "可选值：['公章', '合同专用章', '法人章']" in prompt
    assert "用印事由" in prompt and "可选值" not in prompt.split("用印事由")[1].split("\n")[0]  # 非枚举字段不带可选值


def test_user_prompt_renders_approval_history_records() -> None:
    """history_fix 要能引用真实存在的历史 node_id——prompt 必须把已走完环节的意见记录
    （含解析出的中文姓名、结论、说明文字）带给 agent，不能让它凭空猜 node_id。"""
    org = load_org_seed()
    ctx = build_instance_context(
        instance_id="i-seal", process=_process_with_seal_type_field(), current_node_id="legal",
        form_values={"印章类型": "公章", "用印事由": "签合同"}, org_data=org, initiator_user_id="u_it_app_staff",
        history=[{"node_id": "draft", "node_name": "起草", "actor_user_id": "u_it_app_staff", "action": "提交"}],
    )
    prompt = _user_prompt(ctx, "把起草那步的意见改成不同意", "", can_submit_decision=False, org_data=org)
    assert "历史审批记录" in prompt
    assert "draft" in prompt and "王嘉树" in prompt


# ——— fixtures 本身 ———

def test_mock_instances_cover_normal_and_three_stuck_kinds() -> None:
    kinds = {m.stuck_kind for m in build_mock_instances() if m.status == "stuck"}
    assert kinds == {"design_gap", "org_gap", "data_error"}
    assert any(m.status == "flowing" for m in build_mock_instances())
    assert any(m.status == "completed" for m in build_mock_instances())


def test_diagnose_is_deterministic_and_flags_stuck() -> None:
    svc = OpsCopilotService(agent=StubAgent([]))
    gap = svc.diagnose("inst_gap")
    assert gap["stuck"] is True and gap["blocked_paths"]
    org = svc.diagnose("inst_org")
    assert org["approver_empty"] is True
    ok = svc.diagnose("inst_ok_1")
    assert ok["stuck"] is False


# ——— 决策 → 动作 映射 ———

def test_build_action_maps_and_coerces_number() -> None:
    ctx = next(m for m in build_mock_instances() if m.instance_id == "inst_data").context
    actions = build_actions_from_decision(
        OpsDecision(reply="", category="data_fix", field_name="请假天数", field_value="8"), ctx
    )
    assert len(actions) == 1 and isinstance(actions[0], UpdateFieldValue) and actions[0].value == 8  # "8" → 数字 8


def test_data_fix_supports_multiple_fields() -> None:
    ctx = next(m for m in build_mock_instances() if m.instance_id == "inst_ok_1").context
    actions = build_actions_from_decision(
        OpsDecision(reply="", category="data_fix", field_updates=[
            FieldUpdate(field_name="结束日期", field_value="2026-07-11"),
            FieldUpdate(field_name="请假天数", field_value="1"),
        ]), ctx
    )
    assert len(actions) == 2
    assert {a.field_name for a in actions} == {"结束日期", "请假天数"}
    days = next(a for a in actions if a.field_name == "请假天数")
    assert days.value == 1  # 数字字段仍 coerce


def test_escalate_builds_no_action() -> None:
    ctx = next(m for m in build_mock_instances() if m.instance_id == "inst_gap").context
    assert build_actions_from_decision(OpsDecision(reply="", category="escalate"), ctx) == []


def test_history_fix_maps_to_update_approval_history_opinion() -> None:
    ctx = next(m for m in build_mock_instances() if m.instance_id == "inst_data").context
    actions = build_actions_from_decision(
        OpsDecision(reply="", category="history_fix", history_node_id="mgr",
                    history_opinion="不同意", history_comment="订正：应为不同意"), ctx,
    )
    assert len(actions) == 1
    action = actions[0]
    assert isinstance(action, UpdateApprovalHistoryOpinion)
    assert action.node_id == "mgr" and action.opinion == "不同意" and action.comment == "订正：应为不同意"


def test_history_fix_without_node_id_builds_no_action() -> None:
    ctx = next(m for m in build_mock_instances() if m.instance_id == "inst_data").context
    assert build_actions_from_decision(OpsDecision(reply="", category="history_fix"), ctx) == []


# ——— 三类根因的端到端处理（propose → confirm）———

def test_data_error_fix_unblocks_without_override() -> None:
    """数据填错场景：确认改字段值后，原本卡住的实例应恢复可流转。"""
    svc, _ = _service([OpsDecision(reply="你天数填错了，应为8天，我帮你改。", category="data_fix",
                                   field_name="请假天数", field_value="8", rationale="用户确认实际为8天")])
    proposal = svc.propose("inst_data", "我其实请8天，填成6了")
    assert proposal.category == "data_fix"
    assert proposal.needs_confirmation is True
    assert svc.has_pending("inst_data")

    result = svc.confirm("inst_data")
    assert result.applied is True
    assert result.now_unblocked is True  # 改成8天 → 走"送条线分管领导"，不再卡
    assert svc.get_instance("inst_data").status == "flowing"


def test_propose_threads_conversation_history_to_agent() -> None:
    svc, agent = _service([OpsDecision(reply="x", category="no_issue")])
    svc.propose("inst_ok_1", "确认", history=[
        {"role": "user", "content": "结束日期填错了"},
        {"role": "assistant", "content": "要把结束日期改成7-11、请假天数改成1，确认吗？"},
    ])
    ctx = agent.seen_history[-1]
    assert "结束日期填错了" in ctx and "确认吗" in ctx  # 历史被渲染进 agent 上下文


def test_multi_field_data_fix_confirm_applies_all() -> None:
    """结束日期+请假天数一起改错的场景：一次 data_fix 多字段，确认后两个字段都改。"""
    svc, _ = _service([OpsDecision(reply="帮你把结束日期和请假天数一起改对。", category="data_fix",
                                   field_updates=[FieldUpdate(field_name="结束日期", field_value="2026-07-11"),
                                                  FieldUpdate(field_name="请假天数", field_value="1")])])
    proposal = svc.propose("inst_ok_1", "结束日期填错了应是7-11，我只请1天")
    assert proposal.route == "user_confirm"
    assert "结束日期" in proposal.pending_summary and "请假天数" in proposal.pending_summary  # 两处都在摘要里
    result = svc.confirm("inst_ok_1")
    assert result.applied is True
    fv = svc.get_instance("inst_ok_1").context.form_values
    assert fv["结束日期"] == "2026-07-11" and fv["请假天数"] == 1  # 两个字段都改了


def test_override_routes_to_authorization_not_user_confirm() -> None:
    """override 属高风险：不走发起人确认，而是生成授权工单转 owner；发起人这边无确认。"""
    svc, _ = _service([OpsDecision(reply="你这5天属于设计覆盖漏洞，按规则并入更高一档，跳到条线领导审批。",
                                   category="override_jump", target_node_id="line_leader",
                                   rationale="运维规则：落在覆盖漏洞的请假并入最近的更高一档路径")])
    proposal = svc.propose("inst_gap", "我实际就是要请5天，走不下去了")
    assert proposal.route == "authorization"
    assert proposal.needs_confirmation is False  # 发起人不能自己确认
    assert not svc.has_pending("inst_gap")
    assert proposal.authorization_id and "条线分管领导审批" in proposal.authorization_summary  # 摘要要用中文环节名，不是 node_id
    # 工单出现在授权台，状态 pending，但实例还没动
    items = svc.list_authorizations()["items"]
    assert len(items) == 1 and items[0]["status"] == "pending"
    assert items[0]["requested_by"] == "王嘉树"  # 发起人要显示中文姓名，不是 user_id
    assert svc.get_instance("inst_gap").context.current_node_id == "gm"  # 未执行


def test_authorization_approve_applies_override() -> None:
    svc, _ = _service([OpsDecision(reply="按规则跳到条线领导。", category="override_jump",
                                   target_node_id="line_leader", rationale="运维规则xxx")])
    proposal = svc.propose("inst_gap", "我要请5天")
    result = svc.authorize(proposal.authorization_id, approve=True, note="同意")
    assert result.applied is True
    assert svc.get_instance("inst_gap").context.current_node_id == "line_leader"
    assert svc.list_authorizations()["items"][0]["status"] == "approved"


def test_authorization_reject_does_not_apply() -> None:
    svc, _ = _service([OpsDecision(reply="按规则跳到条线领导。", category="override_jump",
                                   target_node_id="line_leader", rationale="x")])
    proposal = svc.propose("inst_gap", "我要请5天")
    result = svc.authorize(proposal.authorization_id, approve=False, note="不符合条件")
    assert result.applied is False
    assert svc.get_instance("inst_gap").context.current_node_id == "gm"  # 未动
    assert svc.list_authorizations()["items"][0]["status"] == "rejected"


def test_org_gap_reassign_routes_to_authorization() -> None:
    # u_it_line_leader 是 org_seed 里真实存在的用户——改派目标现在要过 user_exists 校验
    svc, _ = _service([OpsDecision(reply="该环节岗位空缺，按规则临时改派给同级负责人。",
                                   category="reassign", reassign_user_id="u_it_line_leader",
                                   rationale="运维规则：岗位空缺可临时改派同级")])
    assert svc.diagnose("inst_org")["approver_empty"] is True
    proposal = svc.propose("inst_org", "审批人是空的，没人能审")
    assert proposal.route == "authorization"
    assert "赵文杰" in proposal.authorization_summary  # 改派摘要要用中文姓名，不是 user_id
    result = svc.authorize(proposal.authorization_id, approve=True)
    assert result.applied is True
    assert svc.get_instance("inst_org").context.approver_resolution.resolved_user_ids == ["u_it_line_leader"]


def test_reassign_with_hallucinated_user_id_escalates_instead_of_authorization() -> None:
    """回归：模型给了一个组织里不存在的占位符/瞎猜 id（如 "<UNKNOWN>"）时，不该生成一张
    注定失败的授权工单去烦流程负责人，应该直接降级成升级人工——跟"没给 id"一视同仁。"""
    svc, _ = _service([OpsDecision(reply="按规则临时改派。", category="reassign",
                                   reassign_user_id="<UNKNOWN>", rationale="运维规则：岗位空缺可临时改派同级")])
    proposal = svc.propose("inst_org", "审批人是空的，没人能审")
    assert proposal.route == "terminal"
    assert proposal.category == "escalate"
    assert svc.list_authorizations()["items"] == []


def test_deny_is_terminal_no_action_no_authorization() -> None:
    """规则明令禁止：直接拒绝，不挂 pending、不生成授权工单、不改动。"""
    svc, _ = _service([OpsDecision(reply="跳过合规硬性环节按《合规办法》明令禁止，无法处理。",
                                   category="deny", rationale="合规办法第X条：合规审核环节不得跳过")])
    proposal = svc.propose("inst_gap", "帮我跳过合规审核直接结束")
    assert proposal.route == "terminal"
    assert proposal.category == "deny"
    assert not svc.has_pending("inst_gap")
    assert svc.list_authorizations()["items"] == []


def test_escalate_leaves_no_pending_and_does_not_mutate() -> None:
    svc, _ = _service([OpsDecision(reply="规则没覆盖你这情况，已为你升级人工。",
                                   category="escalate", rationale="规则未覆盖")])
    proposal = svc.propose("inst_gap", "我这情况很特殊")
    assert proposal.route == "terminal"
    assert svc.has_pending("inst_gap") is False
    assert svc.list_authorizations()["items"] == []
    with pytest.raises(RuntimeError):
        svc.confirm("inst_gap")


def test_discard_clears_pending_without_applying() -> None:
    svc, _ = _service([OpsDecision(reply="改成8天？", category="data_fix", field_name="请假天数", field_value="8")])
    svc.propose("inst_data", "帮我改天数")
    assert svc.has_pending("inst_data")
    before = svc.get_instance("inst_data").context.form_values["请假天数"]
    svc.discard("inst_data")
    assert svc.has_pending("inst_data") is False
    assert svc.get_instance("inst_data").context.form_values["请假天数"] == before  # 未改动


def test_invalid_override_target_is_rejected_at_apply() -> None:
    # agent 给了不存在的目标环节：即便授权人批准，apply 时确定性校验也拦下
    svc, _ = _service([OpsDecision(reply="跳过去", category="override_jump", target_node_id="ghost_node")])
    proposal = svc.propose("inst_gap", "跳到某个环节")
    result = svc.authorize(proposal.authorization_id, approve=True)
    assert result.applied is False
    assert any("不存在" in e for e in result.errors)


# ——— #2 在办任务体检 ———

def test_inspect_flags_missing_material_when_condition_matches() -> None:
    svc = OpsCopilotService(agent=StubAgent([]), knowledge_index=_StubIndex())
    r = svc.inspect("inst_ok_3")  # 病假10天、未传就诊证明
    assert r["clean"] is False
    assert any(m["attachment_type"] == "就诊证明" for m in r["material_findings"])


def test_inspect_clean_when_material_not_required() -> None:
    svc = OpsCopilotService(agent=StubAgent([]), knowledge_index=_StubIndex())
    r = svc.inspect("inst_ok_1")  # 年假2天，不需要就诊证明
    assert r["material_findings"] == []


def test_inspect_compliance_clean_on_wellformed_process() -> None:
    # mock 流程已补齐必填字段+总经理环节 → 设计层面合规体检应为空
    svc = OpsCopilotService(agent=StubAgent([]), knowledge_index=_StubIndex())
    assert svc.inspect("inst_ok_1")["compliance_findings"] == []


# ——— ensure_instance 桥接：待办队列里没有 InstanceContext 的审批项 ———

def _bridge_process() -> ProcessDefinition:
    return ProcessDefinition.model_validate(
        {
            "meta": {"process_id": "T-BRIDGE", "process_name": "测试_桥接流程", "version": "V1.0.0",
                     "responsible_dept": "计划财务部", "description": "ensure_instance 单测用",
                     "applicant_scope": "全员", "entry_point": "OA系统"},
            "form_fields": [
                {"seq": 1, "field_name": "报销总额", "required_stages": ["all"], "visible_stages": ["all"],
                 "editable_stages": ["draft"], "component_type": "数字"},
            ],
            "flow_nodes": [
                {"node_id": "draft", "node_name": "起草", "is_draft": True, "handler": None,
                 "submit_paths": [{"path_name": "送部门主管", "condition": None, "target_node_id": "mgr"}]},
                {"node_id": "mgr", "node_name": "部门主管审批", "is_draft": False,
                 "handler": {"mode": "单选-单人处理", "source": "本部门", "role": "部门主管", "source_field": None},
                 "submit_paths": [
                     {"path_name": "流程结束", "condition": "结论性意见=同意", "target_node_id": "END"},
                     {"path_name": "退回起草", "condition": "结论性意见=不同意", "target_node_id": "DRAFT"},
                 ]},
            ],
            "attachments": None, "roles": None,
        }
    )


def _service_with_lookup(decisions: list[OpsDecision]) -> tuple[OpsCopilotService, StubAgent]:
    agent = StubAgent(decisions)
    svc = OpsCopilotService(
        agent=agent, knowledge_index=_StubIndex(),
        process_lookup=lambda wdid: _bridge_process() if wdid == "wfd_bridge_v1" else (_ for _ in ()).throw(KeyError(wdid)),
    )
    return svc, agent


def test_ensure_instance_returns_existing_mock_instance_unchanged() -> None:
    svc, _ = _service_with_lookup([])
    inst = svc.ensure_instance("inst_ok_1")
    assert inst is not None and inst.instance_id == "inst_ok_1"


def test_ensure_instance_builds_context_for_unknown_id_with_hints() -> None:
    svc, _ = _service_with_lookup([])
    inst = svc.ensure_instance(
        "ap_expense_test", workflow_definition_id="wfd_bridge_v1", node_id="mgr",
        form_values={"报销总额": 8600}, title="差旅报销 · 测试", can_submit_decision=True,
    )
    assert inst is not None
    assert inst.context is not None and inst.context.current_node_id == "mgr"
    assert inst.can_submit_decision is True


def test_awaiting_decision_is_not_diagnosed_as_stuck() -> None:
    """回归：待审批环节还没填结论性意见时，所有条件路径都因"结论未给"而不匹配——
    这不是卡住，是在等处理人决策。诊断不能把它标成 stuck（否则详情页会冒出假的"诊断"块）。
    但同样没有匹配路径、给了同意也走不了的覆盖漏洞（inst_gap）仍应是 stuck。"""
    svc, _ = _service_with_lookup([])
    svc.ensure_instance(
        "ap_await", workflow_definition_id="wfd_bridge_v1", node_id="mgr",
        form_values={"报销总额": 8600}, can_submit_decision=True,  # 未给结论性意见
    )
    d = svc.diagnose("ap_await")
    assert d["stuck"] is False  # 结论=同意 时"流程结束"路径能走 → 只是在等决策
    assert svc.diagnose("inst_gap")["stuck"] is True  # 覆盖漏洞：给了同意也无路可走


def test_ensure_instance_is_idempotent() -> None:
    svc, _ = _service_with_lookup([])
    first = svc.ensure_instance(
        "ap_expense_test", workflow_definition_id="wfd_bridge_v1", node_id="mgr", form_values={"报销总额": 100},
    )
    second = svc.ensure_instance(
        "ap_expense_test", workflow_definition_id="wfd_bridge_v1", node_id="mgr", form_values={"报销总额": 999999},
    )
    assert first is second  # 第二次调用直接拿回第一次建好的，不会用新的 form_values 重建


def test_ensure_instance_returns_none_without_hints() -> None:
    svc, _ = _service_with_lookup([])
    assert svc.ensure_instance("unknown_item") is None


def test_ensure_instance_returns_none_when_node_not_found() -> None:
    svc, _ = _service_with_lookup([])
    assert svc.ensure_instance(
        "ap_x", workflow_definition_id="wfd_bridge_v1", node_id="not_a_node", form_values={},
    ) is None


def test_ensure_instance_returns_none_without_process_lookup_injected() -> None:
    svc, _ = _service(  # 没注入 process_lookup 的默认构造
        []
    )
    assert svc.ensure_instance(
        "ap_x", workflow_definition_id="wfd_bridge_v1", node_id="mgr", form_values={},
    ) is None


def test_bridged_instance_supports_submit_decision_end_to_end() -> None:
    """桥接出来的审批项也能走完整的 propose → confirm 闭环，真正路由到下一环节——
    这就是"办理"跟"排障"共用同一套 harness 的证明。"""
    svc, agent = _service_with_lookup(
        [OpsDecision(reply="材料齐全，建议同意。", category="submit_decision", opinion="同意", rationale="金额在权限内")]
    )
    svc.ensure_instance(
        "ap_expense_test", workflow_definition_id="wfd_bridge_v1", node_id="mgr",
        form_values={"报销总额": 8600}, can_submit_decision=True,
    )
    proposal = svc.propose("ap_expense_test", "同意，可以批了")
    assert agent.seen_can_submit_decision[-1] is True
    assert proposal.category == "submit_decision"
    assert proposal.route == "user_confirm"

    result = svc.confirm("ap_expense_test")
    assert result.applied is True
    inst = svc.get_instance("ap_expense_test")
    assert inst.context.current_node_id == "END"
    # 回归：走到 END 是办结，不是卡住——END 没有 submit_paths，不能套"无路可走=卡住"的公式，
    # 也不该再露出"批准/驳回"这类结论性操作（没有环节处理人这回事了）
    assert inst.status == "completed"
    assert inst.can_submit_decision is False
    assert svc.diagnose("ap_expense_test")["stuck"] is False


def test_bridged_instance_without_can_submit_decision_blocks_submit_decision_even_if_llm_gives_it() -> None:
    """确定性兜底：即便 LLM 犯规给了 submit_decision，can_submit_decision=False 时
    代码层也拒绝构造这个动作——不信任模型自己守规矩。"""
    svc, _ = _service_with_lookup(
        [OpsDecision(reply="我帮你批了。", category="submit_decision", opinion="同意")]
    )
    svc.ensure_instance(  # can_submit_decision 默认 False（比如发起人查看自己单子进度时被误路由到这里）
        "inst_viewer_only", workflow_definition_id="wfd_bridge_v1", node_id="mgr", form_values={"报销总额": 100},
    )
    proposal = svc.propose("inst_viewer_only", "帮我批了吧")
    assert proposal.route == "terminal"
    assert proposal.category == "escalate"  # 动作构造不出来，降级为升级人工


# ——— 演示重置：撤销修改，回到最初模拟状态 ———

def test_reset_instance_restores_core_fixture_after_mutation() -> None:
    """出厂 fixture（inst_data）被 data_fix 改过之后，reset 应该把字段值现算复原，
    状态也跟着回到最初的 stuck（不是复位后残留 confirm 之后的 flowing）。"""
    svc, _ = _service([OpsDecision(reply="", category="data_fix", field_name="请假天数", field_value="8")])
    original_days = svc.get_instance("inst_data").context.form_values["请假天数"]
    original_status = svc.get_instance("inst_data").status

    svc.propose("inst_data", "帮我改成8天")
    svc.confirm("inst_data")
    assert svc.get_instance("inst_data").context.form_values["请假天数"] != original_days
    assert svc.get_instance("inst_data").status != original_status

    assert svc.reset_instance("inst_data") is True
    restored = svc.get_instance("inst_data")
    assert restored.context.form_values["请假天数"] == original_days
    assert restored.status == original_status


def test_reset_instance_forgets_bridged_instance_without_original_snapshot() -> None:
    """桥接来的实例没有出厂快照——reset 直接摘除，之后 get_instance 应该找不到了
    （重新桥接是上层 TodoCopilotService.reset_item 的职责，ops 层只管"清零"）。"""
    svc, _ = _service_with_lookup([])
    svc.ensure_instance(
        "ap_expense_test", workflow_definition_id="wfd_bridge_v1", node_id="mgr", form_values={"报销总额": 8600},
    )
    assert svc.reset_instance("ap_expense_test") is True
    with pytest.raises(KeyError):
        svc.get_instance("ap_expense_test")


def test_reset_instance_clears_pending_action() -> None:
    """还没 confirm 的待确认动作，reset 时应该一并清掉，不留一个指向旧状态的残留提案。"""
    svc, _ = _service([OpsDecision(reply="", category="data_fix", field_name="请假天数", field_value="8")])
    svc.propose("inst_data", "帮我改成8天")
    assert svc.has_pending("inst_data") is True
    svc.reset_instance("inst_data")
    assert svc.has_pending("inst_data") is False


def test_reset_instance_returns_false_for_unknown_id() -> None:
    svc, _ = _service([])
    assert svc.reset_instance("not_a_real_instance") is False


# ——— 提示词内容：给用户看的文案不能泄漏内部 user_id / 排障动作不该被误判成"要处理人才能做" ———

def _ctx_for(instance_id: str):
    return next(m for m in build_mock_instances() if m.instance_id == instance_id).context


def test_user_prompt_resolves_approver_ids_to_chinese_names_when_org_data_given() -> None:
    """回归：审批人展示不能是裸的 user_id（如 u_it_app_supervisor），要换成中文姓名，
    不然模型很容易照抄这个 id 直接甩进给用户看的回复里。"""
    ctx = _ctx_for("inst_ok_1")
    org = load_org_seed()
    prompt = _user_prompt(ctx, "随便问问", "", can_submit_decision=True, org_data=org)
    assert "u_it_app_supervisor" not in prompt
    assert "李承宇" in prompt  # inst_ok_1 的 mgr 环节解析到李承宇


def test_user_prompt_falls_back_to_raw_id_without_org_data() -> None:
    """没给 org_data 时不做名字解析（调用方没传，说明这条路径本来就没有组织数据可查），
    退化成原样展示 id，不报错。"""
    ctx = _ctx_for("inst_ok_1")
    prompt = _user_prompt(ctx, "随便问问", "", can_submit_decision=True, org_data=None)
    assert "u_it_app_supervisor" in prompt


def test_user_prompt_marks_draft_node_for_override_jump_target_lookup() -> None:
    """override_jump 撤回到起草时，模型要能从"全部环节"列表里直接找到起草环节的真实
    node_id——列表必须标出哪个是起草环节，不能让模型只靠"起草"这两个字的节点名去猜。"""
    ctx = _ctx_for("inst_gap")
    prompt = _user_prompt(ctx, "随便问问", "", can_submit_decision=False)
    assert "draft | 起草（起草环节）" in prompt


def test_user_prompt_clarifies_non_processor_can_still_use_ops_actions() -> None:
    """回归：can_submit_decision=False 时的角色说明，必须明确排障类动作(data_fix/withdraw/
    override)不受"不是当前处理人"限制——之前措辞太笼统，模型把这条限制错误扩大到了所有
    动作，拒绝了发起人正常的撤回诉求（见真实复现：模型说"您不是处理人，无法退回起草"）。"""
    ctx = _ctx_for("inst_gap")
    prompt = _user_prompt(ctx, "随便问问", "", can_submit_decision=False)
    assert "data_fix" in prompt and "withdraw" in prompt and "override" in prompt
    assert "不受此限制" in prompt or "无关" in prompt


def test_system_prompt_routes_core_field_withdraw_to_authorization_at_leadership_node() -> None:
    """核心字段填错、且单据已到领导层级环节的撤回，提示词要明确指向 override_jump（转授权），
    不能停在"withdraw 就是自助确认"这种一刀切规则上。"""
    prompt = _system_prompt(can_submit_decision=False)
    assert "领导层级" in prompt
    assert "override_jump" in prompt
    assert "起草环节的真实" in prompt or "起草环节的真实 node_id" in prompt


def test_system_prompt_forbids_leaking_raw_user_id_in_reply() -> None:
    prompt = _system_prompt(can_submit_decision=False)
    assert "中文姓名" in prompt
    assert "user_id" in prompt  # 明确点名不能把这种内部编号写进回复
