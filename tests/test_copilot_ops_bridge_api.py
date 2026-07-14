"""在办副驾（办理+排障合并）桥接接口的 API 层测试——用真实 create_app()（真实
Slice1Service + 真实已发布流程定义），不 mock，验证 ensure_instance/diagnose 这两个
新端点真的接对了：待办队列条目→桥接成 ops 实例→能诊断/体检。
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from app.api.server import create_app


def _client() -> TestClient:
    return TestClient(create_app())


def test_ensure_instance_bridges_real_approval_item() -> None:
    client = _client()
    r = client.post("/api/v1/copilot/todo/ap_expense_wang/ensure_instance")
    assert r.status_code == 200
    assert r.json() == {"diagnosable": True}


def test_ensure_instance_is_idempotent_across_calls() -> None:
    client = _client()
    first = client.post("/api/v1/copilot/todo/ap_expense_wang/ensure_instance").json()
    second = client.post("/api/v1/copilot/todo/ap_expense_wang/ensure_instance").json()
    assert first == second == {"diagnosable": True}


def test_ensure_instance_false_for_unknown_item() -> None:
    client = _client()
    r = client.post("/api/v1/copilot/todo/not_a_real_item/ensure_instance")
    assert r.status_code == 200
    assert r.json() == {"diagnosable": False}


def test_inspect_works_on_bridged_item_after_ensure() -> None:
    client = _client()
    client.post("/api/v1/copilot/todo/ap_expense_wang/ensure_instance")
    r = client.get("/api/v1/copilot/instances/ap_expense_wang/inspect")
    assert r.status_code == 200
    assert r.json()["diagnosable"] is True


def test_diagnose_endpoint_works_on_bridged_item() -> None:
    client = _client()
    client.post("/api/v1/copilot/todo/ap_expense_wang/ensure_instance")
    r = client.get("/api/v1/copilot/instances/ap_expense_wang/diagnose")
    assert r.status_code == 200
    body = r.json()
    assert body["diagnosable"] is True
    assert body["current_node_id"] == "dept_manager_approve"


def test_diagnose_endpoint_works_on_pre_existing_mock_instance() -> None:
    # 不用先 ensure——inst_gap 本来就在 ops mock 库里，diagnose 端点本身要能直接用
    client = _client()
    r = client.get("/api/v1/copilot/instances/inst_gap/diagnose")
    assert r.status_code == 200
    assert r.json()["stuck"] is True


def test_approval_item_gets_can_submit_decision_true() -> None:
    client = _client()
    client.post("/api/v1/copilot/todo/ap_expense_wang/ensure_instance")
    detail = client.get("/api/v1/copilot/instances/ap_expense_wang").json()
    assert detail["can_submit_decision"] is True


def test_initiated_item_gets_can_submit_decision_false() -> None:
    """我发起的（非 approval）单据即便走了 ensure，也不该获得 submit_decision 能力——
    发起人在查看自己单子的进度，不是当前环节的处理人，不该被推荐去替处理人下结论。"""
    client = _client()
    r = client.post("/api/v1/copilot/todo/eoa140_case_1/ensure_instance")
    assert r.json() == {"diagnosable": True}
    detail = client.get("/api/v1/copilot/instances/eoa140_case_1").json()
    assert detail["can_submit_decision"] is False


def test_pre_existing_ops_mock_instance_defaults_can_submit_decision_false() -> None:
    client = _client()
    detail = client.get("/api/v1/copilot/instances/inst_gap").json()
    assert detail["can_submit_decision"] is False


def test_reset_endpoint_rebridges_bridged_item_with_original_worklist_form_values() -> None:
    """reset 端点在真实 app 上：桥接 → 摘除并按 WorklistItem 原始字段重新桥接 → 表单值
    还是最初模拟数据。propose/confirm 那条"真的改过之后能不能改回来"的链路已经在
    test_ops_copilot.py/test_todo_service.py 用 stub agent 单测过（不必在这里再打一次真
    Bedrock），这里只验证端点本身接对了、幂等、对未知 id 老实返回 False。"""
    client = _client()
    original = client.get("/api/v1/copilot/instances/ap_expense_wang")
    assert original.status_code == 404  # 还没桥接过

    client.post("/api/v1/copilot/todo/ap_expense_wang/ensure_instance")
    original_total = client.get("/api/v1/copilot/instances/ap_expense_wang").json()["form_values"]["报销总额"]

    reset_r = client.post("/api/v1/copilot/todo/ap_expense_wang/reset")
    assert reset_r.status_code == 200
    assert reset_r.json() == {"diagnosable": True}
    assert client.get("/api/v1/copilot/instances/ap_expense_wang").json()["form_values"]["报销总额"] == original_total


def test_reset_endpoint_false_for_unknown_item() -> None:
    client = _client()
    r = client.post("/api/v1/copilot/todo/not_a_real_item/reset")
    assert r.status_code == 200
    assert r.json() == {"diagnosable": False}


def test_reset_all_endpoint_rebridges_every_worklist_item_on_real_app() -> None:
    """真实 app 上（真实 process_lookup，覆盖请假/报销/EOA140/采购/用印全部 5 条已发布+
    草稿流程），一键重置应该让"我的流程"列表里的每一条都能重新桥接成功。"""
    client = _client()
    worklist = client.get("/api/v1/copilot/todo").json()
    total_items = len(worklist["items"])

    r = client.post("/api/v1/copilot/todo/reset_all")
    assert r.status_code == 200
    assert r.json() == {"reset_count": total_items}
