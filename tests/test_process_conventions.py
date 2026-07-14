from __future__ import annotations

from pathlib import Path

from app.agents.business_validation import _system_prompt as business_validation_system_prompt
from app.agents.process_extraction import _system_prompt as extraction_system_prompt
from app.process_conventions import (
    load_process_design_conventions,
    render_conventions_for_prompt,
    validate_convention_config,
    validate_process_conventions,
)
from app.workflows import process_v1
from data.schema import ProcessDefinition


def _subsidiary_process_with_convention_drift() -> ProcessDefinition:
    return ProcessDefinition.model_validate(
        {
            "meta": {
                "process_id": "SUB-001",
                "process_name": "子公司重大事项test_1",
                "version": "V0.1.0",
                "responsible_dept": "战略发展部",
                "description": "子公司重大事项审批备案",
                "applicant_scope": "总部和子公司人员",
                "entry_point": "OA系统",
            },
            "form_fields": [
                {
                    "seq": 1,
                    "field_name": "编号",
                    "required_stages": ["draft"],
                    "visible_stages": ["all"],
                    "editable_stages": [],
                    "component_type": "只读文本",
                    "logic_description": "系统自动生成",
                    "default_value": None,
                    "options": None,
                    "placeholder": None,
                    "max_length": None,
                },
                {
                    "seq": 2,
                    "field_name": "部门",
                    "required_stages": ["draft"],
                    "visible_stages": ["all"],
                    "editable_stages": ["draft"],
                    "component_type": "只读文本",
                    "logic_description": None,
                    "default_value": None,
                    "options": None,
                    "placeholder": None,
                    "max_length": None,
                },
                {
                    "seq": 3,
                    "field_name": "任务类型",
                    "required_stages": ["draft"],
                    "visible_stages": ["all"],
                    "editable_stages": ["draft"],
                    "component_type": "单选按钮",
                    "logic_description": "选项写在说明里",
                    "default_value": None,
                    "options": None,
                    "placeholder": None,
                    "max_length": None,
                },
            ],
            "flow_nodes": [
                {
                    "node_id": "draft",
                    "node_name": "起草",
                    "is_draft": True,
                    "handler": None,
                    "opinion": None,
                    "opinion_label": None,
                    "time_limit_days": None,
                    "submit_paths": [
                        {"path_name": "送子公司内部人员审核", "condition": None, "target_node_id": "internal_review"}
                    ],
                },
                {
                    "node_id": "internal_review",
                    "node_name": "子公司内部人员审核",
                    "is_draft": False,
                    "handler": {"mode": "多选-并行处理", "source": "本部门", "role": "子公司内部人员", "source_field": None},
                    "opinion": {"conclusive_required": True, "detail_required": False, "conclusive_options": ["同意", "不同意"]},
                    "opinion_label": "子公司意见",
                    "time_limit_days": None,
                    "submit_paths": [
                        {"path_name": "结束本部门处理", "condition": "结论性意见=同意", "target_node_id": "END"}
                    ],
                },
            ],
            "attachments": None,
            "roles": None,
        }
    )


def test_process_design_conventions_config_uses_known_schema_enums() -> None:
    conventions = load_process_design_conventions()

    assert conventions["scope"] == "minimal_EOA140_subsidiary_major_matter"
    assert validate_convention_config(conventions) == []


def test_conventions_render_generic_system_catalog_without_case_answers() -> None:
    rendered = render_conventions_for_prompt()

    # 通用系统词表：组件类型 / 处理人来源 / 处理人方式 / 条件短语
    assert "流程设计输出规范" in rendered
    assert "文号" in rendered  # ComponentType 枚举
    assert "部门选择" in rendered
    assert "表单字段指定" in rendered  # HandlerSource 枚举
    assert "多选-并行处理" in rendered  # HandlerMode 枚举
    assert "部门最后一人" in rendered  # 条件固定短语

    # 回归保护：不得向抽取 prompt 泄漏任何案例级 gold 答案
    assert "子公司内部人员审核" not in rendered
    assert "subsidiary_internal_review" not in rendered
    assert "handler.source=本公司" not in rendered

    assert "流程设计输出规范" in extraction_system_prompt(include_schema=False)
    assert "流程设计输出规范" in business_validation_system_prompt(include_schema=False)


def test_validate_process_conventions_reports_06_drift_without_blocking_schema(tmp_path: Path) -> None:
    process = _subsidiary_process_with_convention_drift()

    warnings = validate_process_conventions(process)
    warning_ids = {warning["convention_id"] for warning in warnings}

    assert {"field_component_type", "field_required_stages", "node_id", "handler_source", "unsupported_action_path"} <= warning_ids

    state = process_v1.validate_structure(
        {
            "case_dir": "data/cases/EOA140_subsidiary_major_matter",
            "out_dir": str(tmp_path),
            "candidate_process": process,
            "validation_retry_count": 0,
            "max_validation_retries": 1,
        }
    )

    report = state["schema_validation_report"]
    assert report["valid"] is True
    assert report["schema_errors"] == []
    assert report["summary"]["convention_warning_count"] >= 5
    assert (tmp_path / "logs" / "structural_validator" / "schema_validation_report_attempt_0.json").exists()
