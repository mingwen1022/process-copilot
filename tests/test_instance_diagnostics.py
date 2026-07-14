from __future__ import annotations

from app.copilots.diagnostics import build_instance_context, resolve_node_approver
from app.org_knowledge import load_org_seed
from data.schema import ProcessDefinition

APPLICANT = "u_it_app_staff"  # 真实 org_seed 里有部门/主管归属
UNKNOWN_APPLICANT = "u_does_not_exist"  # 无任何岗位归属 → 角色解析必为空


def _gapped_process() -> ProcessDefinition:
    """故意设计一个"区间漏洞"：只覆盖 <=3 天 和 >7 天，4-7 天没有任何路径覆盖——
    对应设计文档 3.5 的"设计缺口"场景。"""
    return ProcessDefinition.model_validate(
        {
            "meta": {"process_id": "T-GAP", "process_name": "测试_区间漏洞流程", "version": "V1.0.0",
                     "responsible_dept": "人力资源部", "description": "诊断单测用",
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
                     {"path_name": "直接结束", "condition": "请假天数≤3天", "target_node_id": "END"},
                     {"path_name": "送条线领导", "condition": "请假天数>7天", "target_node_id": "line_leader"},
                 ]},
                {"node_id": "line_leader", "node_name": "条线分管领导审批", "is_draft": False,
                 "handler": {"mode": "单选-单人处理", "source": "本条线", "role": "条线分管领导", "source_field": None},
                 "submit_paths": [{"path_name": "流程结束", "condition": None, "target_node_id": "END"}]},
            ],
            "attachments": None, "roles": None,
        }
    )


def test_blocked_paths_surfaces_the_gap_with_reason() -> None:
    org = load_org_seed()
    ctx = build_instance_context(
        instance_id="i1", process=_gapped_process(), current_node_id="mgr",
        form_values={"请假天数": 5}, org_data=org, initiator_user_id=APPLICANT,
    )
    assert ctx.has_any_matched_path() is False
    blocked = {p.path_name: p.reason for p in ctx.blocked_paths()}
    assert "直接结束" in blocked and "5" in blocked["直接结束"]
    assert "送条线领导" in blocked


def test_matched_path_means_not_stuck_even_if_other_paths_unmatched() -> None:
    # 2 天满足"直接结束"——有路可走就不算卡；blocked_paths 仍会列出其余条件为假的
    # 路径（如"送条线领导"），这只是信息性的，不代表"卡住"，卡不卡看 has_any_matched_path
    org = load_org_seed()
    ctx = build_instance_context(
        instance_id="i2", process=_gapped_process(), current_node_id="mgr",
        form_values={"请假天数": 2}, org_data=org, initiator_user_id=APPLICANT,
    )
    assert ctx.has_any_matched_path() is True
    matched_names = {p.path_name for p in ctx.path_evaluations if p.matched}
    assert matched_names == {"直接结束"}


def test_approver_resolution_present_for_known_applicant() -> None:
    org = load_org_seed()
    ctx = build_instance_context(
        instance_id="i3", process=_gapped_process(), current_node_id="mgr",
        form_values={"请假天数": 2}, org_data=org, initiator_user_id=APPLICANT,
    )
    assert ctx.approver_resolution is not None
    assert ctx.approver_resolution.is_empty is False
    assert ctx.approver_resolution.resolved_user_ids


def test_approver_resolution_empty_for_applicant_without_department() -> None:
    org = load_org_seed()
    ctx = build_instance_context(
        instance_id="i4", process=_gapped_process(), current_node_id="mgr",
        form_values={"请假天数": 2}, org_data=org, initiator_user_id=UNKNOWN_APPLICANT,
    )
    assert ctx.approver_resolution.is_empty is True
    assert "解析为空" in ctx.approver_resolution.reason


def test_draft_node_has_no_approver_resolution() -> None:
    org = load_org_seed()
    ctx = build_instance_context(
        instance_id="i5", process=_gapped_process(), current_node_id="draft",
        form_values={}, org_data=org, initiator_user_id=APPLICANT,
    )
    assert ctx.approver_resolution is None


def test_resolve_node_approver_diagnoses_target_before_jump() -> None:
    org = load_org_seed()
    ok = resolve_node_approver(
        _gapped_process(), "line_leader", org_data=org, initiator_user_id=APPLICANT, form_values={}
    )
    assert ok.is_empty is False

    empty = resolve_node_approver(
        _gapped_process(), "line_leader", org_data=org, initiator_user_id=UNKNOWN_APPLICANT, form_values={}
    )
    assert empty.is_empty is True
