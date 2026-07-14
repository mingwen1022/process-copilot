from __future__ import annotations

import copy
import json

from app.io_utils import find_standard_json, load_process_definition, load_standard_target
from app.reporting.comparison import compare_processes, merge_clarification_eval, render_missing_report
from data.schema import ProcessDefinition


def _minimal_process(*, node_id: str = "approval", target_node_id: str = "END", path_name: str = "送审批", condition: str | None = None) -> ProcessDefinition:
    return ProcessDefinition.model_validate(
        {
            "meta": {
                "process_id": "TEST-001",
                "process_name": "测试流程",
                "version": "V1.0.0",
                "responsible_dept": "测试部门",
                "description": "测试",
                "applicant_scope": "全员",
                "entry_point": "OA系统",
            },
            "form_fields": [
                {
                    "seq": 1,
                    "field_name": "标题",
                    "required_stages": ["draft"],
                    "visible_stages": ["all"],
                    "editable_stages": ["draft"],
                    "component_type": "单行文本",
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
                    "time_limit_days": None,
                    "submit_paths": [
                        {"path_name": "送审批", "condition": None, "target_node_id": node_id}
                    ],
                },
                {
                    "node_id": node_id,
                    "node_name": "审批环节",
                    "is_draft": False,
                    "handler": {"mode": "单选-单人处理", "source": "本部门", "role": "部门主管", "source_field": None},
                    "opinion": {"conclusive_required": True, "detail_required": False, "conclusive_options": ["同意", "不同意"]},
                    "opinion_label": "审批意见",
                    "time_limit_days": None,
                    "submit_paths": [
                        {"path_name": path_name, "condition": condition, "target_node_id": target_node_id}
                    ],
                },
            ],
            "attachments": None,
            "roles": None,
        }
    )


def test_comparison_reports_missing_field_and_path_condition_difference() -> None:
    expected = load_process_definition(find_standard_json("data/cases/EOA140_subsidiary_major_matter"))
    actual_data = copy.deepcopy(expected.model_dump(mode="json"))
    actual_data["form_fields"] = [field for field in actual_data["form_fields"] if field["field_name"] != "联系电话"]
    actual_data["flow_nodes"][1]["submit_paths"][0]["condition"] = "结论性意见=同意"
    actual = ProcessDefinition.model_validate(actual_data)

    report = compare_processes(actual, expected)
    markdown = render_missing_report(report)

    assert report["missing_form_fields"] == ["联系电话"]
    assert report["path_condition_differences"][0]["node_id"] == "subsidiary_internal_review"
    assert "联系电话" in markdown
    assert "路径条件差异" in markdown


def test_comparison_renders_schema_errors() -> None:
    expected = load_process_definition(find_standard_json("data/cases/EOA140_subsidiary_major_matter"))
    report = compare_processes(
        None,
        expected,
        schema_errors=[{"loc": ["meta", "process_id"], "msg": "Field required", "type": "missing"}],
    )
    markdown = render_missing_report(report)

    assert report["summary"]["has_differences"] is True
    assert "meta.process_id" in markdown


def test_comparison_reports_missing_attachment_role_and_clarification() -> None:
    standard_json = find_standard_json("data/cases/EOA140_subsidiary_major_matter")
    expected = load_process_definition(standard_json)
    actual_data = expected.model_dump(mode="json")
    actual_data["attachments"] = None
    actual_data["roles"] = None
    actual = ProcessDefinition.model_validate(actual_data)

    report = compare_processes(actual, expected)
    report = merge_clarification_eval(report, [], load_standard_target(standard_json))
    markdown = render_missing_report(report)

    assert report["missing_attachments"] == ["普通附件"]
    assert set(report["missing_roles"]) == {"协作流程_会签专员", "合规与风险管理委员会"}
    assert any(item["id"] == "clarify_task_name_mapping" for item in report["missing_required_clarifications"])
    assert "缺失待确认项" in markdown


def test_comparison_aligns_flow_nodes_by_chinese_name_for_paths() -> None:
    expected = _minimal_process(node_id="subsidiary_internal_review")
    actual = _minimal_process(node_id="internal_review")

    report = compare_processes(actual, expected)

    assert report["missing_flow_nodes"] == []
    assert report["extra_flow_nodes"] == []
    assert report["missing_submit_paths"] == []
    assert report["extra_submit_paths"] == []
    assert report["node_alias_map"]["subsidiary_internal_review"]["match_basis"] == "node_name_fallback"
    assert report["matched_paths"][0]["match_basis"] == "source_target_path_name"


def test_comparison_aligns_draft_and_end_special_path_targets() -> None:
    expected = _minimal_process(
        node_id="approval",
        target_node_id="DRAFT",
        path_name="退回起草",
        condition="结论性意见=不同意",
    )
    actual = _minimal_process(
        node_id="approval",
        target_node_id="draft",
        path_name="返回起草",
        condition="不同意",
    )

    report = compare_processes(actual, expected)

    assert report["missing_submit_paths"] == []
    assert report["extra_submit_paths"] == []
    assert report["path_condition_differences"] == []


def test_comparison_marks_ambiguous_path_candidates_without_guessing() -> None:
    expected = _minimal_process(
        node_id="approval",
        target_node_id="END",
        path_name="送结束",
        condition="结论性意见=同意 且 最后一人",
    )
    actual_data = expected.model_dump(mode="json")
    actual_data["flow_nodes"][1]["submit_paths"] = [
        {"path_name": "路径A", "condition": "条件A", "target_node_id": "END"},
        {"path_name": "路径B", "condition": "条件B", "target_node_id": "END"},
    ]
    actual = ProcessDefinition.model_validate(actual_data)

    report = compare_processes(actual, expected)

    assert report["missing_submit_paths"]
    assert report["ambiguous_path_matches"]
    assert report["ambiguous_path_matches"][0]["candidate_count"] == 2


def test_clarification_matching_uses_stable_topics() -> None:
    expected = _minimal_process()
    report = compare_processes(expected, expected)
    report = merge_clarification_eval(
        report,
        [
            {"question": "处理期限目前仍有缺失，请补充。", "recommendation": "建议统一配置审批 SLA。"},
            {"question": "是否作为公司领导批示后的可选分支纳入主流程路径？", "reason": "合规与风险管理委员会路径不明确。"},
        ],
        {
            "clarification_targets": [
                {"id": "sla", "question": "各审批环节是否配置处理期限/SLA？"},
                {"id": "committee", "question": "合规与风险管理委员会是否纳入可选分支？"},
            ]
        },
    )

    assert report["missing_required_clarifications"] == []
    assert {item["id"] for item in report["matched_required_clarifications"]} == {"sla", "committee"}
