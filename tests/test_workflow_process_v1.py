from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

from app.io_utils import find_standard_json, load_process_definition
from app.agents.process_extraction import ProcessExtractionAgent
from app.workflows import process_v1
from app.workflows.process_v1 import build_graph, resolve_cli_out_dir, run_process_case, run_process_case_stream
from app.workflows.state import WorkflowState
from data.schema import ProcessDefinition


class FakeExtractionAgent:
    def __init__(self, process_response: str | list[str]) -> None:
        self.process_responses = process_response if isinstance(process_response, list) else [process_response]
        self.calls = 0
        self.states: list[WorkflowState] = []

    def run(self, state: WorkflowState) -> WorkflowState:
        self.states.append(state.copy())
        response = self.process_responses[min(self.calls, len(self.process_responses) - 1)]
        self.calls += 1
        try:
            process = ProcessDefinition.model_validate_json(response)
        except Exception:
            process = None
        return {**state, "candidate_process": process, "llm_raw_output": response}


def _passing_business_validation() -> dict[str, Any]:
    return {
        "passed": True,
        "blocking_issues": [],
        "warning_issues": [],
        "raw_missing_items": [],
        "user_clarification_requests": [],
        "repair_instructions": [],
        "summary": "pass",
    }


class FakeBusinessValidationAgent:
    def __init__(self, result_response: dict[str, Any] | list[dict[str, Any]] | None = None) -> None:
        responses = result_response if isinstance(result_response, list) else [result_response or _passing_business_validation()]
        self.result_responses = responses
        self.calls = 0
        self.states: list[WorkflowState] = []

    def run(self, state: WorkflowState) -> WorkflowState:
        self.states.append(state.copy())
        result = self.result_responses[min(self.calls, len(self.result_responses) - 1)]
        self.calls += 1
        return {
            **state,
            "business_validation_result": result,
            "business_validation_raw_output": json.dumps(result, ensure_ascii=False),
        }


class FakeStructuredRunner:
    def __init__(self, responses: list[dict[str, Any]]) -> None:
        self.responses = responses
        self.messages: list[list[dict[str, str]]] = []

    def invoke(self, messages: list[dict[str, str]]) -> dict[str, Any]:
        self.messages.append(messages)
        index = min(len(self.messages) - 1, len(self.responses) - 1)
        return self.responses[index]


class FakeStructuredModel:
    def __init__(self, responses: list[dict[str, Any]]) -> None:
        self.runner = FakeStructuredRunner(responses)
        self.structured_output_calls: list[dict[str, Any]] = []

    def with_structured_output(self, schema: Any, **kwargs: Any) -> FakeStructuredRunner:
        self.structured_output_calls.append({"schema": schema, "kwargs": kwargs})
        return self.runner


def _standard_process_json(case_dir: str | Path) -> str:
    return load_process_definition(find_standard_json(case_dir)).model_dump_json()


def _standard_process_data(case_dir: str | Path) -> dict[str, Any]:
    return json.loads(_standard_process_json(case_dir))


def test_workflow_graph_uses_expected_node_names() -> None:
    response = _standard_process_json("data/cases/EOA140_subsidiary_major_matter")
    graph = build_graph(
        extraction_agent=FakeExtractionAgent(response),
        validation_agent=FakeBusinessValidationAgent(),
    )

    assert {
        "source_file_loader",
        "source_context_builder",
        "process_extraction_agent",
        "structural_validator",
        "business_validation_agent",
        "workflow_design_output_writer",
    }.issubset(set(graph.get_graph().nodes))
    assert "artifact_generation_agent" not in graph.get_graph().nodes
    assert "write_run_outputs" not in graph.get_graph().nodes


def test_workflow_stream_emits_node_progress_events(tmp_path: Path) -> None:
    response = _standard_process_json("data/cases/EOA140_subsidiary_major_matter")
    events: list[dict[str, Any]] = []

    result = run_process_case_stream(
        "data/cases/EOA140_subsidiary_major_matter",
        tmp_path,
        extraction_agent=FakeExtractionAgent(response),
        validation_agent=FakeBusinessValidationAgent(),
        event_callback=events.append,
    )

    assert result["workflow_design_output"]["status"] == "draft_ready"
    assert any(event.get("node") == "source_file_loader" and event.get("status") == "running" for event in events)
    assert any(event.get("node") == "workflow_design_output_writer" and event.get("status") == "done" for event in events)


def test_cli_output_dir_appends_timestamp() -> None:
    out_dir = resolve_cli_out_dir("data/cases/EOA140_subsidiary_major_matter", "runs/EOA140")

    assert out_dir.parent == Path("runs")
    assert out_dir.name.startswith("EOA140_")
    assert len(out_dir.name.removeprefix("EOA140_")) == len("20260516_153000")


def test_cli_output_dir_can_disable_timestamp() -> None:
    assert resolve_cli_out_dir("data/cases/EOA140_subsidiary_major_matter", "runs/EOA140", timestamp=False) == Path("runs/EOA140")


def test_workflow_graph_has_validation_conditional_edges() -> None:
    response = _standard_process_json("data/cases/EOA140_subsidiary_major_matter")
    graph = build_graph(
        extraction_agent=FakeExtractionAgent(response),
        validation_agent=FakeBusinessValidationAgent(),
    )
    mermaid = graph.get_graph().draw_mermaid()

    assert "retry_extraction" in mermaid
    assert "write_outputs" in mermaid
    assert "business_validation" in mermaid
    assert "generate_artifacts" not in mermaid


def test_agents_package_contains_expected_agent_modules() -> None:
    modules = {path.name for path in Path("app/agents").glob("*.py") if path.name != "__init__.py"}
    assert {"business_validation.py", "process_extraction.py"}.issubset(modules)
    assert "artifact_generation.py" in modules


def test_process_extraction_agent_does_not_use_strict_response_format() -> None:
    source = Path("app/agents/process_extraction.py").read_text(encoding="utf-8")

    assert "response_format=ProcessDefinition" not in source
    assert "with_structured_output" in source
    assert 'method="function_calling"' in source


def test_process_extraction_agent_repairs_structured_output_parse_error(tmp_path: Path) -> None:
    process = load_process_definition(find_standard_json("data/cases/EOA140_subsidiary_major_matter"))
    repaired_raw = process.model_dump_json(indent=2)
    malformed_raw = '{"logic_description": "系统自动生成，规则：申请人姓名+"用印申请""}'
    model = FakeStructuredModel(
        [
            {"raw": malformed_raw, "parsed": None, "parsing_error": ValueError("Expecting ',' delimiter")},
            {"raw": repaired_raw, "parsed": process, "parsing_error": None},
        ]
    )
    agent = ProcessExtractionAgent(model=model)

    state = agent.run(
        {
            "case_dir": "data/cases/EOA140_subsidiary_major_matter",
            "out_dir": str(tmp_path),
            "source_context": "raw source text",
            "validation_retry_count": 0,
            "max_validation_retries": 1,
            "verbose": False,
        }
    )

    assert state["candidate_process"] == process
    assert state["llm_raw_output"] == repaired_raw
    assert model.structured_output_calls[0]["schema"] is ProcessDefinition
    assert model.structured_output_calls[0]["kwargs"]["method"] == "function_calling"
    assert model.structured_output_calls[0]["kwargs"]["include_raw"] is True
    assert "JSON 格式修复器" in model.runner.messages[1][0]["content"]
    node_dir = tmp_path / "logs" / "process_extraction_agent"
    assert (node_dir / "llm_raw_output_attempt_0.txt").read_text(encoding="utf-8") == malformed_raw
    assert (node_dir / "llm_raw_output_final_attempt_0.txt").read_text(encoding="utf-8") == repaired_raw
    assert (node_dir / "messages_attempt_0.json").exists()


def test_workflow_with_fake_extraction_generates_outputs(tmp_path: Path) -> None:
    case_dir = Path("data/cases/EOA140_subsidiary_major_matter")
    response = _standard_process_json(case_dir)
    extraction_agent = FakeExtractionAgent(response)

    state = run_process_case(
        case_dir,
        tmp_path,
        extraction_agent=extraction_agent,
        validation_agent=FakeBusinessValidationAgent(),
    )

    assert extraction_agent.calls == 1
    assert Path(state["output_paths"]["process_def"]).exists()
    assert Path(state["output_paths"]["workflow_design_output"]).exists()
    assert Path(state["output_paths"]["designer_assistant_message"]).exists()
    assert "process_readable" not in state["output_paths"]
    assert "process_flow" not in state["output_paths"]
    report = json.loads(Path(state["output_paths"]["legacy_comparison_report"]).read_text(encoding="utf-8"))
    schema_report = json.loads(Path(state["output_paths"]["schema_validation_report"]).read_text(encoding="utf-8"))
    workflow_output = json.loads(Path(state["output_paths"]["workflow_design_output"]).read_text(encoding="utf-8"))
    assert report["missing_form_fields"] == []
    assert report["missing_flow_nodes"] == []
    assert report["missing_submit_paths"] == []
    # 候选与 gold 一致、业务校验未产出澄清 → gold 里的 blocking 待确认项应被标记为缺失
    assert {item["id"] for item in report["missing_required_clarifications"]} >= {
        "clarify_task_name_mapping",
    }
    assert schema_report["valid"] is True
    assert workflow_output["status"] == "draft_ready"
    assert Path(state["output_paths"]["process_def"]).parent.name == "process_extraction_agent"
    assert Path(state["output_paths"]["schema_validation_report"]).parent.name == "structural_validator"
    assert Path(state["output_paths"]["business_validation_report"]).parent.name == "business_validation_agent"
    assert Path(state["output_paths"]["legacy_comparison_report"]).parent.name == "legacy_eval"
    assert Path(state["output_paths"]["workflow_design_output"]).parent.name == "product"
    assert (tmp_path / "logs" / "source_ingestion" / "source_context.txt").exists()
    assert (tmp_path / "logs" / "source_ingestion" / "source_file_manifest.json").exists()
    assert (tmp_path / "output_manifest.json").exists()


def test_context_too_large_skips_process_extraction(tmp_path: Path, monkeypatch: Any) -> None:
    case_dir = tmp_path / "large_case"
    raw_dir = case_dir / "raw_sources"
    raw_dir.mkdir(parents=True)
    (raw_dir / "01_large.txt").write_text("员工请假申请。" * 200, encoding="utf-8")
    monkeypatch.setattr(process_v1, "MAX_SOURCE_CONTEXT_CHARS", 80)
    response = _standard_process_json("data/cases/EOA140_subsidiary_major_matter")
    extraction_agent = FakeExtractionAgent(response)

    state = process_v1.run_process_case(
        case_dir,
        tmp_path / "out",
        extraction_agent=extraction_agent,
        validation_agent=FakeBusinessValidationAgent(),
    )

    workflow_output = json.loads(Path(state["output_paths"]["workflow_design_output"]).read_text(encoding="utf-8"))
    assert extraction_agent.calls == 0
    assert state["context_error"]["type"] == "context_too_large"
    assert workflow_output["status"] == "context_too_large"
    assert workflow_output["validation"]["context_error"]["type"] == "context_too_large"


def test_workflow_does_not_use_standard_comparison_for_runtime_retry(tmp_path: Path) -> None:
    case_dir = Path("data/cases/EOA140_subsidiary_major_matter")
    data = copy.deepcopy(_standard_process_data(case_dir))
    data["form_fields"] = [field for field in data["form_fields"] if field["field_name"] != "联系电话"]
    extraction_agent = FakeExtractionAgent(json.dumps(data, ensure_ascii=False))

    state = run_process_case(
        case_dir,
        tmp_path,
        extraction_agent=extraction_agent,
        validation_agent=FakeBusinessValidationAgent(),
    )

    schema_report = json.loads(Path(state["output_paths"]["schema_validation_report"]).read_text(encoding="utf-8"))
    eval_report = json.loads(Path(state["output_paths"]["legacy_comparison_report"]).read_text(encoding="utf-8"))
    assert extraction_agent.calls == 1
    assert "comparison_report" not in extraction_agent.states[0]
    assert schema_report["valid"] is True
    assert eval_report["missing_form_fields"] == ["联系电话"]
    assert not (tmp_path / "logs" / "artifact_generation_agent").exists()


def test_workflow_writes_eval_missing_report_without_feeding_it_to_agent(tmp_path: Path) -> None:
    case_dir = Path("data/cases/EOA140_subsidiary_major_matter")
    data = copy.deepcopy(_standard_process_data(case_dir))
    data["form_fields"] = [field for field in data["form_fields"] if field["field_name"] != "联系电话"]
    extraction_agent = FakeExtractionAgent(json.dumps(data, ensure_ascii=False))

    state = run_process_case(
        case_dir,
        tmp_path,
        extraction_agent=extraction_agent,
        validation_agent=FakeBusinessValidationAgent(),
    )

    report = json.loads(Path(state["output_paths"]["legacy_comparison_report"]).read_text(encoding="utf-8"))
    markdown = Path(state["output_paths"]["legacy_missing_report"]).read_text(encoding="utf-8")
    assert extraction_agent.calls == 1
    assert report["missing_form_fields"] == ["联系电话"]
    assert "联系电话" in markdown
    assert not (tmp_path / "logs" / "artifact_generation_agent").exists()


def test_empty_form_fields_retries_extraction_and_generates_when_repaired(tmp_path: Path) -> None:
    case_dir = Path("data/cases/EOA140_subsidiary_major_matter")
    standard = _standard_process_json(case_dir)
    data = copy.deepcopy(_standard_process_data(case_dir))
    data["form_fields"] = []
    extraction_agent = FakeExtractionAgent([json.dumps(data, ensure_ascii=False), standard])
    validation_agent = FakeBusinessValidationAgent(
        [
            {
                "passed": False,
                "blocking_issues": [
                    {
                        "issue_type": "missing_form_fields",
                        "item": "form_fields",
                        "severity": "blocking",
                        "evidence_status": "found_in_raw",
                        "message": "候选输出表单字段为空，但 raw_sources 有字段清单",
                        "source_hint": "raw source text",
                        "repair_instruction": "补齐表单字段列表",
                    }
                ],
                "warning_issues": [],
                "raw_missing_items": [],
                "repair_instructions": ["补齐表单字段列表"],
                "summary": "need repair",
            },
            _passing_business_validation(),
        ]
    )

    state = run_process_case(
        case_dir,
        tmp_path,
        extraction_agent=extraction_agent,
        validation_agent=validation_agent,
    )

    first_business_report = validation_agent.result_responses[0]
    final_report = json.loads(Path(state["output_paths"]["schema_validation_report"]).read_text(encoding="utf-8"))
    assert extraction_agent.calls == 2
    assert extraction_agent.states[1]["validation_retry_count"] == 1
    assert validation_agent.calls == 2
    assert "form_fields" in extraction_agent.states[1]["validation_feedback"]
    assert first_business_report["blocking_issues"][0]["item"] == "form_fields"
    assert final_report["valid"] is True
    workflow_output = json.loads(Path(state["output_paths"]["workflow_design_output"]).read_text(encoding="utf-8"))
    assert workflow_output["status"] == "draft_ready"
    assert not (tmp_path / "logs" / "artifact_generation_agent").exists()


def test_empty_form_fields_after_retry_reports_missing_and_skips_artifacts(tmp_path: Path) -> None:
    case_dir = Path("data/cases/EOA140_subsidiary_major_matter")
    data = copy.deepcopy(_standard_process_data(case_dir))
    data["form_fields"] = []
    extraction_agent = FakeExtractionAgent(json.dumps(data, ensure_ascii=False))
    validation_agent = FakeBusinessValidationAgent(
        {
            "passed": False,
            "blocking_issues": [
                {
                    "issue_type": "missing_form_fields",
                    "item": "form_fields",
                    "severity": "blocking",
                    "evidence_status": "not_found_in_raw",
                    "message": "raw_sources 未提供表单字段清单，无法补齐",
                    "source_hint": None,
                    "repair_instruction": None,
                }
            ],
            "warning_issues": [],
            "raw_missing_items": [
                {
                    "issue_type": "missing_form_fields",
                    "item": "form_fields",
                    "severity": "blocking",
                    "evidence_status": "not_found_in_raw",
                    "message": "raw_sources 未提供表单字段清单，无法补齐",
                    "source_hint": None,
                    "repair_instruction": None,
                }
            ],
            "user_clarification_requests": [],
            "repair_instructions": [],
            "summary": "raw missing",
        }
    )

    state = run_process_case(
        case_dir,
        tmp_path,
        extraction_agent=extraction_agent,
        validation_agent=validation_agent,
    )

    op = state["output_paths"]
    schema_report = json.loads(Path(op["schema_validation_report"]).read_text(encoding="utf-8"))
    business_report = json.loads(Path(op["business_validation_report"]).read_text(encoding="utf-8"))
    eval_report = json.loads(Path(op["legacy_comparison_report"]).read_text(encoding="utf-8"))
    workflow_output = json.loads(Path(op["workflow_design_output"]).read_text(encoding="utf-8"))
    clarifications = json.loads(Path(op["user_clarification_requests"]).read_text(encoding="utf-8"))
    assistant_message = json.loads(Path(op["designer_assistant_message"]).read_text(encoding="utf-8"))
    markdown = Path(op["legacy_missing_report"]).read_text(encoding="utf-8")

    assert extraction_agent.calls == 1
    assert validation_agent.calls == 1
    assert schema_report["valid"] is True
    assert business_report["blocking_issues"][0]["evidence_status"] == "not_found_in_raw"
    assert "标题" in eval_report["missing_form_fields"]
    assert "raw_sources 未提供表单字段清单" in markdown
    assert workflow_output["status"] == "blocked_missing_source"
    assert clarifications
    assert "当前草稿包含：" in assistant_message["content"]
    assert "建议先处理第 1 个" in assistant_message["content"]
    assert assistant_message["clarification_cards"][0]["recommendation"]
    assert set(assistant_message["clarification_cards"][0]["options"][0]) == {"label", "value"}
    assert not (tmp_path / "logs" / "artifact_generation_agent").exists()


def test_clarification_options_are_deduplicated_before_designer_output(tmp_path: Path) -> None:
    case_dir = Path("data/cases/EOA140_subsidiary_major_matter")
    response = _standard_process_json(case_dir)
    duplicate_label = "source 中李梅和部门主任说3天，张总助说5天，会议纪要标注为待确认，候选流程暂用3天，需业务方最终拍板。"
    validation_agent = FakeBusinessValidationAgent(
        {
            "passed": True,
            "blocking_issues": [],
            "warning_issues": [],
            "raw_missing_items": [],
            "user_clarification_requests": [
                {
                    "id": "leave_days_threshold",
                    "question": "总经理审批的请假天数门槛，请确认最终采用哪个口径？",
                    "reason": "source 里存在 3 天和 5 天两种口径。",
                    "severity": "warning",
                    "issue_type": "threshold_conflict",
                    "item": "总经理审批门槛",
                    "options": [
                        duplicate_label,
                        duplicate_label,
                        "请假天数 > 3 天触发总经理审批",
                    ],
                    "free_text_allowed": True,
                    "related_items": ["dept_gm"],
                    "source_hint": "05_会议纪要_0428.txt / src_005_c001",
                }
            ],
            "repair_instructions": [],
            "summary": "threshold warning",
        }
    )

    state = run_process_case(
        case_dir,
        tmp_path,
        extraction_agent=FakeExtractionAgent(response),
        validation_agent=validation_agent,
    )

    assistant_message = json.loads(
        Path(state["output_paths"]["designer_assistant_message"]).read_text(encoding="utf-8")
    )
    labels = [option["label"] for option in assistant_message["clarification_cards"][0]["options"]]
    assert labels.count(duplicate_label) == 1
    assert len(labels) == len(set(labels))


def test_workflow_with_invalid_extraction_reports_schema_errors_and_skips_artifacts(tmp_path: Path) -> None:
    case_dir = Path("data/cases/EOA140_subsidiary_major_matter")
    extraction_agent = FakeExtractionAgent('{"meta": {}}')

    run_process_case(
        case_dir,
        tmp_path,
        extraction_agent=extraction_agent,
        validation_agent=FakeBusinessValidationAgent(),
    )

    report = json.loads((tmp_path / "logs" / "structural_validator" / "schema_validation_report.json").read_text(encoding="utf-8"))
    workflow_output = json.loads((tmp_path / "logs" / "workflow_design_output_writer" / "workflow_design_output.json").read_text(encoding="utf-8"))
    assert extraction_agent.calls == 2
    assert report["schema_errors"]
    assert "Schema 校验错误" in (tmp_path / "logs" / "legacy_eval" / "missing_report.md").read_text(encoding="utf-8")
    assert workflow_output["status"] == "failed_schema_validation"
    assert not (tmp_path / "logs" / "artifact_generation_agent").exists()


def test_verbose_mode_prints_validation_route(tmp_path: Path, capsys) -> None:
    case_dir = Path("data/cases/EOA140_subsidiary_major_matter")
    response = _standard_process_json(case_dir)

    run_process_case(
        case_dir,
        tmp_path,
        extraction_agent=FakeExtractionAgent(response),
        validation_agent=FakeBusinessValidationAgent(),
        verbose=True,
    )

    captured = capsys.readouterr()
    assert "[business_validation_agent] route=write_outputs" in captured.out
