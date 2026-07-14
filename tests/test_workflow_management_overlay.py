"""回归测试：流程管理卡片的 management 字段必须整体读草稿会话状态，不能有的字段
（issue_count 等）读草稿、有的字段（missing_time_limit_nodes）读已发布/已提交的
definition_json——那样会出现"设计页说已经修好了，流程管理卡片却还说没修"的撕裂。

真实复现路径：AI 在对话里把某个环节的处理期限补上了，但那份改动还在草稿里，
用户还没点"提交上架"——definition_json（已发布/已提交版本）跟
draft_definition_json（当前草稿）这时候是不同的两份数据，management 里的每一个
字段都必须统一取草稿这一份，不能混用。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from app.api.runtime_service import RuntimeService
from app.api.server import create_app
from app.api.slice1_service import Slice1Service
from app.api.workflow_design_service import WorkflowDesignService
from data.schema import ProcessDefinition

CREATED_BY = "u_it_app_staff"


def _process_with_time_limits(missing: bool) -> ProcessDefinition:
    return ProcessDefinition.model_validate(
        {
            "meta": {
                "process_id": "TEST-001",
                "process_name": "测试请假流程",
                "version": "V0.1.0",
                "responsible_dept": "人力资源部",
                "description": "测试用流程",
                "applicant_scope": "全员",
                "entry_point": "OA系统",
            },
            "form_fields": [],
            "flow_nodes": [
                {
                    "node_id": "draft",
                    "node_name": "起草",
                    "is_draft": True,
                    "handler": None,
                    "opinion": None,
                    "opinion_label": None,
                    "time_limit_days": None if missing else 1,
                    "submit_paths": [{"path_name": "送审批", "condition": None, "target_node_id": "approval"}],
                },
                {
                    "node_id": "approval",
                    "node_name": "审批",
                    "is_draft": False,
                    "handler": {"mode": "单选-单人处理", "source": "本部门", "role": "部门主管", "source_field": None},
                    "opinion": {"conclusive_required": True, "detail_required": False, "conclusive_options": ["同意", "不同意"]},
                    "opinion_label": "部门主管意见",
                    "time_limit_days": None if missing else 3,
                    "submit_paths": [{"path_name": "流程结束", "condition": "结论性意见=同意", "target_node_id": "END"}],
                },
            ],
            "attachments": None,
            "roles": None,
        }
    )


def _process_with_mixed_issues() -> ProcessDefinition:
    """一个 time_limit 类问题（起草缺处理期限）+ 一个非 time_limit 类问题
    （审批环节缺处理角色，触发 _validate_definition 的 role 类校验），用来验证
    non_time_limit_issue_count 精确排除了 time_limit 类、不是恒等于 0。"""
    return ProcessDefinition.model_validate(
        {
            "meta": {
                "process_id": "TEST-001",
                "process_name": "测试请假流程",
                "version": "V0.1.0",
                "responsible_dept": "人力资源部",
                "description": "测试用流程",
                "applicant_scope": "全员",
                "entry_point": "OA系统",
            },
            "form_fields": [
                {
                    "seq": 1,
                    "field_name": "请假事由",
                    "required_stages": [],
                    "visible_stages": ["all"],
                    "editable_stages": ["draft"],
                    "component_type": "多行文本",
                    "logic_description": None,
                    "default_value": None,
                    "options": None,
                    "placeholder": None,
                    "max_length": None,
                }
            ],
            "flow_nodes": [
                {
                    "node_id": "draft",
                    "node_name": "起草",
                    "is_draft": True,
                    "handler": None,
                    "opinion": None,
                    "opinion_label": None,
                    "time_limit_days": None,  # → time_limit 类 issue
                    "submit_paths": [{"path_name": "送审批", "condition": None, "target_node_id": "approval"}],
                },
                {
                    "node_id": "approval",
                    "node_name": "审批",
                    "is_draft": False,
                    "handler": None,  # → role 类 issue（非 time_limit）
                    "opinion": {"conclusive_required": True, "detail_required": False, "conclusive_options": ["同意", "不同意"]},
                    "opinion_label": "部门主管意见",
                    "time_limit_days": 3,
                    "submit_paths": [
                        {"path_name": "流程结束", "condition": "结论性意见=同意", "target_node_id": "END"},
                        {"path_name": "退回起草", "condition": "结论性意见=不同意", "target_node_id": "DRAFT"},
                    ],
                },
            ],
            "attachments": None,
            "roles": None,
        }
    )


def _fake_graph_initializer(**_kwargs: Any) -> dict[str, Any]:
    payload = _process_with_time_limits(missing=True).model_dump(mode="json")
    return {
        "output_paths": {},
        "workflow_design_output": {"process_definition": payload},
        "designer_assistant_message": {},
        "user_clarification_requests": [],
        "design_persistence_report": {},
        "schema_validation_report": {},
        "business_validation_result": {},
    }


def _new_session(service: WorkflowDesignService) -> tuple[str, str]:
    payload = service.initialize_new_session(
        created_by=CREATED_BY,
        workflow_type="approval",
        workflow_name="测试请假流程",
        category="审批流程",
        instruction="员工请假申请流程",
        sources=[],
        uploaded_files=[],
    )
    return payload["session"]["session_id"], payload["session"]["workflow_definition_id"]


def test_missing_time_limit_is_optional_not_a_validation_issue(tmp_path: Path) -> None:
    """处理期限是可选建议项、不是规定项：环节没配处理期限不该生成校验 issue、不该计入
    issue_count/待处理——那件事该不该配、配多长，交给运行数据（分析侧堵点反哺）判断，
    不是不分青红皂白地对每个环节都要求配置。missing_time_limit_nodes 仍照旧算出来，
    只是纯信息展示（环节详情面板用），不再是"问题"。"""

    def graph_initializer(**_kwargs: Any) -> dict[str, Any]:
        payload = _process_with_mixed_issues().model_dump(mode="json")
        return {
            "output_paths": {},
            "workflow_design_output": {"process_definition": payload},
            "designer_assistant_message": {},
            "user_clarification_requests": [],
            "design_persistence_report": {},
            "schema_validation_report": {},
            "business_validation_result": {},
        }

    db_path = tmp_path / "runtime.db"
    Slice1Service(db_path)
    workflow_design = WorkflowDesignService(db_path, graph_initializer=graph_initializer)

    _, workflow_definition_id = _new_session(workflow_design)
    summary = workflow_design.design_summary_for_workflow(workflow_definition_id)

    assert summary["issue_count"] == 1  # 只有 role 那一条——time_limit 缺失不算数
    assert summary["missing_time_limit_nodes"] == ["draft"]  # 信息仍在，只是不算"待处理"
    assert summary["non_time_limit_issue_count"] == 1  # 现在恒等于 issue_count


def test_missing_time_limit_nodes_reflects_draft_not_published_definition(tmp_path: Path) -> None:
    db_path = tmp_path / "runtime.db"
    Slice1Service(db_path)  # 建共享表 + 种子用户
    workflow_design = WorkflowDesignService(db_path, graph_initializer=_fake_graph_initializer)

    session_id, workflow_definition_id = _new_session(workflow_design)
    # 创建时 definition_json 和 draft_definition_json 内容一致，都缺处理期限
    before = workflow_design.design_summary_for_workflow(workflow_definition_id)
    assert before["missing_time_limit_nodes"] == ["draft", "approval"]

    # 模拟：AI 在对话里把草稿修好了（审批环节补上处理期限），但还没提交上架——
    # definition_json（已发布/提交版本）保持原样，只有 draft_definition_json 变了。
    fixed_draft = _process_with_time_limits(missing=False).model_dump(mode="json")
    import json

    with workflow_design._connect() as conn:  # noqa: SLF001 - 测试内直接摆状态，模拟"已修但未提交"
        conn.execute(
            "UPDATE workflow_design_sessions SET draft_definition_json = ? WHERE id = ?",
            (json.dumps(fixed_draft), session_id),
        )

    after = workflow_design.design_summary_for_workflow(workflow_definition_id)
    assert after["missing_time_limit_nodes"] == []  # 草稿已经修好了，不该再报缺


def test_workflow_definitions_endpoint_surfaces_draft_state_not_stale_published_state(tmp_path: Path) -> None:
    db_path = tmp_path / "runtime.db"
    slice1 = Slice1Service(db_path)
    workflow_design = WorkflowDesignService(db_path, graph_initializer=_fake_graph_initializer)
    runtime = RuntimeService(db_path)
    client = TestClient(create_app(runtime, slice1, workflow_design))

    session_id, workflow_definition_id = _new_session(workflow_design)

    fixed_draft = _process_with_time_limits(missing=False).model_dump(mode="json")
    import json

    with workflow_design._connect() as conn:  # noqa: SLF001
        conn.execute(
            "UPDATE workflow_design_sessions SET draft_definition_json = ? WHERE id = ?",
            (json.dumps(fixed_draft), session_id),
        )

    items = client.get("/api/v1/workflow-definitions").json()["items"]
    card = next(i for i in items if i["workflow_definition_id"] == workflow_definition_id)
    # 这就是截图里复现的那个 bug：卡片必须跟设计页一样看到"已经修好了"，
    # 不能还在读没被这次修改碰过的已提交版本。
    assert card["management"]["missing_time_limit_nodes"] == []


def test_pre_session_card_does_not_flag_missing_time_limit_as_issue(tmp_path: Path) -> None:
    """还没开设计会话的流程（slice1_service._workflow_management_payload 这条独立路径）
    同样不该把"缺处理期限"算成 issue——之前这条路径的 issue_count 完全等于缺时限的
    环节数，一条流程但凡有节点没配时限就显示"待完善/amber"，跟"处理期限是可选项，不是
    规定项"这条原则矛盾。"""
    db_path = tmp_path / "runtime.db"
    slice1 = Slice1Service(db_path)  # 播种 5 条正式流程，多数节点本就没配 time_limit_days
    workflow_design = WorkflowDesignService(db_path, graph_initializer=_fake_graph_initializer)
    runtime = RuntimeService(db_path)
    client = TestClient(create_app(runtime, slice1, workflow_design))

    items = client.get("/api/v1/workflow-definitions").json()["items"]
    leave = next(i for i in items if i["code"] == "leave_request")
    assert len(leave["management"]["missing_time_limit_nodes"]) > 0  # 确实有环节没配（信息仍在）
    assert leave["management"]["issue_count"] == 0  # 但不算"待处理"
    assert leave["management"]["health"] == "正常"
    assert leave["management"]["health_tone"] == "green"
