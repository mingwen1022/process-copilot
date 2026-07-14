from __future__ import annotations

import pytest

pytestmark = pytest.mark.skip(
    reason="重运行时已冻结(见 doc/项目重定位与执行规划.md §5)；待用 EOA140 重做运行时夹具后再启用"
)

from fastapi.testclient import TestClient

from app.api.runtime_service import RuntimeService
from app.api.server import create_app


def _client(tmp_path):
    service = RuntimeService(tmp_path / "runtime.db")
    return TestClient(create_app(service))


def test_api_imports_processes_and_starts_instance(tmp_path) -> None:
    client = _client(tmp_path)

    processes = client.get("/api/processes")
    assert processes.status_code == 200
    items = processes.json()["items"]
    process_keys = {item["process_key"] for item in items}
    assert {
        "01_leave_request",
        "02_expense_claim",
        "03_seal_application",
        "04_procurement_request",
        "05_invitation_request",
    }.issubset(process_keys)

    created = client.post(
        "/api/instances",
        json={
            "process_key": "01_leave_request",
            "initiator_user_id": "u_it_app_staff",
            "form_values": {"请假天数": 2},
        },
    )
    assert created.status_code == 200
    instance = created.json()
    assert instance["process_id"] == "LEAVE-001"
    assert instance["open_tasks"][0]["node_id"] == "draft"

    tasks = client.get("/api/tasks", params={"assignee_id": "u_it_app_staff"})
    assert tasks.status_code == 200
    assert any(task["task_id"] == instance["open_tasks"][0]["task_id"] for task in tasks.json()["items"])


def test_api_completes_task_and_persists_next_task(tmp_path) -> None:
    client = _client(tmp_path)
    instance = client.post(
        "/api/instances",
        json={
            "process_key": "01_leave_request",
            "initiator_user_id": "u_it_app_staff",
            "form_values": {"请假天数": 2},
        },
    ).json()
    draft_task = instance["open_tasks"][0]

    completed = client.post(
        f"/api/tasks/{draft_task['task_id']}/complete",
        json={
            "actor_user_id": "u_it_app_staff",
            "path_name": "送部门主管审批",
            "form_updates": {"请假天数": 2},
        },
    )
    assert completed.status_code == 200
    payload = completed.json()
    assert payload["current_node_id"] == "dept_supervisor"
    assert payload["open_tasks"][0]["assignee_id"] == "u_it_app_supervisor"

    loaded = client.get(f"/api/instances/{payload['instance_id']}")
    assert loaded.status_code == 200
    assert loaded.json()["current_node_id"] == "dept_supervisor"
