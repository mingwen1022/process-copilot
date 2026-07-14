from __future__ import annotations

import pytest

pytestmark = pytest.mark.skip(
    reason="重运行时已冻结(见 doc/项目重定位与执行规划.md §5)；待用 EOA140 重做运行时夹具后再启用"
)

from fastapi.testclient import TestClient

from app.api.runtime_service import RuntimeService
from app.api.server import create_app
from app.api.slice1_service import LEAVE_WORKFLOW_DEFINITION_ID, Slice1Service


APPLICANT = "u_it_app_staff"
SUPERVISOR = "u_it_app_supervisor"
LEADER = "u_it_line_leader"


def _client(tmp_path):
    db_path = tmp_path / "runtime.db"
    return TestClient(create_app(RuntimeService(db_path), Slice1Service(db_path)))


def _leave_values(days: int = 2) -> dict[str, object]:
    end = "2026-06-11" if days <= 3 else "2026-06-16"
    return {
        "假期类型": "年假",
        "开始日期": "2026-06-10",
        "结束日期": end,
        "请假事由": "家庭事务安排",
        "代理人": "李承宇",
        "紧急联系方式": "13800000000",
    }


def _create_case(client: TestClient) -> dict:
    response = client.post(
        "/api/v1/cases",
        json={
            "workflow_definition_id": LEAVE_WORKFLOW_DEFINITION_ID,
            "initiator_user_id": APPLICANT,
            "form_values": {},
        },
    )
    assert response.status_code == 200
    return response.json()


def _submit_draft(client: TestClient, case_id: str, values: dict[str, object] | None = None) -> dict:
    response = client.post(
        f"/api/v1/cases/{case_id}/submit",
        json={"actor_user_id": APPLICANT, "form_values": values or _leave_values()},
    )
    assert response.status_code == 200
    return response.json()


def _first_item(client: TestClient, user_id: str, bucket: str) -> dict:
    response = client.get("/api/v1/workbench/items", params={"user_id": user_id, "bucket": bucket})
    assert response.status_code == 200
    items = response.json()["items"]
    assert items
    return items[0]


def test_v1_catalog_only_returns_leave_request(tmp_path) -> None:
    client = _client(tmp_path)

    response = client.get("/api/v1/workflows/catalog", params={"user_id": APPLICANT})

    assert response.status_code == 200
    items = response.json()["items"]
    assert [item["workflow_definition_id"] for item in items] == [LEAVE_WORKFLOW_DEFINITION_ID]
    assert items[0]["name"] == "员工请假申请"


def test_v1_workflow_definitions_management_list_only_returns_leave_request(tmp_path) -> None:
    client = _client(tmp_path)

    response = client.get("/api/v1/workflow-definitions", params={"user_id": APPLICANT})

    assert response.status_code == 200
    items = response.json()["items"]
    assert [item["workflow_definition_id"] for item in items] == [LEAVE_WORKFLOW_DEFINITION_ID]
    workflow = items[0]
    assert workflow["name"] == "员工请假申请"
    assert workflow["field_count"] == 10
    assert workflow["node_count"] == 4
    assert workflow["path_count"] == 9
    assert workflow["management"]["draft_state"] == "暂无草稿"
    assert workflow["management"]["issue_count"] == 4
    assert workflow["runtime_stats"]["total_instances"] == 0


def test_v1_workflow_definition_management_detail_returns_definition_and_stats(tmp_path) -> None:
    client = _client(tmp_path)
    _create_case(client)

    response = client.get(
        f"/api/v1/workflow-definitions/{LEAVE_WORKFLOW_DEFINITION_ID}",
        params={"user_id": APPLICANT},
    )

    assert response.status_code == 200
    payload = response.json()
    assert len(payload["definition"]["form_fields"]) == 10
    assert len(payload["definition"]["flow_nodes"]) == 4
    assert sum(len(node["submit_paths"]) for node in payload["definition"]["flow_nodes"]) == 9
    assert payload["runtime_stats"]["total_instances"] == 1
    assert payload["runtime_stats"]["draft_instances"] == 1


def test_v1_workflow_definition_management_unknown_id_returns_404(tmp_path) -> None:
    client = _client(tmp_path)

    response = client.get("/api/v1/workflow-definitions/not_found", params={"user_id": APPLICANT})

    assert response.status_code == 404


def test_v1_login_uses_employee_account_and_password(tmp_path) -> None:
    client = _client(tmp_path)

    ok = client.post("/api/v1/session/login", json={"account": "HD1104", "password": "123456"})
    bad = client.post("/api/v1/session/login", json={"account": "HD1104", "password": "wrong"})

    assert ok.status_code == 200
    assert ok.json()["user"]["user_id"] == APPLICANT
    assert bad.status_code == 400


def test_create_case_generates_applicant_draft_only(tmp_path) -> None:
    client = _client(tmp_path)

    detail = _create_case(client)
    case_id = detail["case"]["id"]

    applicant_drafts = client.get("/api/v1/workbench/items", params={"user_id": APPLICANT, "bucket": "drafts"}).json()["items"]
    supervisor_todo = client.get("/api/v1/workbench/items", params={"user_id": SUPERVISOR, "bucket": "todo"}).json()["items"]
    assert any(item["case_id"] == case_id and item["node_id"] == "draft" for item in applicant_drafts)
    assert supervisor_todo == []


def test_save_draft_updates_form_without_routing(tmp_path) -> None:
    client = _client(tmp_path)
    case_id = _create_case(client)["case"]["id"]

    saved = client.patch(
        f"/api/v1/cases/{case_id}/form",
        json={"actor_user_id": APPLICANT, "form_values": _leave_values()},
    )

    assert saved.status_code == 200
    payload = saved.json()
    assert payload["form_values"]["请假天数"] == 2
    assert payload["case"]["status"] == "DRAFT"
    assert len(payload["open_work_items"]) == 1
    assert payload["open_work_items"][0]["type"] == "DRAFT"


def test_submit_and_approve_leave_case_to_completion(tmp_path) -> None:
    client = _client(tmp_path)
    case_id = _create_case(client)["case"]["id"]

    submitted = _submit_draft(client, case_id)
    assert submitted["case"]["status"] == "IN_PROGRESS"
    assert submitted["case"]["current_node_id"] == "dept_supervisor"
    assert _first_item(client, SUPERVISOR, "todo")["node_id"] == "dept_supervisor"

    supervisor_item = _first_item(client, SUPERVISOR, "todo")
    options = client.post(
        f"/api/v1/work-items/{supervisor_item['work_item_id']}/route-options",
        json={"actor_user_id": SUPERVISOR, "decision": "同意"},
    ).json()["items"]
    assert {item["path_name"]: item["enabled"] for item in options}["送部门领导审批"] is True

    leader_step = client.post(
        f"/api/v1/work-items/{supervisor_item['work_item_id']}/complete",
        json={"actor_user_id": SUPERVISOR, "decision": "同意", "path_name": "送部门领导审批"},
    )
    assert leader_step.status_code == 200
    assert leader_step.json()["case"]["current_node_id"] == "dept_leader"
    assert _first_item(client, LEADER, "todo")["node_id"] == "dept_leader"
    assert client.get("/api/v1/workbench/items", params={"user_id": "u_it_deputy", "bucket": "todo"}).json()["items"] == []

    leader_item = _first_item(client, LEADER, "todo")
    completed = client.post(
        f"/api/v1/work-items/{leader_item['work_item_id']}/complete",
        json={"actor_user_id": LEADER, "decision": "同意", "path_name": "流程结束"},
    )
    assert completed.status_code == 200
    payload = completed.json()
    assert payload["case"]["status"] == "COMPLETED"
    assert payload["case"]["current_node_id"] == "END"
    assert any(event["type"] == "CASE_COMPLETED" for event in payload["timeline"])


def test_reject_returns_case_to_applicant_drafts(tmp_path) -> None:
    client = _client(tmp_path)
    case_id = _create_case(client)["case"]["id"]
    _submit_draft(client, case_id)
    supervisor_item = _first_item(client, SUPERVISOR, "todo")

    returned = client.post(
        f"/api/v1/work-items/{supervisor_item['work_item_id']}/complete",
        json={"actor_user_id": SUPERVISOR, "decision": "不同意", "path_name": "退回起草", "comment": "请补充请假说明"},
    )

    assert returned.status_code == 200
    payload = returned.json()
    assert payload["case"]["status"] == "RETURNED"
    applicant_drafts = client.get("/api/v1/workbench/items", params={"user_id": APPLICANT, "bucket": "drafts"}).json()["items"]
    assert any(item["case_id"] == case_id and item["node_id"] == "draft" for item in applicant_drafts)
    assert any(event["type"] == "CASE_RETURNED" for event in payload["timeline"])


def test_done_bucket_deduplicates_cases_and_hides_current_open_items(tmp_path) -> None:
    client = _client(tmp_path)
    case_id = _create_case(client)["case"]["id"]
    _submit_draft(client, case_id)
    supervisor_item = _first_item(client, SUPERVISOR, "todo")

    returned = client.post(
        f"/api/v1/work-items/{supervisor_item['work_item_id']}/complete",
        json={"actor_user_id": SUPERVISOR, "decision": "不同意", "path_name": "退回起草", "comment": "请补充请假说明"},
    )
    assert returned.status_code == 200
    done_after_return = client.get("/api/v1/workbench/items", params={"user_id": SUPERVISOR, "bucket": "done"}).json()["items"]
    assert len(done_after_return) == 1
    assert done_after_return[0]["case_id"] == case_id
    assert done_after_return[0]["handled_count"] == 1

    _submit_draft(client, case_id)
    assert client.get("/api/v1/workbench/items", params={"user_id": SUPERVISOR, "bucket": "done"}).json()["items"] == []
    assert client.get("/api/v1/workbench/summary", params={"user_id": SUPERVISOR}).json()["counts"]["done"] == 0

    supervisor_item = _first_item(client, SUPERVISOR, "todo")
    approved = client.post(
        f"/api/v1/work-items/{supervisor_item['work_item_id']}/complete",
        json={"actor_user_id": SUPERVISOR, "decision": "同意", "path_name": "送部门领导审批"},
    )
    assert approved.status_code == 200

    done_after_approve = client.get("/api/v1/workbench/items", params={"user_id": SUPERVISOR, "bucket": "done"}).json()["items"]
    assert len(done_after_approve) == 1
    assert done_after_approve[0]["case_id"] == case_id
    assert done_after_approve[0]["handled_count"] == 2
    assert done_after_approve[0]["decision"] == "同意"
    assert client.get("/api/v1/workbench/summary", params={"user_id": SUPERVISOR}).json()["counts"]["done"] == 1


def test_non_current_handler_cannot_complete_work_item(tmp_path) -> None:
    client = _client(tmp_path)
    case_id = _create_case(client)["case"]["id"]
    _submit_draft(client, case_id)
    supervisor_item = _first_item(client, SUPERVISOR, "todo")

    response = client.post(
        f"/api/v1/work-items/{supervisor_item['work_item_id']}/complete",
        json={"actor_user_id": APPLICANT, "decision": "同意", "path_name": "送部门领导审批"},
    )

    assert response.status_code == 400
