from __future__ import annotations

import argparse
import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any

from langgraph.config import get_stream_writer
from langgraph.graph import END, StateGraph
from pydantic import ValidationError

from app.agents.business_validation import BusinessValidationAgent
from app.agents.process_extraction import ProcessExtractionAgent
from app.io_utils import (
    extract_json_object,
    find_standard_json,
    is_process_definition_json,
    load_process_definition,
    load_standard_target,
    write_json,
    write_process_json,
)
from app.process_conventions import validate_process_conventions
from app.reporting.comparison import compare_processes, merge_clarification_eval, render_missing_report
from app.run_layout import (
    ensure_run_layout,
    eval_dir as run_eval_dir,
    legacy_eval_dir,
    logs_dir,
    node_log_dir,
    product_dir,
    source_ingestion_dir,
    write_run_meta,
)
from app.tracing import write_trace
from app.tools.source_parsers import parse_source_file
from app.workflows.state import WorkflowState
from data.schema import ProcessDefinition

MAX_SOURCE_CONTEXT_CHARS = 120_000


def run_process_case(
    case_dir: str | Path,
    out_dir: str | Path,
    *,
    model: Any | None = None,
    extraction_agent: Any | None = None,
    validation_agent: Any | None = None,
    image_transcriber: Any | None = None,
    max_validation_retries: int = 1,
    verbose: bool = False,
) -> dict[str, Any]:
    ensure_run_layout(out_dir)
    write_run_meta(out_dir, case_id=Path(case_dir).name, case_dir=case_dir, status="running")
    graph = build_graph(
        model=model,
        extraction_agent=extraction_agent,
        validation_agent=validation_agent,
        image_transcriber=image_transcriber,
    )
    initial_state: WorkflowState = {
        "case_dir": str(case_dir),
        "out_dir": str(out_dir),
        "verbose": verbose,
        "validation_retry_count": 0,
        "max_validation_retries": max_validation_retries,
    }
    return graph.invoke(initial_state)


def run_process_case_stream(
    case_dir: str | Path,
    out_dir: str | Path,
    *,
    model: Any | None = None,
    extraction_agent: Any | None = None,
    validation_agent: Any | None = None,
    image_transcriber: Any | None = None,
    max_validation_retries: int = 1,
    verbose: bool = False,
    session_id: str | None = None,
    event_callback: Any | None = None,
) -> dict[str, Any]:
    ensure_run_layout(out_dir)
    write_run_meta(
        out_dir,
        case_id=Path(case_dir).name,
        case_dir=case_dir,
        status="running",
        extra={"session_id": session_id},
    )
    graph = build_graph(
        model=model,
        extraction_agent=extraction_agent,
        validation_agent=validation_agent,
        image_transcriber=image_transcriber,
    )
    current_state: dict[str, Any] = {
        "case_dir": str(case_dir),
        "out_dir": str(out_dir),
        "verbose": verbose,
        "validation_retry_count": 0,
        "max_validation_retries": max_validation_retries,
    }
    if session_id:
        current_state["session_id"] = session_id
    graph_events_path = logs_dir(out_dir) / "graph_events.jsonl"

    def emit_event(event: dict[str, Any]) -> None:
        graph_events_path.parent.mkdir(parents=True, exist_ok=True)
        with graph_events_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(event, ensure_ascii=False) + "\n")
        if event_callback:
            event_callback(event)

    for part in graph.stream(current_state, stream_mode=["updates", "custom"], version="v2"):
        if isinstance(part, tuple) and len(part) == 2:
            part_type, data = part
        elif isinstance(part, dict) and "type" in part and "data" in part:
            part_type = part.get("type")
            data = part.get("data")
        else:
            part_type = "updates"
            data = part
        if part_type == "custom":
            if isinstance(data, dict):
                emit_event({"event_type": "custom", **data})
            continue
        if part_type != "updates" or not isinstance(data, dict):
            continue
        for node_name, update in data.items():
            if isinstance(update, dict):
                current_state.update(update)
            emit_event(
                {
                    "event_type": "node_done",
                    "kind": "node_status",
                    "node": node_name,
                    "status": "done",
                    "message": _node_done_message(node_name, update),
                }
            )
    return current_state


def build_graph(
    model: Any | None = None,
    *,
    extraction_agent: Any | None = None,
    validation_agent: Any | None = None,
    image_transcriber: Any | None = None,
):
    process_agent = extraction_agent or ProcessExtractionAgent(model=model)
    validation_node = validation_agent or BusinessValidationAgent(model=model)

    def _source_file_loader(state: WorkflowState) -> WorkflowState:
        return source_file_loader(state, image_transcriber=image_transcriber)

    graph = StateGraph(WorkflowState)
    graph.add_node("source_file_loader", _progress_node("source_file_loader", _source_file_loader))
    graph.add_node("source_context_builder", _progress_node("source_context_builder", source_context_builder))
    graph.add_node("process_extraction_agent", _progress_node("process_extraction_agent", process_agent.run))
    graph.add_node("structural_validator", _progress_node("structural_validator", validate_structure))
    graph.add_node(
        "business_validation_agent",
        _progress_node("business_validation_agent", _business_validation_node(validation_node)),
    )
    graph.add_node(
        "workflow_design_output_writer",
        _progress_node("workflow_design_output_writer", workflow_design_output_writer),
    )

    graph.set_entry_point("source_file_loader")
    graph.add_edge("source_file_loader", "source_context_builder")
    graph.add_conditional_edges(
        "source_context_builder",
        route_after_source_context_builder,
        {
            "process_extraction": "process_extraction_agent",
            "write_outputs": "workflow_design_output_writer",
        },
    )
    graph.add_edge("process_extraction_agent", "structural_validator")
    graph.add_conditional_edges(
        "structural_validator",
        route_after_structural_validation,
        {
            "retry_extraction": "process_extraction_agent",
            "business_validation": "business_validation_agent",
            "write_outputs": "workflow_design_output_writer",
        },
    )
    graph.add_conditional_edges(
        "business_validation_agent",
        route_after_business_validation,
        {
            "retry_extraction": "process_extraction_agent",
            "write_outputs": "workflow_design_output_writer",
        },
    )
    graph.add_edge("workflow_design_output_writer", END)
    return graph.compile()


def route_after_source_context_builder(state: WorkflowState) -> str:
    if state.get("context_error"):
        return "write_outputs"
    return "process_extraction"


def _progress_node(node_name: str, fn: Any):
    def node(state: WorkflowState) -> WorkflowState:
        _emit_progress(node=node_name, status="running", message=_node_start_message(node_name))
        try:
            result = fn(state)
        except Exception as exc:
            _emit_progress(node=node_name, status="failed", message=f"{_node_label(node_name)}执行失败：{exc}")
            raise
        _emit_progress(node=node_name, status="done", message=_node_done_message(node_name, result))
        return result

    return node


def _emit_progress(*, node: str, status: str, message: str, **extra: Any) -> None:
    try:
        writer = get_stream_writer()
    except Exception:
        return
    payload = {
        "kind": "node_step",
        "node": node,
        "status": status,
        "message": message,
        **extra,
    }
    try:
        writer(payload)
    except Exception:
        return


def _node_label(node_name: str) -> str:
    return {
        "source_file_loader": "读取输入",
        "source_context_builder": "构建 source context",
        "process_extraction_agent": "抽取流程定义",
        "structural_validator": "结构校验",
        "business_validation_agent": "业务缺口校验",
        "workflow_design_output_writer": "写入设计草稿",
    }.get(node_name, node_name)


def _node_start_message(node_name: str) -> str:
    return {
        "source_file_loader": "开始读取 source 文件和补充说明",
        "source_context_builder": "开始构建 source catalog 和上下文",
        "process_extraction_agent": "开始调用 ProcessExtractionAgent 生成流程草稿",
        "structural_validator": "开始校验 schema、字段引用和路径目标",
        "business_validation_agent": "开始检查业务缺口和待确认事项",
        "workflow_design_output_writer": "开始写入设计草稿输出",
    }.get(node_name, f"开始执行 {node_name}")


def _node_done_message(node_name: str, update: Any) -> str:
    if node_name == "source_file_loader" and isinstance(update, dict):
        warning_count = len(update.get("ingestion_warnings", []))
        suffix = f"，{warning_count} 个解析提示" if warning_count else ""
        return f"已读取 {len(update.get('source_file_manifest', []))} 个 source 文件{suffix}"
    if node_name == "source_context_builder" and isinstance(update, dict):
        report = update.get("context_budget_report") or {}
        suffix = "，已按上下文预算截断" if report.get("truncated") else ""
        return f"已构建 {len(update.get('chunk_catalog', []))} 个 source chunk{suffix}"
    if node_name == "process_extraction_agent" and isinstance(update, dict):
        process = update.get("candidate_process")
        if process is not None:
            return (
                f"已生成 {len(process.form_fields)} 个字段、"
                f"{len(process.flow_nodes)} 个环节、"
                f"{sum(len(node.submit_paths) for node in process.flow_nodes)} 条路径"
            )
    if node_name == "structural_validator" and isinstance(update, dict):
        report = update.get("schema_validation_report", {})
        return "结构校验通过" if report.get("valid") else f"结构校验发现 {len(report.get('schema_errors', []))} 个问题"
    if node_name == "business_validation_agent" and isinstance(update, dict):
        result = update.get("business_validation_result", {})
        return (
            f"业务校验完成：{len(result.get('blocking_issues', []))} 个阻断项、"
            f"{len(result.get('warning_issues', []))} 个提示项、"
            f"{len(result.get('user_clarification_requests', []))} 个待确认项"
        )
    if node_name == "workflow_design_output_writer":
        return "设计草稿输出已写入"
    return f"{_node_label(node_name)}已完成"


def _business_validation_node(validation_agent: Any):
    def node(state: WorkflowState) -> WorkflowState:
        return finalize_business_validation(validation_agent.run(state))

    return node


def _load_uploaded_source_manifest(case_dir: Path) -> list[dict[str, Any]]:
    manifest_path = case_dir / "uploaded_source_manifest.json"
    if not manifest_path.exists():
        return []
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    if isinstance(payload, dict):
        sources = payload.get("sources", [])
    else:
        sources = payload
    if not isinstance(sources, list):
        raise ValueError(f"uploaded source manifest must be a list: {manifest_path}")
    normalized: list[dict[str, Any]] = []
    for index, source in enumerate(sources, start=1):
        if not isinstance(source, dict):
            continue
        storage_path = source.get("storage_path") or source.get("path")
        if not storage_path:
            continue
        path = Path(storage_path)
        if not path.is_absolute():
            path = case_dir / path
        normalized.append(
            {
                "source_id": source.get("source_id") or f"upload_{index:03d}",
                "title": source.get("title") or source.get("original_filename") or path.name,
                "mime_type": source.get("mime_type"),
                "size_bytes": source.get("size_bytes"),
                "path": str(path),
                "storage_path": str(path),
                "original_filename": source.get("original_filename") or path.name,
                "source_type": source.get("source_type"),
            }
        )
    return normalized


def _source_entries_from_case(case_dir: Path, uploaded_manifest: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if uploaded_manifest:
        return uploaded_manifest
    raw_dir = _fallback_source_dir(case_dir)
    if not raw_dir.is_dir():
        raise FileNotFoundError(f"source directory not found: {raw_dir}")
    entries: list[dict[str, Any]] = []
    internal_source_files = {"source_manifest.json", "uploaded_source_manifest.json", "input_manifest.json"}
    for path in sorted(
        item
        for item in raw_dir.iterdir()
        if item.is_file() and not item.name.startswith(".") and item.name not in internal_source_files
    ):
        entries.append(
            {
                "source_id": f"raw_{len(entries) + 1:03d}",
                "title": path.name,
                "mime_type": None,
                "size_bytes": path.stat().st_size,
                "path": str(path),
                "storage_path": str(path),
                "original_filename": path.name,
                "source_type": path.suffix.removeprefix(".") or "file",
            }
        )
    return entries


def _fallback_source_dir(case_dir: Path) -> Path:
    for name in ("raw_sources2", "raw_sources"):
        source_dir = case_dir / name
        if source_dir.is_dir():
            return source_dir
    return case_dir / "raw_sources"


def _apply_vision_transcription(
    *,
    source_path: Path,
    source_id: str,
    file_name: str,
    chunks: list[dict[str, Any]],
    warnings: list[dict[str, Any]],
    transcriber: Any,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """对触发 requires_vision_ocr 的图片 source 做视觉转录，转录文本作为 chunk 进上下文。

    只做忠实转录（理解/抽取留给下游 agent）；转录失败或无可读内容时保留原 warning、不阻断。
    """
    vision_warning = next((w for w in warnings if w.get("warning_type") == "requires_vision_ocr"), None)
    if vision_warning is None:
        return chunks, warnings
    try:
        text = transcriber(source_path, "image")
    except Exception as exc:  # noqa: BLE001 - 视觉转录失败降级为原 warning，不阻断整包
        note = {**vision_warning, "message": f"视觉转录失败，降级为未读取：{exc}"}
        return chunks, [w for w in warnings if w is not vision_warning] + [note]
    if not text or "[无法辨认" in text:
        return chunks, warnings
    vision_chunk = {
        "chunk_id": f"{source_id}_vision",
        "source_id": source_id,
        "source_file": file_name,
        "source_type": "image",
        "location": {"kind": "image_vision"},
        "text": f"[图片来源：{file_name}（视觉转录）]\n{text}",
        "char_count": len(text),
        "metadata": {"vision_transcribed": True},
    }
    transcribed_warning = {
        **vision_warning,
        "warning_type": "vision_transcribed",
        "level": "info",
        "message": "图片已由视觉模型转录进入上下文（来源标注为截图/扫描件，供参考类材料仍受线下参考护栏约束）。",
    }
    new_warnings = [w for w in warnings if w is not vision_warning] + [transcribed_warning]
    return chunks + [vision_chunk], new_warnings


def source_file_loader(state: WorkflowState, *, image_transcriber: Any | None = None) -> WorkflowState:
    case_dir = Path(state["case_dir"])
    out_dir = Path(state["out_dir"])
    node_dir = source_ingestion_dir(out_dir)
    node_dir.mkdir(parents=True, exist_ok=True)
    uploaded_manifest = _load_uploaded_source_manifest(case_dir)
    source_entries = _source_entries_from_case(case_dir, uploaded_manifest)
    source_file_manifest: list[dict[str, Any]] = []
    source_chunks: list[dict[str, Any]] = []
    ingestion_warnings: list[dict[str, Any]] = []

    _emit_progress(
        node="source_file_loader",
        status="running",
        message=f"发现 {len(source_entries)} 个 source 文件",
        current=0,
        total=len(source_entries),
    )
    raw_sources_for_compat: list[dict[str, str]] = []
    for index, source in enumerate(source_entries, start=1):
        source_path = Path(source["path"])
        _emit_progress(
            node="source_file_loader",
            status="running",
            message=f"正在读取 {source_path.name}",
            current=index,
            total=len(source_entries),
        )
        source_id = f"src_{index:03d}"
        parsed = parse_source_file(
            source_path,
            source_id=source_id,
            title=source.get("title") or source_path.name,
            mime_type=source.get("mime_type"),
        )
        chunks = list(parsed.chunks)
        warnings = list(parsed.warnings)
        manifest = dict(parsed.manifest)
        if image_transcriber is not None:
            chunks, warnings = _apply_vision_transcription(
                source_path=source_path,
                source_id=source_id,
                file_name=source.get("title") or source_path.name,
                chunks=chunks,
                warnings=warnings,
                transcriber=image_transcriber,
            )
            manifest["chunk_count"] = len(chunks)
            manifest["chunk_ids"] = [chunk["chunk_id"] for chunk in chunks]
            manifest["warnings"] = warnings
        source_file_manifest.append(manifest)
        source_chunks.extend(chunks)
        ingestion_warnings.extend(warnings)
        for warning in warnings:
            _emit_progress(
                node="source_file_loader",
                status="warning",
                message=f"{warning.get('source_file')}: {warning.get('message')}",
                warning_type=warning.get("warning_type"),
                source_file=warning.get("source_file"),
            )
        combined_text = "\n\n".join(str(chunk.get("text") or "") for chunk in chunks).strip()
        raw_sources_for_compat.append({"path": str(source_path), "text": combined_text})

    package_ref = {
        "package_id": case_dir.name,
        "root": str(case_dir),
        "source_count": len(source_file_manifest),
        "chunk_count": len(source_chunks),
        "warning_count": len(ingestion_warnings),
    }
    source_repair_required = any(warning.get("level") == "blocking" for warning in ingestion_warnings)
    source_repair_report = {
        "status": "pending" if source_repair_required else "skipped",
        "reason": "source parser produced blocking warnings" if source_repair_required else "source parser produced no blocking warnings",
    }
    parser_improvement_suggestions: list[dict[str, Any]] = []

    write_json(package_ref, node_dir / "source_package_ref.json")
    write_json(uploaded_manifest, node_dir / "uploaded_source_manifest.json")
    write_json(source_file_manifest, node_dir / "source_file_manifest.json")
    write_json(ingestion_warnings, node_dir / "ingestion_warnings.json")
    write_json(source_repair_report, node_dir / "source_repair_report.json")
    write_json(parser_improvement_suggestions, node_dir / "parser_improvement_suggestions.json")
    (node_dir / "source_chunks.jsonl").write_text(
        "\n".join(json.dumps(chunk, ensure_ascii=False) for chunk in source_chunks) + ("\n" if source_chunks else ""),
        encoding="utf-8",
    )
    write_json(
        raw_sources_for_compat,
        node_dir / "raw_sources.json",
    )
    return {
        **state,
        "case_id": case_dir.name,
        "source_package_ref": package_ref,
        "uploaded_source_manifest": uploaded_manifest,
        "source_file_manifest": source_file_manifest,
        "source_chunks": source_chunks,
        "ingestion_warnings": ingestion_warnings,
        "source_repair_required": source_repair_required,
        "source_repair_report": source_repair_report,
        "parser_improvement_suggestions": parser_improvement_suggestions,
        "raw_sources": raw_sources_for_compat,
        "verbose": state.get("verbose", False),
        "validation_retry_count": state.get("validation_retry_count", 0),
        "max_validation_retries": state.get("max_validation_retries", 1),
    }


def source_context_builder(state: WorkflowState) -> WorkflowState:
    out_dir = Path(state["out_dir"])
    node_dir = source_ingestion_dir(out_dir)
    node_dir.mkdir(parents=True, exist_ok=True)
    chunks = state.get("source_chunks", [])
    _emit_progress(
        node="source_context_builder",
        status="running",
        message=f"正在整理 {len(chunks)} 个 source chunk",
        current=0,
        total=len(chunks),
    )
    catalog_items: list[str] = []
    context_items: list[str] = []
    source_index: dict[str, Any] = {}
    selected_seed_chunks: list[str] = []
    skipped_chunks: list[str] = []
    context_char_count = 0

    for chunk in chunks:
        chunk_id = str(chunk["chunk_id"])
        text = str(chunk.get("text") or "").strip()
        preview = " ".join(text.split())[:180]
        catalog_items.append(
            f"- {chunk_id} | {chunk.get('source_file')} | {chunk.get('source_type')} | {preview}"
        )
        source_index[chunk_id] = {
            "source_id": chunk.get("source_id"),
            "source_file": chunk.get("source_file"),
            "source_type": chunk.get("source_type"),
            "location": chunk.get("location"),
            "char_count": chunk.get("char_count"),
        }
        if not text:
            continue
        context_block = "## 来源文件：" f"{chunk.get('source_file')} ({chunk_id})\n\n{text}"
        if context_char_count + len(context_block) > MAX_SOURCE_CONTEXT_CHARS:
            skipped_chunks.append(chunk_id)
            continue
        _emit_progress(
            node="source_context_builder",
            status="running",
            message=f"正在加入 {chunk.get('source_file')} 到 source context",
            current=len(selected_seed_chunks) + 1,
            total=len(chunks),
        )
        selected_seed_chunks.append(chunk_id)
        context_items.append(context_block)
        context_char_count += len(context_block)

    source_catalog_context = "Source Catalog:\n" + "\n".join(catalog_items)
    source_context = (
        f"{source_catalog_context}\n\n---\n\nSource Chunks 原文：\n\n"
        + "\n\n---\n\n".join(context_items)
    )
    warning_lines = [
        f"- {warning.get('source_file')}: {warning.get('message')}"
        for warning in state.get("ingestion_warnings", [])
    ]
    if warning_lines:
        source_context += "\n\n---\n\nSource Ingestion Warnings:\n" + "\n".join(warning_lines)

    chunk_catalog = [
        {
            "chunk_id": chunk.get("chunk_id"),
            "source_file": chunk.get("source_file"),
            "summary": " ".join(str(chunk.get("text") or "").split())[:180],
            "tags": [],
            "candidate_elements": [],
        }
        for chunk in chunks
    ]
    evidence_index: dict[str, Any] = {}
    source_quality_findings: list[dict[str, Any]] = []
    context_budget_report = {
        "max_chars": MAX_SOURCE_CONTEXT_CHARS,
        "selected_char_count": context_char_count,
        "selected_chunk_count": len(selected_seed_chunks),
        "skipped_chunk_count": len(skipped_chunks),
        "skipped_chunk_ids": skipped_chunks,
        "truncated": bool(skipped_chunks),
    }
    context_error = None
    if skipped_chunks:
        context_error = {
            "type": "context_too_large",
            "message": (
                f"Source context 超出 {MAX_SOURCE_CONTEXT_CHARS} 字符预算，"
                f"已跳过 {len(skipped_chunks)} 个 chunk；本次不调用 Bedrock 抽取。"
            ),
            "skipped_chunk_ids": skipped_chunks,
            "max_chars": MAX_SOURCE_CONTEXT_CHARS,
            "selected_char_count": context_char_count,
        }
        _emit_progress(
            node="source_context_builder",
            status="warning",
            message=context_error["message"],
            error_type="context_too_large",
        )

    (node_dir / "source_catalog_context.txt").write_text(source_catalog_context, encoding="utf-8")
    (node_dir / "source_context.txt").write_text(source_context, encoding="utf-8")
    write_json(selected_seed_chunks, node_dir / "selected_seed_chunks.json")
    write_json(source_index, node_dir / "source_index.json")
    write_json(chunk_catalog, node_dir / "chunk_catalog.json")
    write_json(evidence_index, node_dir / "evidence_index.json")
    write_json(source_quality_findings, node_dir / "source_quality_findings.json")
    write_json(context_budget_report, node_dir / "context_budget_report.json")
    if context_error:
        write_json(context_error, node_dir / "context_error.json")

    return {
        **state,
        "source_catalog_context": source_catalog_context,
        "source_context": source_context,
        "source_index": source_index,
        "selected_seed_chunks": selected_seed_chunks,
        "chunk_catalog": chunk_catalog,
        "evidence_index": evidence_index,
        "source_quality_findings": source_quality_findings,
        "context_budget_report": context_budget_report,
        "context_error": context_error,
    }


def validate_structure(state: WorkflowState) -> WorkflowState:
    schema_errors = state.get("schema_errors")
    convention_warnings: list[dict[str, Any]] = []
    actual = state.get("candidate_process")
    _emit_progress(node="structural_validator", status="running", message="正在检查候选流程 schema")

    if actual is None and schema_errors:
        return _finalize_structural_validation(
            {
                **state,
                "candidate_process": None,
                "schema_errors": schema_errors,
            },
            schema_errors=schema_errors,
        )

    if actual is None:
        try:
            payload = extract_json_object(state.get("llm_raw_output", ""))
            actual = ProcessDefinition.model_validate(payload)
        except ValidationError as exc:
            schema_errors = _validation_errors(exc)
            return _finalize_structural_validation(
                {
                    **state,
                    "candidate_process": None,
                    "schema_errors": schema_errors,
                },
                schema_errors=schema_errors,
            )
        except ValueError as exc:
            schema_errors = [{"loc": ["llm_output"], "msg": str(exc), "type": "json_parse_error"}]
            return _finalize_structural_validation(
                {
                    **state,
                    "candidate_process": None,
                    "schema_errors": schema_errors,
                },
                schema_errors=schema_errors,
            )

    schema_errors = _validate_process_references(actual)
    if not schema_errors:
        convention_warnings = validate_process_conventions(actual)
    _emit_progress(
        node="structural_validator",
        status="running",
        message="正在检查字段、环节和提交路径引用",
    )
    return _finalize_structural_validation(
        {
            **state,
            "candidate_process": actual,
            "schema_errors": schema_errors,
            "convention_warnings": convention_warnings,
        },
        schema_errors=schema_errors,
        convention_warnings=convention_warnings,
    )


def route_after_structural_validation(state: WorkflowState) -> str:
    route = _compute_structural_route(state)
    if state.get("verbose"):
        report = state.get("schema_validation_report", {})
        print(
            "[structural_validator] "
            f"route={route} "
            f"schema_valid={report.get('valid')} "
            f"schema_errors={len(report.get('schema_errors', []))}"
        )
    return route


def route_after_business_validation(state: WorkflowState) -> str:
    route = _compute_business_route(state)
    if state.get("verbose"):
        result = state.get("business_validation_result", {})
        print(
            "[business_validation_agent] "
            f"route={route} "
            f"passed={result.get('passed')} "
            f"blocking={len(result.get('blocking_issues', []))} "
            f"warnings={len(result.get('warning_issues', []))}"
        )
    return route


def _compute_structural_route(state: WorkflowState) -> str:
    if state.get("should_retry_extraction"):
        return "retry_extraction"
    if state.get("schema_validation_report", {}).get("valid"):
        return "business_validation"
    return "write_outputs"


def _compute_business_route(state: WorkflowState) -> str:
    if state.get("should_retry_extraction"):
        return "retry_extraction"
    return "write_outputs"


def _finalize_structural_validation(
    state: WorkflowState,
    *,
    schema_errors: list[dict[str, Any]],
    convention_warnings: list[dict[str, Any]] | None = None,
) -> WorkflowState:
    current_attempt = state.get("validation_retry_count", 0)
    should_retry = bool(schema_errors) and current_attempt < state.get("max_validation_retries", 1)
    report = _schema_validation_report(schema_errors, convention_warnings=convention_warnings)
    feedback = _render_structural_feedback(report, previous_output=state.get("llm_raw_output", ""))
    routed_state: WorkflowState = {
        **state,
        "schema_errors": schema_errors,
        "convention_warnings": convention_warnings or [],
        "schema_validation_report": report,
        "validation_feedback": feedback,
        "should_retry_extraction": should_retry,
    }
    if schema_errors:
        routed_state["business_validation_result"] = {}
        routed_state["business_validation_raw_output"] = ""
    if should_retry:
        routed_state["validation_retry_count"] = current_attempt + 1
    return _persist_structural_validation_attempt(routed_state, attempt=current_attempt)


def finalize_business_validation(state: WorkflowState) -> WorkflowState:
    current_attempt = state.get("validation_retry_count", 0)
    result = state.get("business_validation_result") or {
        "passed": False,
        "blocking_issues": [
            {
                "issue_type": "missing_business_validation_result",
                "item": "business_validation_result",
                "severity": "blocking",
                "evidence_status": "uncertain",
                "message": "business_validation_agent 没有返回结构化结果",
            }
        ],
        "warning_issues": [],
        "raw_missing_items": [],
        "user_clarification_requests": [],
        "repair_instructions": [],
        "summary": "business_validation_result 缺失",
    }
    blocking_issues = result.get("blocking_issues", [])
    result = {**result, "passed": not bool(blocking_issues)}
    should_retry = _business_result_needs_retry(result) and current_attempt < state.get("max_validation_retries", 1)
    feedback = _render_business_feedback(result, previous_output=state.get("llm_raw_output", ""))
    clarification_requests = _normalize_user_clarification_requests(result)
    routed_state: WorkflowState = {
        **state,
        "business_validation_result": result,
        "validation_feedback": feedback,
        "should_retry_extraction": should_retry,
        "user_clarification_requests": clarification_requests,
    }
    if should_retry:
        routed_state["validation_retry_count"] = current_attempt + 1
    return _persist_business_validation_route(routed_state, attempt=current_attempt)


def _persist_structural_validation_attempt(state: WorkflowState, *, attempt: int) -> WorkflowState:
    node_dir = node_log_dir(state["out_dir"], "structural_validator")
    node_dir.mkdir(parents=True, exist_ok=True)
    write_json(state["schema_validation_report"], node_dir / f"schema_validation_report_attempt_{attempt}.json")
    write_trace(
        {
            "node": "structural_validator",
            "attempt": attempt,
            "route": _compute_structural_route(state),
            "valid": state.get("schema_validation_report", {}).get("valid"),
            "schema_error_count": len(state.get("schema_validation_report", {}).get("schema_errors", [])),
            "next_validation_retry_count": state.get("validation_retry_count", 0),
        },
        node_dir / f"route_attempt_{attempt}.json",
    )
    return state


def _persist_business_validation_route(state: WorkflowState, *, attempt: int) -> WorkflowState:
    node_dir = node_log_dir(state["out_dir"], "business_validation_agent")
    node_dir.mkdir(parents=True, exist_ok=True)
    write_trace(
        {
            "node": "business_validation_agent",
            "attempt": attempt,
            "route": _compute_business_route(state),
            "should_retry_extraction": state.get("should_retry_extraction"),
            "next_validation_retry_count": state.get("validation_retry_count", 0),
        },
        node_dir / f"route_attempt_{attempt}.json",
    )
    return state


def _schema_validation_report(
    schema_errors: list[dict[str, Any]],
    *,
    convention_warnings: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    convention_warnings = convention_warnings or []
    return {
        "valid": not schema_errors,
        "schema_valid": not schema_errors,
        "schema_errors": schema_errors,
        "convention_warnings": convention_warnings,
        "summary": {
            "schema_error_count": len(schema_errors),
            "convention_warning_count": len(convention_warnings),
        },
    }


def _render_structural_feedback(report: dict[str, Any], *, previous_output: str) -> str:
    if report.get("valid"):
        return ""
    lines = [
        "上一轮输出未通过结构校验。请重新检查 source material，并修复 ProcessDefinition 结构问题。",
        "",
    ]
    schema_errors = report.get("schema_errors", [])
    if schema_errors:
        lines.append("Schema/引用错误：")
        lines.extend(f"- {'.'.join(map(str, item.get('loc', [])))}: {item.get('msg')}" for item in schema_errors)
        lines.append("")
    if previous_output.strip():
        lines.append("上一轮输出：")
        lines.append(previous_output)
    return "\n".join(lines)


def _render_business_feedback(result: dict[str, Any], *, previous_output: str) -> str:
    blocking_issues = result.get("blocking_issues", [])
    if not blocking_issues:
        return ""
    lines = [
        "business_validation_agent 发现上一轮候选流程存在业务问题。请基于 source material 修复，不要编造原文中没有的信息。",
        "",
        "阻断问题：",
    ]
    for issue in blocking_issues:
        lines.append(
            "- "
            f"{issue.get('issue_type')}/{issue.get('item')} "
            f"evidence={issue.get('evidence_status')}: {issue.get('message')}"
        )
        if issue.get("source_hint"):
            lines.append(f"  source_hint: {issue.get('source_hint')}")
        if issue.get("repair_instruction"):
            lines.append(f"  repair_instruction: {issue.get('repair_instruction')}")
    repair_instructions = result.get("repair_instructions", [])
    if repair_instructions:
        lines.append("")
        lines.append("修复指令：")
        lines.extend(f"- {item}" for item in repair_instructions)
    if previous_output.strip():
        lines.append("")
        lines.append("上一轮输出：")
        lines.append(previous_output)
    return "\n".join(lines)


def _business_result_needs_retry(result: dict[str, Any]) -> bool:
    blocking_issues = result.get("blocking_issues", [])
    if not blocking_issues:
        return False
    return any(issue.get("evidence_status") == "found_in_raw" for issue in blocking_issues)


def _validation_errors(exc: ValidationError) -> list[dict[str, Any]]:
    return [
        {"loc": list(error.get("loc", [])), "msg": error.get("msg", ""), "type": error.get("type", "")}
        for error in exc.errors()
    ]


def _validate_process_references(process: ProcessDefinition) -> list[dict[str, Any]]:
    node_ids = {node.node_id for node in process.flow_nodes}
    allowed_targets = node_ids | {"DRAFT", "END"}
    errors: list[dict[str, Any]] = []

    for node in process.flow_nodes:
        for index, path in enumerate(node.submit_paths):
            if path.target_node_id not in allowed_targets:
                errors.append(
                    {
                        "loc": ["flow_nodes", node.node_id, "submit_paths", index, "target_node_id"],
                        "msg": f"unknown target_node_id: {path.target_node_id}",
                        "type": "invalid_reference",
                    }
                )

    allowed_stages = node_ids | {"all"}
    for index, field in enumerate(process.form_fields):
        for attr in ("required_stages", "visible_stages", "editable_stages"):
            for stage in getattr(field, attr):
                if stage not in allowed_stages:
                    errors.append(
                        {
                            "loc": ["form_fields", index, attr],
                            "msg": f"unknown stage reference: {stage}",
                            "type": "invalid_reference",
                        }
                    )
    for index, attachment in enumerate(process.attachments or []):
        for attr in ("upload_stages", "required_stages"):
            for stage in getattr(attachment, attr):
                if stage not in allowed_stages:
                    errors.append(
                        {
                            "loc": ["attachments", index, attr],
                            "msg": f"unknown stage reference: {stage}",
                            "type": "invalid_reference",
                        }
                    )
    return errors


def workflow_design_output_writer(state: WorkflowState) -> WorkflowState:
    out_dir = Path(state["out_dir"])
    layout = ensure_run_layout(out_dir)
    _emit_progress(node="workflow_design_output_writer", status="running", message="正在汇总流程草稿和校验结果")
    process_dir = node_log_dir(out_dir, "process_extraction_agent")
    validate_dir = node_log_dir(out_dir, "structural_validator")
    business_validate_dir = node_log_dir(out_dir, "business_validation_agent")
    output_dir = node_log_dir(out_dir, "workflow_design_output_writer")
    product_output_dir = product_dir(out_dir)
    eval_output_dir = run_eval_dir(out_dir)
    legacy_output_dir = legacy_eval_dir(out_dir)
    for path in (process_dir, validate_dir, business_validate_dir, output_dir, product_output_dir, eval_output_dir, legacy_output_dir):
        path.mkdir(parents=True, exist_ok=True)

    paths = {
        "llm_raw_output": str(process_dir / "llm_raw_output.txt"),
        "schema_validation_report": str(validate_dir / "schema_validation_report.json"),
    }

    (process_dir / "llm_raw_output.txt").write_text(state.get("llm_raw_output", ""), encoding="utf-8")
    schema_validation_report = state.get("schema_validation_report") or _schema_validation_report([])
    if state.get("context_error"):
        schema_validation_report = {
            **schema_validation_report,
            "valid": False,
            "schema_valid": False,
            "skipped": True,
            "skip_reason": state["context_error"],
        }
    write_json(schema_validation_report, validate_dir / "schema_validation_report.json")
    if state.get("business_validation_result"):
        paths["business_validation_report"] = str(
            write_json(state["business_validation_result"], business_validate_dir / "business_validation_report.json")
        )
        raw_output = state.get("business_validation_raw_output")
        if raw_output is not None:
            raw_path = business_validate_dir / "business_validation_raw_output.txt"
            raw_path.write_text(raw_output, encoding="utf-8")
            paths["business_validation_raw_output"] = str(raw_path)

    actual = state.get("candidate_process")
    if actual is not None:
        paths["product_process_definition"] = str(write_process_json(actual, product_output_dir / "process_definition.json"))
        paths["process_def"] = str(write_process_json(actual, process_dir / "process_def.json"))
        eval_paths = _write_eval_outputs(state, actual, eval_output_dir, legacy_output_dir)
        paths.update(eval_paths)
    elif _standard_json_exists(state["case_dir"]):
        standard_json = find_standard_json(state["case_dir"])
        standard_process = load_process_definition(standard_json)
        report = compare_processes(
            None,
            standard_process,
            schema_errors=state.get("schema_errors", []),
            business_validation_issues=_business_validation_issues(state),
        )
        report = merge_clarification_eval(
            report,
            _normalize_user_clarification_requests(state.get("business_validation_result") or {}),
            load_standard_target(standard_json),
        )
        paths["legacy_comparison_report"] = str(write_json(report, legacy_output_dir / "comparison_report.json"))
        paths["legacy_missing_report"] = str((legacy_output_dir / "missing_report.md"))
        (legacy_output_dir / "missing_report.md").write_text(render_missing_report(report), encoding="utf-8")

    user_clarification_requests = _normalize_user_clarification_requests(state.get("business_validation_result") or {})
    workflow_design_output = _build_workflow_design_output(state, user_clarification_requests=user_clarification_requests)
    designer_assistant_message = _build_designer_assistant_message(workflow_design_output)
    _emit_progress(
        node="workflow_design_output_writer",
        status="running",
        message=f"正在生成 AI 设计助手消息和 {len(user_clarification_requests)} 个待确认项",
    )
    design_persistence_report = {
        "persisted": False,
        "draft_saved": actual is not None and workflow_design_output["status"] not in {"failed_schema_validation", "context_too_large"},
        "published_definition_updated": False,
        "tables": [],
        "reason": "CLI workflow output only; product service persists this payload to SQLite.",
    }
    validation_summary = _build_validation_summary(workflow_design_output)
    actual_eval_target = _build_actual_eval_target(state, workflow_design_output)
    paths["actual_eval_target"] = str(write_json(actual_eval_target, eval_output_dir / "actual_eval_target.json"))

    paths["workflow_design_output"] = str(write_json(workflow_design_output, product_output_dir / "workflow_design_output.json"))
    paths["workflow_design_output_log"] = str(write_json(workflow_design_output, output_dir / "workflow_design_output.json"))
    _emit_progress(node="workflow_design_output_writer", status="running", message="正在写入 workflow_design_output.json")
    paths["designer_assistant_message"] = str(
        write_json(designer_assistant_message, product_output_dir / "designer_assistant_message.json")
    )
    paths["user_clarification_requests"] = str(
        write_json(user_clarification_requests, product_output_dir / "user_clarification_requests.json")
    )
    paths["validation_summary"] = str(
        write_json(validation_summary, product_output_dir / "validation_summary.json")
    )
    paths["design_persistence_report"] = str(
        write_json(design_persistence_report, output_dir / "design_persistence_report.json")
    )
    paths["output_manifest"] = str(out_dir / "output_manifest.json")
    write_json(paths, output_dir / "output_manifest.json")
    write_json(paths, out_dir / "output_manifest.json")
    write_run_meta(
        out_dir,
        case_id=state.get("case_id") or Path(state["case_dir"]).name,
        case_dir=state.get("case_dir"),
        status=workflow_design_output.get("status", "completed"),
        extra={
            "layout": {name: str(path) for name, path in layout.items()},
            "output_manifest": paths,
        },
    )

    return {
        **state,
        "workflow_design_output": workflow_design_output,
        "designer_assistant_message": designer_assistant_message,
        "design_persistence_report": design_persistence_report,
        "user_clarification_requests": user_clarification_requests,
        "output_paths": paths,
    }


def _build_workflow_design_output(
    state: WorkflowState,
    *,
    user_clarification_requests: list[dict[str, Any]],
) -> dict[str, Any]:
    process = state.get("candidate_process")
    schema_report = state.get("schema_validation_report") or {}
    business_result = state.get("business_validation_result") or {}
    context_error = state.get("context_error")
    blocking_issues = list(business_result.get("blocking_issues", []))
    warning_issues = list(business_result.get("warning_issues", []))
    raw_missing_items = list(business_result.get("raw_missing_items", []))
    status = _workflow_design_status(
        process=process,
        schema_report=schema_report,
        blocking_issues=blocking_issues,
        warning_issues=warning_issues,
        user_clarification_requests=user_clarification_requests,
        context_error=context_error,
    )
    process_payload = process.model_dump(mode="json") if process is not None else None
    summary = _design_summary(process, state, blocking_issues, warning_issues, user_clarification_requests)
    validation = {
        "schema_valid": bool(schema_report.get("valid")),
        "business_passed": not blocking_issues,
        "schema_errors": schema_report.get("schema_errors", []),
        "blocking_issues": blocking_issues,
        "warning_issues": warning_issues,
        "raw_missing_items": raw_missing_items,
        "context_error": context_error,
    }
    return {
        "status": status,
        "session_id": state.get("session_id") or state.get("case_id"),
        "workflow_definition_id": _draft_workflow_definition_id(process, state),
        "draft_version": process.meta.version if process is not None else "DRAFT",
        "process_definition": process_payload,
        "design_summary": summary,
        "validation": validation,
        "source_evidence": {
            "source_manifest_path": str(source_ingestion_dir(state["out_dir"]) / "source_file_manifest.json"),
            "chunk_catalog_path": str(source_ingestion_dir(state["out_dir"]) / "chunk_catalog.json"),
            "source_repair_report_path": str(source_ingestion_dir(state["out_dir"]) / "source_repair_report.json"),
        },
        "ui_payload": _ui_payload(process, summary, validation, user_clarification_requests),
        "user_clarification_requests": user_clarification_requests,
    }


def _workflow_design_status(
    *,
    process: ProcessDefinition | None,
    schema_report: dict[str, Any],
    blocking_issues: list[dict[str, Any]],
    warning_issues: list[dict[str, Any]],
    user_clarification_requests: list[dict[str, Any]],
    context_error: dict[str, Any] | None = None,
) -> str:
    if context_error:
        return str(context_error.get("type") or "context_error")
    if process is None or not schema_report.get("valid"):
        return "failed_schema_validation"
    if blocking_issues:
        if any(issue.get("evidence_status") == "found_in_raw" for issue in blocking_issues):
            return "needs_extraction_review"
        return "blocked_missing_source"
    if warning_issues or user_clarification_requests:
        return "needs_user_confirmation"
    return "draft_ready"


def _design_summary(
    process: ProcessDefinition | None,
    state: WorkflowState,
    blocking_issues: list[dict[str, Any]],
    warning_issues: list[dict[str, Any]],
    user_clarification_requests: list[dict[str, Any]],
) -> dict[str, Any]:
    if process is None:
        return {
            "field_count": 0,
            "node_count": 0,
            "path_count": 0,
            "role_count": 0,
            "attachment_count": 0,
            "source_count": len(state.get("source_file_manifest", [])),
            "blocking_issue_count": len(blocking_issues),
            "warning_issue_count": len(warning_issues),
            "clarification_count": len(user_clarification_requests),
        }
    return {
        "field_count": len(process.form_fields),
        "node_count": len(process.flow_nodes),
        "path_count": sum(len(node.submit_paths) for node in process.flow_nodes),
        "role_count": len(process.roles or []),
        "attachment_count": len(process.attachments or []),
        "source_count": len(state.get("source_file_manifest", [])),
        "blocking_issue_count": len(blocking_issues),
        "warning_issue_count": len(warning_issues),
        "clarification_count": len(user_clarification_requests),
    }


def _ui_payload(
    process: ProcessDefinition | None,
    summary: dict[str, Any],
    validation: dict[str, Any],
    user_clarification_requests: list[dict[str, Any]],
) -> dict[str, Any]:
    return {
        "overview_cards": [
            {"label": "表单字段", "value": summary["field_count"], "hint": "已从 source 中提取"},
            {"label": "流程环节", "value": summary["node_count"], "hint": "含起草与审批环节"},
            {"label": "提交路径", "value": summary["path_count"], "hint": "含同意、退回和条件分支"},
            {"label": "待确认", "value": summary["clarification_count"], "hint": "需要用户确认后再发布"},
        ],
        "tabs": {
            "overview": {"status": "draft_ready" if process is not None else "failed"},
            "form_fields": [field.model_dump(mode="json") for field in process.form_fields] if process else [],
            "flow_nodes": [node.model_dump(mode="json") for node in process.flow_nodes] if process else [],
            "attachments": [item.model_dump(mode="json") for item in (process.attachments or [])] if process else [],
            "roles": [item.model_dump(mode="json") for item in (process.roles or [])] if process else [],
            "validation": {
                **validation,
                "user_clarification_requests": user_clarification_requests,
            },
        },
    }


def _draft_workflow_definition_id(process: ProcessDefinition | None, state: WorkflowState) -> str:
    if process is not None:
        return f"wfd_{process.meta.process_id.lower().replace('-', '_')}_{process.meta.version.replace('.', '_')}"
    return f"wfd_{state.get('case_id', 'unknown')}_draft"


def _build_designer_assistant_message(workflow_design_output: dict[str, Any]) -> dict[str, Any]:
    summary = workflow_design_output.get("design_summary", {})
    process = workflow_design_output.get("process_definition") or {}
    meta = process.get("meta", {}) if isinstance(process, dict) else {}
    process_name = meta.get("process_name") or "流程草稿"
    clarification_cards = [
        _assistant_clarification_card(card, index=index)
        for index, card in enumerate(workflow_design_output.get("user_clarification_requests", []), start=1)
    ]
    status = workflow_design_output.get("status")
    if status == "context_too_large":
        title = "Source 内容超过初始化上下文预算"
        context_error = (workflow_design_output.get("validation") or {}).get("context_error") or {}
        content = context_error.get(
            "message",
            "当前上传 source 内容过大，已停止调用模型抽取。请减少材料范围，或等待后续接入检索式初始化。",
        )
    elif status == "failed_schema_validation":
        title = "流程初始化未生成合法草稿"
        content = "我没有得到符合 ProcessDefinition schema 的流程草稿。请检查 source 或重新初始化。"
    elif status == "blocked_missing_source":
        title = f"{process_name}草稿需要补充关键信息"
        content_lines = [
            f"已基于 {summary.get('source_count', 0)} 个 source 生成{process_name}草稿，但仍缺少关键依据。",
            "",
            "当前草稿包含：",
            f"- {summary.get('field_count', 0)} 个表单字段",
            f"- {summary.get('node_count', 0)} 个流程环节",
            f"- {summary.get('path_count', 0)} 条提交路径",
        ]
        if clarification_cards:
            content_lines.extend(_clarification_message_lines(clarification_cards))
        content = "\n".join(content_lines)
    else:
        title = f"已基于 {summary.get('source_count', 0)} 个 source 生成{process_name}草稿"
        content_lines = [
            f"已基于 {summary.get('source_count', 0)} 个 source 生成{process_name}草稿。",
            "",
            "当前草稿包含：",
            f"- {summary.get('field_count', 0)} 个表单字段",
            f"- {summary.get('node_count', 0)} 个流程环节",
            f"- {summary.get('path_count', 0)} 条提交路径",
        ]
        if clarification_cards:
            content_lines.extend(_clarification_message_lines(clarification_cards))
        content = "\n".join(content_lines)
    return {
        "role": "assistant",
        "message_type": "initialization_result",
        "title": title,
        "content": content,
        "summary_bullets": _assistant_summary_bullets(workflow_design_output),
        "clarification_cards": clarification_cards,
        "next_actions": [
            {"action": "answer_clarifications", "label": "处理待确认项"},
            {"action": "open_draft_overview", "label": "查看草稿配置"},
            {"action": "save_draft", "label": "保存草稿"},
        ],
    }


def _clarification_message_lines(clarification_cards: list[dict[str, Any]]) -> list[str]:
    lines = ["", f"我还发现 {len(clarification_cards)} 个需要确认的事项："]
    for index, card in enumerate(clarification_cards, start=1):
        lines.append(f"{index}. {card.get('question') or card.get('item') or '待确认事项'}")
    lines.extend(["", "建议先处理第 1 个。"])
    return lines


def _assistant_clarification_card(card: dict[str, Any], *, index: int) -> dict[str, Any]:
    normalized = dict(card)
    normalized.setdefault("id", f"clarify_{index}")
    normalized["question"] = _clarification_question(normalized)
    normalized["recommendation"] = normalized.get("recommendation") or _clarification_recommendation(normalized)
    normalized["options"] = _assistant_clarification_options(normalized)
    normalized.setdefault("free_text_allowed", True)
    return normalized


def _clarification_question(card: dict[str, Any]) -> str:
    issue_type = str(card.get("issue_type") or "")
    item = str(card.get("item") or "")
    question = str(card.get("question") or item or "待确认事项")
    if issue_type == "missing_time_limit" or "处理期限" in item or "处理期限" in question:
        return "处理期限目前仍有缺失，请补充。"
    if "附件" in item or "附件" in question or issue_type == "missing_attachment":
        return "附件材料要求目前仍有缺失，请补充。"
    if "代理人" in item or "代理人" in question:
        return "代理人字段是否必填目前仍不明确，请补充。"
    return question if question.endswith(("。", "？", "?")) else f"{question}。"


def _assistant_clarification_options(card: dict[str, Any]) -> list[dict[str, str]]:
    options: list[dict[str, str]] = []
    seen_keys: set[str] = set()

    def add(option: Any) -> None:
        normalized = _assistant_option(option)
        label = normalized["label"]
        if not label or _is_manual_clarification_option(label) or _is_generic_clarification_option(label):
            return
        key = _clarification_option_key(label)
        if key in seen_keys:
            return
        seen_keys.add(key)
        options.append(normalized)

    for option in card.get("options") or []:
        add(option)
    if not options:
        add(card.get("recommendation") or _clarification_recommendation(card))
    if not any(_is_skip_clarification_option(option["label"]) for option in options):
        add("暂不处理")
    return options


def _assistant_option(option: Any) -> dict[str, str]:
    if isinstance(option, dict):
        label = str(option.get("label") or option.get("value") or "选项")
        value = str(option.get("value") or label)
        return {"label": label, "value": value}
    label = str(option)
    return {"label": label, "value": label}


def _is_manual_clarification_option(label: str) -> bool:
    return any(token in label for token in ("手动", "自行", "自定义"))


def _is_skip_clarification_option(label: str) -> bool:
    return any(token in label for token in ("暂不", "不处理", "不配置"))


def _is_generic_clarification_option(label: str) -> bool:
    return any(token in label for token in ("按建议处理", "按常规配置"))


def _clarification_option_key(label: str) -> str:
    text = str(label or "").lower()
    text = re.sub(r"\s+", "", text)
    text = re.sub(r"[，,。；;：:、（）()「」『』【】\\[\\]\"'“”‘’]", "", text)
    text = (
        text.replace("source中", "")
        .replace("源材料中", "")
        .replace("根据source", "")
        .replace("候选流程", "")
        .replace("建议", "")
        .replace("请确认", "")
        .replace("需业务方最终拍板", "业务确认")
        .replace("需要业务方最终拍板", "业务确认")
        .replace("待业务确认", "业务确认")
        .replace("待确认", "业务确认")
    )
    if "3天" in text and ("总经理" in text or "部门总经理" in text) and ("业务确认" in text or "暂用" in text):
        return "leave_gt_3_dept_gm_confirm"
    if "5天" in text and ("总经理" in text or "部门总经理" in text) and ("业务确认" in text or "暂用" in text):
        return "leave_gt_5_dept_gm_confirm"
    if ("sla" in text or "处理期限" in text or "期限" in text) and ("审批" in text or "环节" in text):
        return "approval_sla"
    return text


def _clarification_recommendation(card: dict[str, Any]) -> str:
    issue_type = str(card.get("issue_type") or "")
    item = str(card.get("item") or "")
    if issue_type == "missing_time_limit" or "处理期限" in item:
        return "建议为审批环节配置处理时限。"
    if "附件" in item or issue_type == "missing_attachment":
        return "source 没有明确附件要求时，建议先设为非必填，后续由制度补充。"
    if "代理人" in item:
        return "建议先作为可选字段，避免短假场景增加填写负担。"
    return str(card.get("reason") or "请根据实际业务口径确认该配置。")


def _assistant_summary_bullets(workflow_design_output: dict[str, Any]) -> list[str]:
    process = workflow_design_output.get("process_definition") or {}
    if not isinstance(process, dict):
        return []
    fields = [item.get("field_name") for item in process.get("form_fields", []) if item.get("field_name")]
    nodes = [item.get("node_name") for item in process.get("flow_nodes", []) if item.get("node_name")]
    bullets: list[str] = []
    if fields:
        bullets.append("表单字段已覆盖：" + "、".join(fields[:8]) + ("等。" if len(fields) > 8 else "。"))
    if nodes:
        bullets.append("流程环节已覆盖：" + "、".join(nodes) + "。")
    validation = workflow_design_output.get("validation") or {}
    warnings = validation.get("warning_issues", [])
    if warnings:
        bullets.append(f"当前还有 {len(warnings)} 个提示项，需要在发布前确认。")
    return bullets


def _normalize_user_clarification_requests(result: dict[str, Any]) -> list[dict[str, Any]]:
    requests = list(result.get("user_clarification_requests") or [])
    if requests:
        return requests

    issues = [
        *result.get("raw_missing_items", []),
        *[
            issue
            for issue in result.get("blocking_issues", [])
            if issue.get("evidence_status") in {"not_found_in_raw", "uncertain"}
        ],
        *[
            issue
            for issue in result.get("warning_issues", [])
            if issue.get("evidence_status") in {"not_found_in_raw", "uncertain"}
        ],
    ]
    normalized: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for index, issue in enumerate(issues, start=1):
        key = (str(issue.get("issue_type")), str(issue.get("item")))
        if key in seen:
            continue
        seen.add(key)
        normalized.append(_clarification_from_issue(issue, index=index))
    return normalized


def _clarification_from_issue(issue: dict[str, Any], *, index: int) -> dict[str, Any]:
    issue_type = str(issue.get("issue_type") or "clarification")
    item = str(issue.get("item") or "待确认事项")
    return {
        "id": f"clarify_{issue_type}_{index}",
        "question": f"请确认「{item}」应该如何配置？",
        "reason": issue.get("message") or "当前 source 中没有足够明确的信息。",
        "severity": issue.get("severity", "warning"),
        "issue_type": issue_type,
        "item": item,
        "options": ["暂不处理"],
        "free_text_allowed": True,
        "related_items": [item],
        "source_hint": issue.get("source_hint"),
    }


def _build_validation_summary(workflow_design_output: dict[str, Any]) -> dict[str, Any]:
    validation = workflow_design_output.get("validation") or {}
    summary = workflow_design_output.get("design_summary") or {}
    return {
        "status": workflow_design_output.get("status"),
        "schema_valid": bool(validation.get("schema_valid")),
        "business_passed": bool(validation.get("business_passed")),
        "schema_error_count": len(validation.get("schema_errors") or []),
        "blocking_issue_count": len(validation.get("blocking_issues") or []),
        "warning_issue_count": len(validation.get("warning_issues") or []),
        "raw_missing_item_count": len(validation.get("raw_missing_items") or []),
        "clarification_count": len(workflow_design_output.get("user_clarification_requests") or []),
        "field_count": summary.get("field_count", 0),
        "node_count": summary.get("node_count", 0),
        "path_count": summary.get("path_count", 0),
    }


def _build_actual_eval_target(state: WorkflowState, workflow_design_output: dict[str, Any]) -> dict[str, Any]:
    process = workflow_design_output.get("process_definition")
    return {
        "case_id": state.get("case_id") or Path(state["case_dir"]).name,
        "source": "langgraph_actual",
        "generated_at": datetime.now().isoformat(),
        "review_metadata": {
            "review_status": "generated",
            "reviewer": "langgraph",
            "reviewed_at": None,
            "review_notes": "Projected from LangGraph product output for eval comparison.",
        },
        "deterministic_process_definition": process,
        "clarification_targets": workflow_design_output.get("user_clarification_requests") or [],
        "accepted_optional_clarifications": [],
        "forbidden_clarifications": [],
        "guardrail_targets": [],
        "evidence_map": {},
        "eval_weights": {},
    }


def _target_review_status(target_payload: dict[str, Any]) -> str:
    review = target_payload.get("review_metadata") or {}
    return str(review.get("review_status") or "draft")


def _is_formal_gold_target(target_payload: dict[str, Any]) -> bool:
    return _target_review_status(target_payload) in {"human_reviewed", "locked"}


def _standard_json_exists(case_dir: str | Path) -> bool:
    standard_dir = Path(case_dir) / "standard"
    return any(is_process_definition_json(path) for path in standard_dir.glob("*.json"))


def _business_validation_issues(state: WorkflowState) -> list[dict[str, Any]]:
    result = state.get("business_validation_result") or {}
    return [
        *result.get("blocking_issues", []),
        *result.get("warning_issues", []),
        *result.get("raw_missing_items", []),
    ]


def _write_eval_outputs(
    state: WorkflowState,
    actual: ProcessDefinition,
    eval_dir: Path,
    legacy_dir: Path,
) -> dict[str, str]:
    if not _standard_json_exists(state["case_dir"]):
        return {}
    standard_json = find_standard_json(state["case_dir"])
    standard_target = load_standard_target(standard_json)
    paths: dict[str, str] = {}
    if _is_formal_gold_target(standard_target):
        paths["gold_target_snapshot"] = str(write_json(standard_target, eval_dir / "gold_target_snapshot.json"))
    else:
        paths["gold_target_snapshot_skipped"] = str(
            write_json(
                {
                    "target_path": str(standard_json),
                    "review_status": _target_review_status(standard_target),
                    "reason": "gold target snapshot requires review_status human_reviewed or locked",
                },
                eval_dir / "gold_target_snapshot_skipped.json",
            )
        )
    standard_process = load_process_definition(standard_json)
    report = compare_processes(
        actual,
        standard_process,
        schema_errors=state.get("schema_errors", []),
        business_validation_issues=_business_validation_issues(state),
    )
    report = merge_clarification_eval(
        report,
        _normalize_user_clarification_requests(state.get("business_validation_result") or {}),
        standard_target,
    )
    comparison_path = write_json(report, legacy_dir / "comparison_report.json")
    missing_path = legacy_dir / "missing_report.md"
    missing_path.write_text(render_missing_report(report), encoding="utf-8")
    write_process_json(standard_process, legacy_dir / "standard_process.json")
    write_json(standard_target, legacy_dir / "standard_target.json")
    paths.update(
        {
            "legacy_comparison_report": str(comparison_path),
            "legacy_missing_report": str(missing_path),
            "legacy_standard_process": str(legacy_dir / "standard_process.json"),
            "legacy_standard_target": str(legacy_dir / "standard_target.json"),
        }
    )
    return paths


def _default_image_transcriber() -> Any:
    """真实运行入口默认构造的视觉转录器（懒加载 Bedrock 视觉模型）。"""
    from app.tools.vision_ocr import build_bedrock_image_transcriber

    return build_bedrock_image_transcriber()


def main() -> int:
    parser = argparse.ArgumentParser(description="Run process design Agent V1 for one dataset case.")
    parser.add_argument("--case", required=True, help="Case directory, e.g. data/cases/EOA140_subsidiary_major_matter")
    parser.add_argument(
        "--out",
        default=None,
        help="Output directory prefix. Defaults to runs/active/<YYYYMMDD_HHMMSS>_<case_name>.",
    )
    parser.add_argument("--no-timestamp", action="store_true", help="Use --out exactly without appending a timestamp.")
    parser.add_argument("--verbose", action="store_true", help="Print LLM raw outputs and validation route details.")
    parser.add_argument("--no-vision", action="store_true", help="Disable vision transcription of image sources.")
    args = parser.parse_args()

    out_dir = resolve_cli_out_dir(args.case, args.out, timestamp=not args.no_timestamp)
    transcriber = None if args.no_vision else _default_image_transcriber()
    state = run_process_case(args.case, out_dir, image_transcriber=transcriber, verbose=args.verbose)
    output_paths = state.get("output_paths", {})
    for name, path in output_paths.items():
        print(f"{name}: {path}")
    if args.verbose:
        _print_final_verbose_summary(state)
    return 0


def resolve_cli_out_dir(case_dir: str | Path, out: str | Path | None, *, timestamp: bool = True) -> Path:
    case_name = Path(case_dir).name
    suffix = datetime.now().strftime("%Y%m%d_%H%M%S")
    if out is None:
        return Path("runs") / "active" / (f"{suffix}_{case_name}" if timestamp else case_name)

    out_path = Path(out)
    if not timestamp:
        return out_path
    return out_path.with_name(f"{out_path.name}_{suffix}")


def _print_final_verbose_summary(state: WorkflowState) -> None:
    report = state.get("schema_validation_report", {})
    print(
        "[summary] "
        f"schema_valid={report.get('valid')} "
        f"schema_errors={len(report.get('schema_errors', []))}"
    )


if __name__ == "__main__":
    raise SystemExit(main())
