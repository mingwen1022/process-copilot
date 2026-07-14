from __future__ import annotations

import pytest

pytestmark = pytest.mark.skip(
    reason="重运行时已冻结(见 doc/项目重定位与执行规划.md §5)；待用 EOA140 重做运行时夹具后再启用"
)

import time

from fastapi.testclient import TestClient

from app.api.runtime_service import RuntimeService
from app.api.server import create_app
from app.api.slice1_service import LEAVE_CASE_DIR, LEAVE_WORKFLOW_DEFINITION_ID, Slice1Service
from app.api.workflow_design_service import WorkflowDesignService
from app.io_utils import find_standard_json, load_process_definition
from data.schema import ProcessDefinition


APPLICANT = "u_it_app_staff"


def _client(tmp_path):
    db_path = tmp_path / "runtime.db"
    runtime = RuntimeService(db_path)
    slice1 = Slice1Service(db_path)
    workflow_design = WorkflowDesignService(db_path, graph_initializer=_fake_graph_initializer)
    return TestClient(create_app(runtime, slice1, workflow_design))


def _fake_graph_initializer(**_kwargs) -> dict:
    process = load_process_definition(find_standard_json(LEAVE_CASE_DIR))
    process_payload = process.model_dump(mode="json")
    workflow_design_output = {
        "status": "draft_ready",
        "session_id": "fake_session",
        "workflow_definition_id": "fake_definition",
        "draft_version": process.meta.version,
        "process_definition": process_payload,
        "design_summary": {
            "field_count": len(process.form_fields),
            "node_count": len(process.flow_nodes),
            "path_count": sum(len(node.submit_paths) for node in process.flow_nodes),
            "role_count": len(process.roles or []),
            "attachment_count": len(process.attachments or []),
            "source_count": 1,
            "blocking_issue_count": 0,
            "warning_issue_count": 0,
            "clarification_count": 0,
        },
        "validation": {
            "schema_valid": True,
            "business_passed": True,
            "blocking_issues": [],
            "warning_issues": [],
            "raw_missing_items": [],
        },
        "user_clarification_requests": [],
    }
    assistant_message = {
        "role": "assistant",
        "message_type": "initialization_result",
        "title": "已基于 source 生成员工请假申请草稿",
        "content": "已基于 source 生成员工请假申请草稿。",
        "summary_bullets": [],
        "clarification_cards": [],
        "next_actions": [],
    }
    return {
        "candidate_process": process,
        "workflow_design_output": workflow_design_output,
        "designer_assistant_message": assistant_message,
        "user_clarification_requests": [],
        "design_persistence_report": {"persisted": False, "draft_saved": True},
        "output_paths": {},
        "schema_validation_report": {"valid": True, "summary": {"schema_error_count": 0}},
        "business_validation_result": {
            "passed": True,
            "blocking_issues": [],
            "warning_issues": [],
            "raw_missing_items": [],
            "user_clarification_requests": [],
            "summary": "pass",
        },
    }


def _create_session(client: TestClient) -> dict:
    response = client.post(
        "/api/v1/workflow-design/sessions",
        json={
            "workflow_definition_id": LEAVE_WORKFLOW_DEFINITION_ID,
            "created_by": APPLICANT,
            "mode": "revise",
        },
    )
    assert response.status_code == 200
    return response.json()


def test_create_design_session_loads_leave_draft_and_raw_sources(tmp_path) -> None:
    client = _client(tmp_path)

    payload = _create_session(client)

    assert payload["workflow"]["name"] == "员工请假申请"
    assert payload["session"]["draft_version"] == "V1.1.0-draft"
    assert len(payload["draft_definition"]["form_fields"]) == 10
    assert len(payload["draft_definition"]["flow_nodes"]) == 4
    assert len(payload["sources"]) == 5
    assert payload["validation_report"]["issue_count"] == 4


def test_design_session_does_not_change_published_workflow_definition(tmp_path) -> None:
    client = _client(tmp_path)
    before = client.get(f"/api/v1/workflow-definitions/{LEAVE_WORKFLOW_DEFINITION_ID}").json()
    session = _create_session(client)

    generated = client.post(f"/api/v1/workflow-design/sessions/{session['session']['session_id']}/generate-draft")
    after = client.get(f"/api/v1/workflow-definitions/{LEAVE_WORKFLOW_DEFINITION_ID}").json()

    assert generated.status_code == 200
    assert before["definition"]["meta"]["version"] == "V1.0.0"
    assert after["definition"]["meta"]["version"] == "V1.0.0"
    assert generated.json()["draft_definition"]["meta"]["version"] == "V1.1.0-draft"


def test_add_source_and_message_then_generate_draft_persists_state(tmp_path) -> None:
    client = _client(tmp_path)
    session = _create_session(client)
    session_id = session["session"]["session_id"]

    source_response = client.post(
        f"/api/v1/workflow-design/sessions/{session_id}/sources",
        json={
            "title": "处理期限补充",
            "content": "请为所有审批环节配置次日处理期限。",
            "created_by": APPLICANT,
            "source_type": "text",
        },
    )
    message_response = client.post(
        f"/api/v1/workflow-design/sessions/{session_id}/messages",
        json={"role": "user", "content": "处理期限按 1 天配置。"},
    )
    generated = client.post(f"/api/v1/workflow-design/sessions/{session_id}/generate-draft")

    assert source_response.status_code == 200
    assert message_response.status_code == 200
    payload = generated.json()
    assert generated.status_code == 200
    assert len(payload["sources"]) == 6
    assert any(message["role"] == "user" and "处理期限" in message["content"] for message in payload["messages"])
    approval_nodes = [node for node in payload["draft_definition"]["flow_nodes"] if not node["is_draft"]]
    assert {node["time_limit_days"] for node in approval_nodes} == {1}
    assert payload["validation_report"]["warning_count"] == 1


def test_delete_source_removes_it_from_design_session(tmp_path) -> None:
    client = _client(tmp_path)
    session = _create_session(client)
    session_id = session["session"]["session_id"]

    source_response = client.post(
        f"/api/v1/workflow-design/sessions/{session_id}/sources",
        json={
            "title": "临时 source",
            "content": "这是一段可以删除的补充材料。",
            "created_by": APPLICANT,
            "source_type": "text",
        },
    )
    source_id = source_response.json()["sources"][-1]["source_id"]
    delete_response = client.delete(f"/api/v1/workflow-design/sessions/{session_id}/sources/{source_id}")

    assert source_response.status_code == 200
    assert delete_response.status_code == 200
    assert len(delete_response.json()["sources"]) == 5
    assert all(source["source_id"] != source_id for source in delete_response.json()["sources"])


def test_save_design_session_keeps_draft_available_from_management(tmp_path) -> None:
    client = _client(tmp_path)
    session = _create_session(client)
    session_id = session["session"]["session_id"]

    save_response = client.post(f"/api/v1/workflow-design/sessions/{session_id}/save")
    management = client.get("/api/v1/workflow-definitions", params={"user_id": APPLICANT}).json()["items"][0]

    assert save_response.status_code == 200
    assert save_response.json()["session"]["session_id"] == session_id
    assert management["management"]["draft_state"] == "AI 草稿"
    assert management["management"]["draft_version"] == "V1.1.0-draft"
    assert management["management"]["updated_at"] == save_response.json()["session"]["updated_at"]


def test_initialize_design_session_from_text_only_creates_draft(tmp_path) -> None:
    client = _client(tmp_path)

    response = client.post(
        "/api/v1/workflow-design/initialize",
        json={
            "created_by": APPLICANT,
            "workflow_type": "approval",
            "workflow_name": "员工请假申请",
            "category": "人事行政",
            "instruction": "员工请假申请，需要填写请假类型、开始日期、结束日期和请假原因，部门主管和部门领导审批。",
            "sources": [],
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["session"]["mode"] == "initialize"
    assert payload["session"]["base_version"] == "NEW"
    assert payload["draft_definition"]["meta"]["process_name"] == "员工请假申请"
    assert payload["draft_definition"]["meta"]["version"] == "V0.1.0-draft"
    assert len(payload["sources"]) == 1
    assert payload["sources"][0]["title"] == "00_用户初始化说明.txt"
    assert payload["workflow"]["status"] == "DRAFT"
    assert payload["generation_report"]["provider"] == "langgraph"

    management = client.get("/api/v1/workflow-definitions", params={"user_id": APPLICANT}).json()["items"]
    draft_cards = [item for item in management if item["workflow_definition_id"] == payload["workflow"]["workflow_definition_id"]]
    assert len(draft_cards) == 1
    assert draft_cards[0]["status"] == "DRAFT"
    assert draft_cards[0]["management"]["publish_state"] == "未发布草稿"
    assert draft_cards[0]["management"]["draft_state"] == "AI 草稿"
    assert draft_cards[0]["management"]["design_session_id"] == payload["session"]["session_id"]


def test_initialize_design_session_from_sources_and_instruction(tmp_path) -> None:
    client = _client(tmp_path)

    response = client.post(
        "/api/v1/workflow-design/initialize",
        json={
            "created_by": APPLICANT,
            "workflow_type": "approval",
            "workflow_name": "费用审批流程",
            "category": "财务费用",
            "instruction": "需要按金额区分审批。",
            "sources": [
                {
                    "title": "费用管理办法.txt",
                    "content": "员工提交费用申请，部门主管审批，通过后流程结束，不同意退回起草。",
                    "source_type": "txt",
                }
            ],
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["draft_definition"]["meta"]["process_name"] == "费用审批流程"
    assert len(payload["sources"]) == 2
    assert len(payload["draft_definition"]["form_fields"]) >= 5
    assert any(message["payload"].get("event") == "draft_initialized" for message in payload["messages"])
    assert "workflow_design_output" in payload["generation_report"]["langgraph"]
    assert "artifact_generation" not in payload["generation_report"]["langgraph"]
    assert any(
        message["payload"].get("assistant_message", {}).get("message_type") == "initialization_result"
        for message in payload["messages"]
    )


def test_delete_new_workflow_draft_removes_definition_and_session(tmp_path) -> None:
    client = _client(tmp_path)
    created = client.post(
        "/api/v1/workflow-design/initialize",
        json={
            "created_by": APPLICANT,
            "workflow_type": "approval",
            "workflow_name": "测试草稿流程",
            "category": "测试",
            "instruction": "员工提交申请，主管审批。",
            "sources": [],
        },
    ).json()
    session_id = created["session"]["session_id"]
    workflow_definition_id = created["workflow"]["workflow_definition_id"]

    delete_response = client.delete(f"/api/v1/workflow-design/sessions/{session_id}")

    assert delete_response.status_code == 200
    assert delete_response.json()["deleted_workflow_definition"] is True
    assert client.get(f"/api/v1/workflow-design/sessions/{session_id}").status_code == 404
    assert client.get(f"/api/v1/workflow-definitions/{workflow_definition_id}", params={"user_id": APPLICANT}).status_code == 404
    management = client.get("/api/v1/workflow-definitions", params={"user_id": APPLICANT}).json()["items"]
    assert all(item["workflow_definition_id"] != workflow_definition_id for item in management)


def test_delete_revision_draft_keeps_published_workflow_definition(tmp_path) -> None:
    client = _client(tmp_path)
    session = _create_session(client)
    session_id = session["session"]["session_id"]

    delete_response = client.delete(f"/api/v1/workflow-design/sessions/{session_id}")

    assert delete_response.status_code == 200
    assert delete_response.json()["deleted_workflow_definition"] is False
    assert client.get(f"/api/v1/workflow-design/sessions/{session_id}").status_code == 404
    workflow_response = client.get(f"/api/v1/workflow-definitions/{LEAVE_WORKFLOW_DEFINITION_ID}", params={"user_id": APPLICANT})
    assert workflow_response.status_code == 200
    assert workflow_response.json()["workflow_definition_id"] == LEAVE_WORKFLOW_DEFINITION_ID


def test_initialize_design_job_completes_and_exposes_events(tmp_path) -> None:
    client = _client(tmp_path)

    response = client.post(
        "/api/v1/workflow-design/initialization-jobs",
        json={
            "created_by": APPLICANT,
            "workflow_type": "approval",
            "workflow_name": "员工请假申请",
            "category": "人事行政",
            "instruction": "员工请假申请，部门主管和部门领导审批。",
            "sources": [],
        },
    )

    assert response.status_code == 200
    job_id = response.json()["job_id"]

    payload = response.json()
    for _ in range(30):
        if payload["status"] in {"completed", "failed"}:
            break
        time.sleep(0.05)
        payload = client.get(f"/api/v1/workflow-design/initialization-jobs/{job_id}").json()

    assert payload["status"] == "completed"
    assert payload["session_id"]
    assert any(event["event_type"] == "job_completed" for event in payload["events"])
    session = client.get(f"/api/v1/workflow-design/sessions/{payload['session_id']}").json()
    assert session["draft_definition"]["meta"]["process_name"] == "员工请假申请"


def test_initialize_design_file_job_preserves_uploaded_file_metadata(tmp_path) -> None:
    client = _client(tmp_path)
    file_bytes = b"\x89PNG\r\n\x1a\nfake image bytes"

    response = client.post(
        "/api/v1/workflow-design/initialization-file-jobs",
        data={
            "created_by": APPLICANT,
            "workflow_type": "approval",
            "workflow_name": "员工请假申请",
            "category": "人事行政",
            "instruction": "员工请假申请，部门主管和部门领导审批。",
            "text_sources_json": "[]",
        },
        files=[
            (
                "sources",
                (
                    "请假截图.png",
                    file_bytes,
                    "image/png",
                ),
            )
        ],
    )

    assert response.status_code == 200
    job_id = response.json()["job_id"]
    payload = response.json()
    for _ in range(30):
        if payload["status"] in {"completed", "failed"}:
            break
        time.sleep(0.05)
        payload = client.get(f"/api/v1/workflow-design/initialization-jobs/{job_id}").json()

    assert payload["status"] == "completed"
    session_response = client.get(f"/api/v1/workflow-design/sessions/{payload['session_id']}")
    assert session_response.status_code == 200
    session = session_response.json()
    uploaded = [source for source in session["sources"] if source["title"] == "请假截图.png"]
    assert len(uploaded) == 1
    assert uploaded[0]["content"] == ""
    assert uploaded[0]["content_preview"] == ""
    assert uploaded[0]["source_type"] == "png"
    assert uploaded[0]["size_bytes"] == len(file_bytes)


def test_validate_reports_missing_time_limits_and_invalid_path(tmp_path) -> None:
    client = _client(tmp_path)
    session = _create_session(client)
    session_id = session["session"]["session_id"]

    validated = client.post(f"/api/v1/workflow-design/sessions/{session_id}/validate")

    assert validated.status_code == 200
    report = validated.json()["validation_report"]
    assert report["status"] == "WARN"
    assert report["warning_count"] == 4
    assert any(issue["category"] == "time_limit" for issue in report["issues"])


def test_workflow_management_reflects_design_draft_after_session_created(tmp_path) -> None:
    client = _client(tmp_path)

    before = client.get("/api/v1/workflow-definitions", params={"user_id": APPLICANT}).json()["items"][0]
    _create_session(client)
    after = client.get("/api/v1/workflow-definitions", params={"user_id": APPLICANT}).json()["items"][0]

    assert before["management"]["draft_state"] == "暂无草稿"
    assert after["management"]["draft_state"] == "AI 草稿"
    assert after["management"]["draft_version"] == "V1.1.0-draft"
