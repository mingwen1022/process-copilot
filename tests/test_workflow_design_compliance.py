from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from app.api.slice1_service import LEAVE_CASE_DIR, Slice1Service
from app.api.workflow_design_service import WorkflowDesignService
from app.io_utils import find_standard_json, load_process_definition

CREATED_BY = "u_it_app_staff"


def _leave_graph_initializer(**_kwargs: Any) -> dict[str, Any]:
    process = load_process_definition(find_standard_json(LEAVE_CASE_DIR))
    return {
        "candidate_process": process,
        "workflow_design_output": {"draft_version": process.meta.version, "process_definition": process.model_dump(mode="json")},
        "designer_assistant_message": {"role": "assistant", "message_type": "initialization_result", "title": "草稿", "content": "已生成请假草稿。", "summary_bullets": [], "clarification_cards": [], "next_actions": []},
        "user_clarification_requests": [],
        "design_persistence_report": {"persisted": False, "draft_saved": True},
        "output_paths": {},
        "schema_validation_report": {"valid": True, "summary": {"schema_error_count": 0}},
        "business_validation_result": {"passed": True, "blocking_issues": [], "warning_issues": [], "raw_missing_items": [], "user_clarification_requests": [], "summary": "pass"},
    }


class _StubJudge:
    def __init__(self, verdicts):
        from app.agents.compliance_judge_agent import RuleVerdict
        self._verdicts = [RuleVerdict.model_validate(v) for v in verdicts]
        self.structured_model = object()

    def judge(self, process, rules):
        valid = {r.rule_id for r in rules}
        return [v for v in self._verdicts if v.rule_id in valid]


@pytest.fixture()
def leave_session(tmp_path: Path):
    db_path = tmp_path / "runtime.db"
    Slice1Service(db_path)
    service = WorkflowDesignService(db_path, graph_initializer=_leave_graph_initializer)
    payload = service.initialize_new_session(
        created_by=CREATED_BY, workflow_type="approval", workflow_name="员工请假申请流程",
        category="审批流程", instruction="员工请假申请流程", sources=[], uploaded_files=[],
    )
    return service, payload["session"]["session_id"]


def test_compliance_report_flags_real_finding_with_citation(leave_session) -> None:
    service, session_id = leave_session
    judge = _StubJudge([{"rule_id": "design.node_naming_role_action", "violations": []}])
    report = service.compliance_report(session_id=session_id, judge=judge)

    assert report["process_name"] == "员工请假申请流程"
    assert report["applicable_rule_count"] >= 5
    det_ids = {f["rule_id"] for f in report["deterministic_findings"]}
    assert "leave.sick_leave_certificate" in det_ids  # 请假 gold 附件没设 required_condition
    finding = next(f for f in report["deterministic_findings"] if f["rule_id"] == "leave.sick_leave_certificate")
    assert finding["source_doc"] == "leave_management_policy" and finding["clause"]  # 引用到子句
    # 定性规则被判合规 → 不进违规
    assert not any(f["rule_id"] == "design.node_naming_role_action" for f in report["qualitative_findings"])


def test_compliance_report_surfaces_qualitative_violation(leave_session) -> None:
    service, session_id = leave_session
    judge = _StubJudge([{
        "rule_id": "design.node_naming_role_action",
        "violations": [{"node_id": "gm", "detail": "环节命名不规范"}],
    }])
    report = service.compliance_report(session_id=session_id, judge=judge)
    assert any(f["rule_id"] == "design.node_naming_role_action" for f in report["qualitative_findings"])
