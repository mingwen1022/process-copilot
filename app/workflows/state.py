from __future__ import annotations

from typing import Any, NotRequired, TypedDict

from data.schema import ProcessDefinition


class WorkflowState(TypedDict):
    case_dir: str
    out_dir: str
    verbose: NotRequired[bool]
    case_id: NotRequired[str]
    session_id: NotRequired[str]
    process_domain_hint: NotRequired[str]
    raw_sources: NotRequired[list[dict[str, str]]]
    source_package_ref: NotRequired[dict[str, Any]]
    uploaded_source_manifest: NotRequired[list[dict[str, Any]]]
    source_file_manifest: NotRequired[list[dict[str, Any]]]
    source_chunks: NotRequired[list[dict[str, Any]]]
    ingestion_warnings: NotRequired[list[dict[str, Any]]]
    source_repair_required: NotRequired[bool]
    source_repair_attempt_count: NotRequired[int]
    source_repair_report: NotRequired[dict[str, Any]]
    repaired_sources: NotRequired[list[dict[str, Any]]]
    parser_improvement_suggestions: NotRequired[list[dict[str, Any]]]
    chunk_catalog: NotRequired[list[dict[str, Any]]]
    evidence_index: NotRequired[dict[str, Any]]
    source_quality_findings: NotRequired[list[dict[str, Any]]]
    source_index: NotRequired[dict[str, Any]]
    selected_seed_chunks: NotRequired[list[str]]
    source_catalog_context: NotRequired[str]
    source_context: NotRequired[str]
    context_budget_report: NotRequired[dict[str, Any]]
    context_error: NotRequired[dict[str, Any]]
    candidate_process: NotRequired[ProcessDefinition | None]
    llm_raw_output: NotRequired[str]
    schema_errors: NotRequired[list[dict[str, Any]]]
    convention_warnings: NotRequired[list[dict[str, Any]]]
    schema_validation_report: NotRequired[dict[str, Any]]
    structural_validation_route: NotRequired[str]
    business_validation_result: NotRequired[dict[str, Any]]
    business_validation_raw_output: NotRequired[str]
    user_clarification_requests: NotRequired[list[dict[str, Any]]]
    validation_feedback: NotRequired[str]
    should_retry_extraction: NotRequired[bool]
    validation_retry_count: NotRequired[int]
    max_validation_retries: NotRequired[int]
    workflow_design_output: NotRequired[dict[str, Any]]
    designer_assistant_message: NotRequired[dict[str, Any]]
    design_persistence_report: NotRequired[dict[str, Any]]
    output_paths: NotRequired[dict[str, str]]
