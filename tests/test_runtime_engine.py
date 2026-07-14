from __future__ import annotations

import json
from pathlib import Path

import pytest

pytestmark = pytest.mark.skip(
    reason="重运行时已冻结(见 doc/项目重定位与执行规划.md §5)；EOA140 含并行多人节点，待重做运行时夹具后再启用"
)

from app.io_utils import find_standard_json, load_process_definition
from app.runtime import RuntimeEngine, SQLiteRuntimeStore
from app.runtime.form_renderer import render_form_schema
from app.runtime.models import InstanceStatus, TaskStatus
from data.schema import ProcessDefinition


def _process(case_dir: str) -> ProcessDefinition:
    return load_process_definition(find_standard_json(case_dir))


def _task(instance, node_id: str, assignee_id: str | None = None):
    for task in instance.open_tasks():
        if task.node_id == node_id and (assignee_id is None or task.assignee_id == assignee_id):
            return task
    raise AssertionError(f"open task not found: {node_id}/{assignee_id}")


def _complete(engine, instance, process, node_id, actor, path, opinion=None):
    task = _task(instance, node_id, actor)
    return engine.complete_task(
        instance,
        process,
        task.task_id,
        actor_user_id=actor,
        path_name=path,
        conclusive_opinion=opinion,
    )


def test_leave_runtime_completes_with_same_actor_skip() -> None:
    process = _process("data/01_leave_request")
    engine = RuntimeEngine()
    instance = engine.start_instance(process, initiator_user_id="u_it_app_staff", form_values={"请假天数": 5})

    _complete(engine, instance, process, "draft", "u_it_app_staff", "送部门主管审批")
    _complete(engine, instance, process, "dept_supervisor", "u_it_app_supervisor", "送部门领导审批", "同意")

    paths = engine.available_paths(
        instance,
        process,
        _task(instance, "dept_leader", "u_it_line_leader").task_id,
        conclusive_opinion="同意",
    )
    assert {path.path_name: path.enabled for path in paths}["送部门总经理审批"] is True
    assert {path.path_name: path.enabled for path in paths}["流程结束"] is False

    _complete(engine, instance, process, "dept_leader", "u_it_line_leader", "送部门总经理审批", "同意")

    assert instance.status == InstanceStatus.COMPLETED
    assert any(log.event_type == "SAME_ACTOR_SKIPPED" and log.node_id == "dept_gm" for log in instance.audit_logs)


def test_expense_runtime_completes_high_amount_path() -> None:
    process = _process("data/02_expense_claim")
    engine = RuntimeEngine()
    instance = engine.start_instance(process, initiator_user_id="u_wealth_staff", form_values={"报销金额（元）": 60000})

    _complete(engine, instance, process, "draft", "u_wealth_staff", "送部门负责人审批")
    _complete(engine, instance, process, "dept_leader", "u_wealth_gm", "送财务初审", "同意")
    _complete(engine, instance, process, "finance_review", "u_finance_staff", "送财务负责人审批", "同意")
    _complete(engine, instance, process, "finance_leader", "u_finance_gm", "送财务确认", "同意")
    _complete(engine, instance, process, "finance_cashier", "u_cashier", "流程结束", "同意")

    assert instance.status == InstanceStatus.COMPLETED


def test_seal_runtime_completes_public_stamp_path() -> None:
    process = _process("data/03_seal_application")
    engine = RuntimeEngine()
    instance = engine.start_instance(process, initiator_user_id="u_admin_staff", form_values={"印章类型": "公章"})

    _complete(engine, instance, process, "draft", "u_admin_staff", "送部门主管审批")
    _complete(engine, instance, process, "dept_supervisor", "u_admin_supervisor", "送法务审核", "同意")
    _complete(engine, instance, process, "legal_review", "u_compliance_supervisor", "送行政管理部审批", "同意")
    _complete(engine, instance, process, "admin_approval", "u_admin_gm", "送印章管理员办理", "同意")
    _complete(engine, instance, process, "seal_officer", "u_seal_admin", "送申请人知悉")
    _complete(engine, instance, process, "applicant_informed", "u_admin_staff", "流程结束")

    assert instance.status == InstanceStatus.COMPLETED


def test_procurement_runtime_handles_parallel_committee() -> None:
    process = _process("data/04_procurement_request")
    engine = RuntimeEngine()
    instance = engine.start_instance(
        process,
        initiator_user_id="u_it_app_staff",
        form_values={"采购金额（元）": 600000, "采购部门受理人": "方若云"},
    )

    _complete(engine, instance, process, "draft", "u_it_app_staff", "送部门主管审批")
    _complete(engine, instance, process, "dept_supervisor", "u_it_app_supervisor", "送部门负责人审批", "同意")
    _complete(engine, instance, process, "dept_leader", "u_it_line_leader", "送采购部门受理", "同意")
    _complete(engine, instance, process, "procurement_accept", "u_procurement_staff", "送采购委员会会签")

    committee_tasks = [task for task in instance.open_tasks() if task.node_id == "committee_review"]
    assert len(committee_tasks) == 5
    for task in committee_tasks[:-1]:
        engine.complete_task(
            instance,
            process,
            task.task_id,
            actor_user_id=task.assignee_id,
            path_name="结束本人处理",
        )
    last = _task(instance, "committee_review")
    engine.complete_task(
        instance,
        process,
        last.task_id,
        actor_user_id=last.assignee_id,
        path_name="送申请人确认",
        conclusive_opinion="同意",
    )
    _complete(engine, instance, process, "applicant_confirm", "u_it_app_staff", "流程结束")

    assert instance.status == InstanceStatus.COMPLETED
    assert all(task.status == TaskStatus.COMPLETED for task in committee_tasks)


@pytest.mark.parametrize(
    ("process_path", "initiator", "form_values", "supervisor"),
    [
        ("data/01_leave_request", "u_it_app_staff", {"请假天数": 2}, "u_it_app_supervisor"),
        ("data/02_expense_claim", "u_wealth_staff", {"报销金额（元）": 1000}, "u_wealth_gm"),
        ("data/03_seal_application", "u_admin_staff", {"印章类型": "公章"}, "u_admin_supervisor"),
        (
            "data/04_procurement_request",
            "u_it_app_staff",
            {"采购金额（元）": 100000, "采购部门受理人": "方若云"},
            "u_it_app_supervisor",
        ),
    ],
)
def test_runtime_return_to_draft_creates_returned_draft_task(
    process_path: str,
    initiator: str,
    form_values: dict[str, object],
    supervisor: str,
) -> None:
    process = _process(process_path)
    engine = RuntimeEngine()
    instance = engine.start_instance(process, initiator_user_id=initiator, form_values=form_values)

    first_path = "送部门负责人审批" if process_path == "data/02_expense_claim" else "送部门主管审批"
    first_approval_node = "dept_leader" if process_path == "data/02_expense_claim" else "dept_supervisor"
    _complete(engine, instance, process, "draft", initiator, first_path)
    _complete(engine, instance, process, first_approval_node, supervisor, "退回起草", "不同意")

    assert instance.status == InstanceStatus.RETURNED
    assert _task(instance, "draft", initiator)
    assert any(log.event_type == "INSTANCE_RETURNED" for log in instance.audit_logs)


def test_form_renderer_marks_draft_permissions() -> None:
    process = _process("data/01_leave_request")
    fields = render_form_schema(process, node_id="draft")
    applicant = next(field for field in fields if field["field_name"] == "申请人")
    leave_reason = next(field for field in fields if field["field_name"] == "请假事由")

    assert applicant["visible"] is True
    assert applicant["editable"] is False
    assert leave_reason["required"] is True
    assert leave_reason["editable"] is True


def test_sqlite_runtime_store_saves_instance_snapshot(tmp_path: Path) -> None:
    process = _process("data/01_leave_request")
    engine = RuntimeEngine()
    instance = engine.start_instance(process, initiator_user_id="u_it_app_staff", form_values={"请假天数": 2})
    store = SQLiteRuntimeStore(tmp_path / "runtime.db")

    store.save_instance(instance)
    snapshot = store.load_instance_snapshot(instance.instance_id)
    listed = store.list_instances()

    assert snapshot["instance_id"] == instance.instance_id
    assert snapshot["tasks"][0]["node_id"] == "draft"
    assert listed[0]["instance_id"] == instance.instance_id
