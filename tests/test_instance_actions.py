from __future__ import annotations

from app.copilots.diagnostics import build_instance_context
from app.org_knowledge import load_org_seed
from app.tools.instance_actions import (
    AnswerOnly,
    JumpToNode,
    ReassignApprover,
    SkipCurrentNode,
    SubmitDecision,
    UpdateApprovalHistoryOpinion,
    UpdateFieldValue,
    Withdraw,
    apply_instance_action,
)
from data.schema import ProcessDefinition
from tests.test_instance_diagnostics import APPLICANT, UNKNOWN_APPLICANT, _gapped_process


def _stuck_context(days: int = 5):
    org = load_org_seed()
    return build_instance_context(
        instance_id="i1", process=_gapped_process(), current_node_id="mgr",
        form_values={"请假天数": days}, org_data=org, initiator_user_id=APPLICANT,
    ), org


def test_update_field_value_fixes_the_gap_without_any_override() -> None:
    """数据问题场景（运维特权路径）：请假天数从卡住的 5 改成 8（不需要 jump/skip，
    路径自己就通了）。运维能在非 draft 环节改数据，普通用户不能——见下一个测试。"""
    ctx, org = _stuck_context(days=5)
    assert ctx.has_any_matched_path() is False

    result = apply_instance_action(
        ctx, UpdateFieldValue(field_name="请假天数", value=8), org_data=org, allow_stage_override=True
    )
    assert result.applied is True
    assert result.errors == []
    assert result.context.current_node_id == "mgr"  # 没有跳转，还在同一环节
    assert result.context.has_any_matched_path() is True  # 但现在通了


def test_update_field_value_rejects_unknown_field() -> None:
    ctx, org = _stuck_context()
    result = apply_instance_action(ctx, UpdateFieldValue(field_name="不存在的字段", value=1), org_data=org)
    assert result.applied is False
    assert "不存在" in result.errors[0]


def test_update_field_value_rejects_when_not_editable_at_current_stage() -> None:
    # 默认（allow_stage_override=False，普通用户权限）：请假天数 editable_stages=['draft']，
    # 当前在 mgr 环节，不可编辑——跟上面运维特权路径的行为形成对照
    ctx, org = _stuck_context()
    result = apply_instance_action(ctx, UpdateFieldValue(field_name="请假天数", value=8), org_data=org)
    assert result.applied is False
    assert "不可编辑" in result.errors[0]


# ——— 枚举字段（下拉单选/多选）改值必须校验在 options 内——真实场景：运维副驾现在
# 能看到字段的 options 了（之前 prompt 里没带，答不出"还有什么可选"），可能会直接提议
# 改成某个选项，但那是 LLM 给的候选值，不能不校验就信，得确定性核对。———

def _process_with_seal_type_field() -> ProcessDefinition:
    return ProcessDefinition.model_validate({
        "meta": {"process_id": "T-SEAL", "process_name": "测试_用印流程", "version": "V1.0.0",
                 "responsible_dept": "行政部", "description": "枚举字段校验单测用",
                 "applicant_scope": "全员", "entry_point": "OA系统"},
        "form_fields": [
            {"seq": 1, "field_name": "印章类型", "required_stages": ["draft"], "visible_stages": ["all"],
             "editable_stages": ["draft"], "component_type": "下拉单选",
             "options": ["公章", "合同专用章", "法人章"]},
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


def _seal_context(value: str = "公章"):
    org = load_org_seed()
    return build_instance_context(
        instance_id="i-seal", process=_process_with_seal_type_field(), current_node_id="draft",
        form_values={"印章类型": value}, org_data=org, initiator_user_id=APPLICANT,
    ), org


def test_update_field_value_rejects_value_not_in_options() -> None:
    """运维/副驾编了一个不存在的选项——必须确定性拒绝，不能悄悄写进数据。"""
    ctx, org = _seal_context()
    result = apply_instance_action(
        ctx, UpdateFieldValue(field_name="印章类型", value="萝卜章"), org_data=org, allow_stage_override=True
    )
    assert result.applied is False
    assert "萝卜章" in result.errors[0] and "可选值" in result.errors[0]


def test_update_field_value_accepts_value_in_options() -> None:
    """改成真实存在的选项——正常放行。"""
    ctx, org = _seal_context()
    result = apply_instance_action(
        ctx, UpdateFieldValue(field_name="印章类型", value="法人章"), org_data=org, allow_stage_override=True
    )
    assert result.applied is True
    assert result.context.form_values["印章类型"] == "法人章"


def test_update_field_value_non_enum_field_skips_options_check() -> None:
    """没有 options 的普通字段（如数字/文本）不受这条校验限制——只对枚举类字段生效。"""
    ctx, org = _stuck_context(days=5)
    result = apply_instance_action(
        ctx, UpdateFieldValue(field_name="请假天数", value=8), org_data=org, allow_stage_override=True
    )
    assert result.applied is True


def test_jump_to_node_moves_and_recomputes_diagnosis() -> None:
    ctx, org = _stuck_context(days=8)  # 8 天其实满足"送条线领导"，用它来验证跳转后的现算
    result = apply_instance_action(ctx, JumpToNode(target_node_id="line_leader"), org_data=org)
    assert result.applied is True
    assert result.context.current_node_id == "line_leader"
    assert result.context.approver_resolution is not None


def test_jump_to_node_rejects_unknown_target() -> None:
    ctx, org = _stuck_context()
    result = apply_instance_action(ctx, JumpToNode(target_node_id="not_a_node"), org_data=org)
    assert result.applied is False
    assert "不存在" in result.errors[0]


def test_jump_to_node_supports_end() -> None:
    ctx, org = _stuck_context()
    result = apply_instance_action(ctx, JumpToNode(target_node_id="END"), org_data=org)
    assert result.applied is True
    assert result.context.current_node_id == "END"
    assert result.context.approver_resolution is None


def test_skip_current_node_rejects_when_multiple_forward_targets() -> None:
    # mgr 有两条不同去向（END / line_leader），skip 语义不明确，应拒绝并建议用 jump
    ctx, org = _stuck_context()
    result = apply_instance_action(ctx, SkipCurrentNode(), org_data=org)
    assert result.applied is False
    assert "jump_to_node" in result.errors[0]


def test_skip_current_node_succeeds_with_single_forward_target() -> None:
    org = load_org_seed()
    ctx = build_instance_context(
        instance_id="i1", process=_gapped_process(), current_node_id="line_leader",
        form_values={}, org_data=org, initiator_user_id=APPLICANT,
    )
    result = apply_instance_action(ctx, SkipCurrentNode(), org_data=org)
    assert result.applied is True
    assert result.context.current_node_id == "END"


def test_withdraw_returns_to_the_real_draft_node_id() -> None:
    # 撤回要回到真实的起草环节 node_id（"draft"），不是 submit_paths 里那个 "DRAFT" 哨兵值
    ctx, org = _stuck_context()
    result = apply_instance_action(ctx, Withdraw(), org_data=org)
    assert result.applied is True
    assert result.context.current_node_id == "draft"


def test_reassign_approver_fills_the_org_gap() -> None:
    org = load_org_seed()
    ctx = build_instance_context(
        instance_id="i1", process=_gapped_process(), current_node_id="mgr",
        form_values={"请假天数": 2}, org_data=org, initiator_user_id=UNKNOWN_APPLICANT,
    )
    assert ctx.approver_resolution.is_empty is True

    # 改派目标必须是组织里真实存在的用户（u_it_line_leader 是 org_seed 里的真实 id）——
    # 回归：曾经任意非空字符串都会被接受，静默把审批人改派成不存在的人
    result = apply_instance_action(ctx, ReassignApprover(user_id="u_it_line_leader"), org_data=org)
    assert result.applied is True
    assert result.context.approver_resolution.is_empty is False
    assert result.context.approver_resolution.resolved_user_ids == ["u_it_line_leader"]


def test_reassign_approver_rejects_empty_user_id() -> None:
    ctx, org = _stuck_context()
    result = apply_instance_action(ctx, ReassignApprover(user_id="  "), org_data=org)
    assert result.applied is False


def test_reassign_approver_rejects_user_not_in_org() -> None:
    """回归：LLM 结构化输出偶尔会给一个组织里不存在的占位符/瞎猜 id（如 "<UNKNOWN>"）
    而不是真的留空——不校验就会把审批人静默改派成一个不存在的人。"""
    org = load_org_seed()
    ctx = build_instance_context(
        instance_id="i1", process=_gapped_process(), current_node_id="mgr",
        form_values={"请假天数": 2}, org_data=org, initiator_user_id=UNKNOWN_APPLICANT,
    )
    result = apply_instance_action(ctx, ReassignApprover(user_id="<UNKNOWN>"), org_data=org)
    assert result.applied is False
    assert "<UNKNOWN>" in result.errors[0]


def test_answer_only_never_mutates_context() -> None:
    ctx, org = _stuck_context()
    result = apply_instance_action(ctx, AnswerOnly(text="按规则你这个情况需要走条线领导审批。"), org_data=org)
    assert result.applied is True
    assert result.context == ctx


# ——— UpdateApprovalHistoryOpinion（订正已走完环节的历史意见记录，不重新触发路由）———

def _context_with_history():
    from data.schema import OpinionConfig

    org = load_org_seed()
    process = _gapped_process()
    mgr = process.get_node_by_id("mgr")
    mgr.opinion = OpinionConfig(conclusive_required=True, detail_required=False, conclusive_options=["同意", "不同意"])
    mgr.opinion_label = "部门主管意见"
    ctx = build_instance_context(
        instance_id="i1", process=process, current_node_id="line_leader",
        form_values={"请假天数": 8}, org_data=org, initiator_user_id=APPLICANT,
        history=[{"node_id": "mgr", "node_name": "部门主管审批", "actor_user_id": APPLICANT,
                  "action": "同意", "opinion": "同意", "comment": "无异议"}],
    )
    return ctx, org


def test_update_approval_history_opinion_corrects_existing_entry() -> None:
    ctx, org = _context_with_history()
    result = apply_instance_action(
        ctx, UpdateApprovalHistoryOpinion(node_id="mgr", opinion="不同意", comment="材料不齐，应驳回"), org_data=org,
    )
    assert result.applied is True
    assert result.errors == []
    entry = result.context.history[0]
    assert entry.opinion == "不同意"
    assert entry.comment == "材料不齐，应驳回"
    assert result.context.current_node_id == "line_leader"  # 不重新触发路由，实例位置不变


def test_update_approval_history_opinion_comment_only_keeps_opinion() -> None:
    ctx, org = _context_with_history()
    result = apply_instance_action(ctx, UpdateApprovalHistoryOpinion(node_id="mgr", comment="补充说明"), org_data=org)
    assert result.applied is True
    entry = result.context.history[0]
    assert entry.opinion == "同意"  # 未指定结论，保持原值
    assert entry.comment == "补充说明"


def test_update_approval_history_opinion_rejects_invalid_opinion() -> None:
    ctx, org = _context_with_history()
    result = apply_instance_action(ctx, UpdateApprovalHistoryOpinion(node_id="mgr", opinion="弃权"), org_data=org)
    assert result.applied is False
    assert "弃权" in result.errors[0]
    assert ctx.history[0].opinion == "同意"  # 拒绝时原记录不受影响


def test_update_approval_history_opinion_rejects_unknown_node() -> None:
    ctx, org = _context_with_history()
    result = apply_instance_action(ctx, UpdateApprovalHistoryOpinion(node_id="line_leader", opinion="同意"), org_data=org)
    assert result.applied is False
    assert "line_leader" in result.errors[0]


def test_update_approval_history_opinion_rejects_when_nothing_specified() -> None:
    ctx, org = _context_with_history()
    result = apply_instance_action(ctx, UpdateApprovalHistoryOpinion(node_id="mgr"), org_data=org)
    assert result.applied is False


# ——— SubmitDecision（办理：结论性意见 → 确定性路径求值，不是 override）———

def _opinion_process() -> ProcessDefinition:
    """跟真实项目约定一致：意见条件写"结论性意见=同意/不同意"，同意侧另按天数分岔。"""
    return ProcessDefinition.model_validate(
        {
            "meta": {"process_id": "T-OPN", "process_name": "测试_意见路由流程", "version": "V1.0.0",
                     "responsible_dept": "人力资源部", "description": "SubmitDecision 单测用",
                     "applicant_scope": "全员", "entry_point": "OA系统"},
            "form_fields": [
                {"seq": 1, "field_name": "请假天数", "required_stages": ["all"], "visible_stages": ["all"],
                 "editable_stages": ["draft"], "component_type": "数字"},
            ],
            "flow_nodes": [
                {"node_id": "draft", "node_name": "起草", "is_draft": True, "handler": None,
                 "submit_paths": [{"path_name": "送部门主管", "condition": None, "target_node_id": "mgr"}]},
                {"node_id": "mgr", "node_name": "部门主管审批", "is_draft": False,
                 "handler": {"mode": "单选-单人处理", "source": "本部门", "role": "部门主管", "source_field": None},
                 "submit_paths": [
                     {"path_name": "直接结束", "condition": "结论性意见=同意 且 请假天数≤3天", "target_node_id": "END"},
                     {"path_name": "送条线领导", "condition": "结论性意见=同意 且 请假天数>3天", "target_node_id": "line_leader"},
                     {"path_name": "退回起草", "condition": "结论性意见=不同意", "target_node_id": "DRAFT"},
                 ]},
                {"node_id": "line_leader", "node_name": "条线分管领导审批", "is_draft": False,
                 "handler": {"mode": "单选-单人处理", "source": "本条线", "role": "条线分管领导", "source_field": None},
                 "submit_paths": [{"path_name": "流程结束", "condition": "结论性意见=同意", "target_node_id": "END"}]},
            ],
            "attachments": None, "roles": None,
        }
    )


def _opinion_context(days: int = 2, node: str = "mgr"):
    org = load_org_seed()
    return build_instance_context(
        instance_id="i1", process=_opinion_process(), current_node_id=node,
        form_values={"请假天数": days}, org_data=org, initiator_user_id=APPLICANT,
    ), org


def test_submit_decision_agree_routes_to_matched_path() -> None:
    ctx, org = _opinion_context(days=2)  # ≤3 天 + 同意 → 直接结束
    result = apply_instance_action(ctx, SubmitDecision(opinion="同意"), org_data=org)
    assert result.applied is True
    assert result.context.current_node_id == "END"


def test_submit_decision_agree_branches_by_actual_form_value() -> None:
    ctx, org = _opinion_context(days=5)  # >3 天 + 同意 → 送条线领导（不是 END）
    result = apply_instance_action(ctx, SubmitDecision(opinion="同意"), org_data=org)
    assert result.applied is True
    assert result.context.current_node_id == "line_leader"


def test_submit_decision_disagree_returns_to_real_draft_node_id() -> None:
    ctx, org = _opinion_context(days=2)
    result = apply_instance_action(ctx, SubmitDecision(opinion="不同意", comment="材料不全，请补充"), org_data=org)
    assert result.applied is True
    assert result.context.current_node_id == "draft"  # DRAFT 哨兵值解析成真实 node_id


def test_submit_decision_rejects_on_draft_node() -> None:
    ctx, org = _opinion_context(node="draft")
    result = apply_instance_action(ctx, SubmitDecision(opinion="同意"), org_data=org)
    assert result.applied is False
    assert "起草环节" in result.errors[0]


def test_submit_decision_does_not_carry_opinion_into_next_node_form_values() -> None:
    """回归：上一环节的结论性意见不该污染下一环节——不然下一环节的 path_evaluations
    会误以为"已经有结论"，实际是下一个处理人自己还没给意见。"""
    ctx, org = _opinion_context(days=5)
    result = apply_instance_action(ctx, SubmitDecision(opinion="同意"), org_data=org)
    assert result.context.current_node_id == "line_leader"
    assert "结论性意见" not in result.context.form_values
    # line_leader 的路径条件也是"结论性意见=同意"，没有结论性意见值 → 不该被误判为已匹配
    assert result.context.has_any_matched_path() is False


def test_submit_decision_records_comment_without_affecting_routing() -> None:
    ctx, org = _opinion_context(days=2)
    result = apply_instance_action(ctx, SubmitDecision(opinion="同意", comment="材料齐全，同意放行"), org_data=org)
    assert result.applied is True
    assert result.context.form_values.get("mgr_审批意见") == "材料齐全，同意放行"
