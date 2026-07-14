from __future__ import annotations

import argparse
import copy
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.io_utils import load_process_definition, load_standard_target, write_json
from app.reporting.comparison import compare_processes, merge_clarification_eval
from app.reporting.normalization import normalize_text
from app.run_layout import logs_dir, product_dir, source_ingestion_dir
from data.schema import ProcessDefinition


DEFAULT_WEIGHTS = {
    "meta": 5.0,
    "form_fields": 20.0,
    "flow_nodes": 15.0,
    "submit_paths": 15.0,
    "attachments": 8.0,
    "roles": 7.0,
    "clarifications": 20.0,
    "evidence_guardrail": 10.0,
}


@dataclass(frozen=True)
class ListEvalSpec:
    key: str
    weight_key: str
    missing_key: str
    extra_key: str
    diff_key: str
    target_count: int
    actual_count: int
    attr_count: int


def evaluate_run_against_target(
    run_dir: str | Path,
    target_path: str | Path,
    *,
    overrides_path: str | Path | None = None,
    allow_draft_target: bool = False,
) -> dict[str, Any]:
    run = Path(run_dir)
    target = Path(target_path)
    target_payload = load_standard_target(target)
    review_status = _review_metadata(target_payload).get("review_status", "draft")
    if not allow_draft_target and review_status not in {"human_reviewed", "locked"}:
        raise ValueError(
            f"Target {target} review_status={review_status!r}; "
            "formal eval requires human_reviewed or locked. Use --allow-draft-target for exploratory scoring."
        )
    expected = load_process_definition(target)
    actual = _load_actual_process(run)
    schema_errors = _load_schema_errors(run)
    business_issues = _load_business_issues(run)

    comparison = compare_processes(
        actual,
        expected,
        schema_errors=schema_errors,
        business_validation_issues=business_issues,
    )
    comparison = merge_clarification_eval(comparison, _load_clarifications(run), target_payload)
    weights = _resolve_weights(target_payload)
    auto = _score_all(
        actual=actual,
        expected=expected,
        comparison=comparison,
        run_dir=run,
        weights=weights,
    )
    overrides = _load_human_overrides(run, overrides_path)
    adjusted_comparison = _apply_human_overrides(comparison, overrides)
    adjusted = _score_all(
        actual=actual,
        expected=expected,
        comparison=adjusted_comparison,
        run_dir=run,
        weights=weights,
    )
    final_adjusted = adjusted if overrides else auto
    report = {
        "case_id": run.name,
        "target_file": str(target),
        "run_dir": str(run),
        "review_metadata": _review_metadata(target_payload),
        "status": auto["status"],
        "score": {
            "total": round(auto["total"], 2),
            "raw_total": round(auto["raw_total"], 2),
            "deterministic_process_definition": round(auto["deterministic"]["score"], 2),
            "clarification": round(auto["clarification"]["score"], 2),
            "evidence_and_guardrail": round(auto["evidence"]["score"], 2),
            "max_score": 100.0,
        },
        "auto_score": {
            "total": round(auto["total"], 2),
            "raw_total": round(auto["raw_total"], 2),
            "deterministic_process_definition": round(auto["deterministic"]["score"], 2),
            "clarification": round(auto["clarification"]["score"], 2),
            "evidence_and_guardrail": round(auto["evidence"]["score"], 2),
            "status": auto["status"],
        },
        "human_adjusted_score": {
            "total": round(final_adjusted["total"], 2),
            "raw_total": round(final_adjusted["raw_total"], 2),
            "deterministic_process_definition": round(final_adjusted["deterministic"]["score"], 2),
            "clarification": round(final_adjusted["clarification"]["score"], 2),
            "evidence_and_guardrail": round(final_adjusted["evidence"]["score"], 2),
            "status": final_adjusted["status"],
        },
        "override_count": len(overrides.get("overrides", [])),
        "human_review_overrides": overrides,
        "weights": weights,
        "hard_failures": auto["hard_failures"],
        "caps_applied": auto["caps"],
        "deterministic_eval": auto["deterministic"],
        "clarification_eval": auto["clarification"],
        "evidence_eval": auto["evidence"],
        "human_adjusted_eval": {
            "comparison_report": adjusted_comparison,
            "deterministic_eval": adjusted["deterministic"],
            "clarification_eval": adjusted["clarification"],
            "evidence_eval": adjusted["evidence"],
            "hard_failures": adjusted["hard_failures"],
            "caps_applied": adjusted["caps"],
        } if overrides else None,
        "comparison_report": comparison,
        "recommendations": _recommendations(comparison, auto["hard_failures"], auto["caps"]),
        # 本次评测实际用了哪些源文件（供人读："材料齐不齐"是判断分数可信度的参考信息，不进分）
        "source_files": sorted(_available_source_files(run)),
        "_gold_target_snapshot": target_payload,
        "_actual_eval_target": _build_actual_eval_target(run, actual, _load_clarifications(run)),
    }
    return report


def _score_all(
    *,
    actual: ProcessDefinition | None,
    expected: ProcessDefinition,
    comparison: dict[str, Any],
    run_dir: Path,
    weights: dict[str, float],
) -> dict[str, Any]:
    deterministic = _score_deterministic(actual, expected, comparison, weights)
    clarification = _score_clarifications(comparison, _load_clarifications(run_dir), weights["clarifications"])
    evidence = _score_evidence_and_guardrails(
        comparison=comparison,
        weight=weights["evidence_guardrail"],
    )
    raw_total = deterministic["score"] + clarification["score"] + evidence["score"]
    caps = _score_caps(expected, actual, comparison)
    hard_failures = _hard_failures(actual, comparison)
    final_score = min([raw_total, *[item["cap"] for item in caps]]) if caps else raw_total
    return {
        "total": final_score,
        "raw_total": raw_total,
        "status": _status(final_score, hard_failures),
        "deterministic": deterministic,
        "clarification": clarification,
        "evidence": evidence,
        "caps": caps,
        "hard_failures": hard_failures,
    }


def write_evaluation_report(report: dict[str, Any], output_dir: str | Path) -> dict[str, Path]:
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)
    gold_snapshot = report.get("_gold_target_snapshot")
    actual_eval_target = report.get("_actual_eval_target")
    report_payload = {key: value for key, value in report.items() if not key.startswith("_")}
    json_path = write_json(report_payload, out / "evaluation_report.json")
    md_path = out / "evaluation_report.md"
    md_path.write_text(render_evaluation_markdown(report_payload), encoding="utf-8")
    paths = {"json": json_path, "markdown": md_path}
    if gold_snapshot is not None:
        paths["gold_target_snapshot"] = write_json(gold_snapshot, out / "gold_target_snapshot.json")
    if actual_eval_target is not None:
        paths["actual_eval_target"] = write_json(actual_eval_target, out / "actual_eval_target.json")
    return paths


def render_evaluation_markdown(report: dict[str, Any]) -> str:
    review = report.get("review_metadata", {})
    lines = [
        f"# Evaluation Report: {report.get('case_id')}",
        "",
        f"- 状态：{report.get('status')}",
        f"- Target 人审状态：{review.get('review_status', 'draft')}",
        f"- 总分：{report.get('score', {}).get('total')} / 100",
        f"- Human adjusted：{report.get('human_adjusted_score', {}).get('total')} / 100",
        f"- Override 数：{report.get('override_count', 0)}",
        f"- Raw 分：{report.get('score', {}).get('raw_total')} / 100",
        "",
        "## 分数",
        "",
        f"- 确定性流程定义：{report['score']['deterministic_process_definition']}",
        f"- 待确认项：{report['score']['clarification']}",
        f"- 证据与约束：{report['score']['evidence_and_guardrail']}",
        "",
    ]
    _append_items(lines, "Hard Failures", report.get("hard_failures", []))
    _append_items(lines, "Caps Applied", [f"{item['reason']} => cap {item['cap']}" for item in report.get("caps_applied", [])])
    comparison = report.get("comparison_report", {})
    alignment = comparison.get("alignment_summary") or {}
    if alignment:
        _append_items(
            lines,
            "对齐摘要",
            [
                f"matched_nodes={alignment.get('matched_node_count', 0)}",
                f"node_name_fallback={alignment.get('node_name_fallback_count', 0)}",
                f"matched_paths={comparison.get('summary', {}).get('matched_path_count', 0)}",
                f"ambiguous_paths={comparison.get('summary', {}).get('ambiguous_path_match_count', 0)}",
                f"matched_clarifications={comparison.get('summary', {}).get('matched_required_clarification_count', 0)}",
            ],
        )
    _append_items(lines, "缺失表单字段", comparison.get("missing_form_fields", []))
    _append_items(lines, "字段差异", [_format_diff(item, "field_name") for item in comparison.get("field_differences", [])])
    _append_items(lines, "缺失流程环节", comparison.get("missing_flow_nodes", []))
    _append_items(lines, "环节差异", [_format_diff(item, "node_id") for item in comparison.get("node_differences", [])])
    _append_items(lines, "缺失提交路径", [_format_path(item) for item in comparison.get("missing_submit_paths", [])])
    _append_items(lines, "路径候选不唯一", [_format_ambiguous_path(item) for item in comparison.get("ambiguous_path_matches", [])])
    _append_items(lines, "路径条件差异", [_format_path_diff(item) for item in comparison.get("path_condition_differences", [])])
    _append_items(lines, "缺失附件", comparison.get("missing_attachments", []))
    _append_items(lines, "附件差异", [_format_diff(item, "attachment_type") for item in comparison.get("attachment_differences", [])])
    _append_items(lines, "缺失流程自定义角色", comparison.get("missing_roles", []))
    _append_items(lines, "角色差异", [_format_diff(item, "role_name") for item in comparison.get("role_differences", [])])
    _append_items(
        lines,
        "缺失待确认项",
        [item.get("question") or item.get("id") for item in comparison.get("missing_required_clarifications", [])],
    )
    _append_items(
        lines,
        "已匹配待确认项",
        [
            f"{item.get('question') or item.get('id')} ({item.get('match_basis')})"
            for item in comparison.get("matched_required_clarifications", [])
        ],
    )
    _append_items(
        lines,
        "命中禁止待确认项",
        [item.get("description") or item.get("id") for item in comparison.get("forbidden_clarification_hits", [])],
    )
    _append_items(lines, "建议", report.get("recommendations", []))
    overrides = report.get("human_review_overrides", {}).get("overrides", [])
    if overrides:
        _append_items(lines, "Human Overrides", [_format_override(item) for item in overrides])
    if lines[-1] != "":
        lines.append("")
    return "\n".join(lines)


def _load_actual_process(run_dir: Path) -> ProcessDefinition | None:
    candidates = [
        product_dir(run_dir) / "workflow_design_output.json",
        product_dir(run_dir) / "process_definition.json",
        logs_dir(run_dir) / "workflow_design_output_writer" / "workflow_design_output.json",
        logs_dir(run_dir) / "process_extraction_agent" / "process_def.json",
        run_dir / "workflow_design_output_writer" / "workflow_design_output.json",
        run_dir / "process_extraction_agent" / "process_def.json",
    ]
    for path in candidates:
        if not path.exists():
            continue
        payload = json.loads(path.read_text(encoding="utf-8"))
        if "process_definition" in payload:
            data = payload.get("process_definition")
            return ProcessDefinition.model_validate(data) if data else None
        return ProcessDefinition.model_validate(payload)
    return None


def _load_clarifications(run_dir: Path) -> list[dict[str, Any]]:
    for path in (
        product_dir(run_dir) / "user_clarification_requests.json",
        logs_dir(run_dir) / "workflow_design_output_writer" / "user_clarification_requests.json",
        run_dir / "workflow_design_output_writer" / "user_clarification_requests.json",
    ):
        if path.exists():
            payload = json.loads(path.read_text(encoding="utf-8"))
            return payload if isinstance(payload, list) else []
    for output_path in (
        product_dir(run_dir) / "workflow_design_output.json",
        logs_dir(run_dir) / "workflow_design_output_writer" / "workflow_design_output.json",
        run_dir / "workflow_design_output_writer" / "workflow_design_output.json",
    ):
        if output_path.exists():
            payload = json.loads(output_path.read_text(encoding="utf-8"))
            value = payload.get("user_clarification_requests", [])
            return value if isinstance(value, list) else []
    return []


def _load_schema_errors(run_dir: Path) -> list[dict[str, Any]]:
    for path in (
        logs_dir(run_dir) / "structural_validator" / "schema_validation_report.json",
        run_dir / "structural_validator" / "schema_validation_report.json",
    ):
        if path.exists():
            payload = json.loads(path.read_text(encoding="utf-8"))
            return payload.get("schema_errors", []) if isinstance(payload, dict) else []
    return []


def _load_business_issues(run_dir: Path) -> list[dict[str, Any]]:
    path = None
    for candidate in (
        logs_dir(run_dir) / "business_validation_agent" / "business_validation_report.json",
        run_dir / "business_validation_agent" / "business_validation_report.json",
    ):
        if candidate.exists():
            path = candidate
            break
    if path is None:
        return []
    payload = json.loads(path.read_text(encoding="utf-8"))
    issues: list[dict[str, Any]] = []
    for key in ("blocking_issues", "warning_issues", "raw_missing_items"):
        value = payload.get(key, [])
        if isinstance(value, list):
            issues.extend(item for item in value if isinstance(item, dict))
    return issues


def _build_actual_eval_target(
    run_dir: Path,
    actual: ProcessDefinition | None,
    clarifications: list[dict[str, Any]],
) -> dict[str, Any]:
    return {
        "case_id": run_dir.name,
        "source": "langgraph_actual",
        "deterministic_process_definition": actual.model_dump(mode="json") if actual is not None else None,
        "clarification_targets": clarifications,
        "accepted_optional_clarifications": [],
        "forbidden_clarifications": [],
        "guardrail_targets": [],
        "evidence_map": {},
        "eval_weights": {},
    }


def _resolve_weights(target_payload: dict[str, Any]) -> dict[str, float]:
    raw = target_payload.get("eval_weights") or {}
    if raw and all(key in raw for key in DEFAULT_WEIGHTS):
        return {key: float(raw[key]) for key in DEFAULT_WEIGHTS}
    if {"process_definition", "attachments", "custom_roles", "clarifications"} <= set(raw):
        process_total = float(raw.get("process_definition", 0.0)) * 100
        ratios = {"meta": 5, "form_fields": 20, "flow_nodes": 15, "submit_paths": 15}
        ratio_total = sum(ratios.values())
        weights = {
            key: process_total * value / ratio_total
            for key, value in ratios.items()
        }
        weights["attachments"] = float(raw.get("attachments", 0.0)) * 100
        weights["roles"] = float(raw.get("custom_roles", 0.0)) * 100
        weights["clarifications"] = float(raw.get("clarifications", 0.0)) * 100
        weights["evidence_guardrail"] = max(0.0, 100.0 - sum(weights.values()))
        return {key: round(value, 4) for key, value in weights.items()}
    return dict(DEFAULT_WEIGHTS)


def _review_metadata(target_payload: dict[str, Any]) -> dict[str, Any]:
    metadata = target_payload.get("review_metadata") or {}
    return {
        "review_status": metadata.get("review_status") or "draft",
        "reviewer": metadata.get("reviewer"),
        "reviewed_at": metadata.get("reviewed_at"),
        "review_notes": metadata.get("review_notes") or "",
    }


def _load_human_overrides(run_dir: Path, overrides_path: str | Path | None) -> dict[str, Any]:
    path = Path(overrides_path) if overrides_path else run_dir / "eval" / "human_review_overrides.json"
    if not path.exists():
        return {}
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        return {}
    overrides = payload.get("overrides", [])
    if not isinstance(overrides, list):
        payload["overrides"] = []
    return payload


def _apply_human_overrides(comparison: dict[str, Any], overrides_payload: dict[str, Any]) -> dict[str, Any]:
    overrides = overrides_payload.get("overrides", []) if isinstance(overrides_payload, dict) else []
    if not overrides:
        return comparison
    adjusted = copy.deepcopy(comparison)
    for override in overrides:
        if not isinstance(override, dict) or override.get("human_judgement") != "match":
            continue
        item_type = str(override.get("item_type") or "")
        target_key = str(override.get("target_key") or "")
        actual_key = str(override.get("actual_key") or "")
        keys = [key for key in [target_key, actual_key] if key]
        if item_type in {"form_field", "field"}:
            _remove_string_matches(adjusted, ["missing_form_fields", "extra_form_fields"], keys)
            _remove_dict_matches(adjusted, ["field_differences"], keys, ["field_name"])
        elif item_type in {"flow_node", "node"}:
            _remove_string_matches(adjusted, ["missing_flow_nodes", "extra_flow_nodes"], keys)
            _remove_dict_matches(adjusted, ["node_differences"], keys, ["node_id", "node_name"])
        elif item_type in {"submit_path", "path"}:
            _remove_dict_matches(adjusted, ["missing_submit_paths", "extra_submit_paths", "path_condition_differences"], keys, ["path_name", "node_id", "target_node_id", "condition"])
        elif item_type in {"attachment", "attachments"}:
            _remove_string_matches(adjusted, ["missing_attachments", "extra_attachments"], keys)
            _remove_dict_matches(adjusted, ["attachment_differences"], keys, ["attachment_type"])
        elif item_type in {"custom_role", "role", "roles"}:
            _remove_string_matches(adjusted, ["missing_roles", "extra_roles"], keys)
            _remove_dict_matches(adjusted, ["role_differences"], keys, ["role_name"])
        elif item_type in {"clarification", "clarification_target"}:
            moved = _pop_dict_matches(adjusted, "missing_required_clarifications", keys, ["id", "question"])
            adjusted.setdefault("matched_required_clarifications", []).extend(moved)
    adjusted["summary"] = _comparison_summary(adjusted)
    return adjusted


def _remove_string_matches(payload: dict[str, Any], list_keys: list[str], keys: list[str]) -> None:
    for list_key in list_keys:
        payload[list_key] = [
            item for item in payload.get(list_key, [])
            if not _matches_any_key(item, keys)
        ]


def _remove_dict_matches(payload: dict[str, Any], list_keys: list[str], keys: list[str], fields: list[str]) -> None:
    for list_key in list_keys:
        payload[list_key] = [
            item for item in payload.get(list_key, [])
            if not _dict_matches_any_key(item, keys, fields)
        ]


def _pop_dict_matches(payload: dict[str, Any], list_key: str, keys: list[str], fields: list[str]) -> list[dict[str, Any]]:
    kept: list[dict[str, Any]] = []
    popped: list[dict[str, Any]] = []
    for item in payload.get(list_key, []):
        if _dict_matches_any_key(item, keys, fields):
            popped.append(item)
        else:
            kept.append(item)
    payload[list_key] = kept
    return popped


def _dict_matches_any_key(item: dict[str, Any], keys: list[str], fields: list[str]) -> bool:
    values = [item.get(field) for field in fields]
    values.append(json.dumps(item, ensure_ascii=False))
    return any(_matches_any_key(value, keys) for value in values)


def _matches_any_key(value: Any, keys: list[str]) -> bool:
    normalized_value = _normalize_scalar(value)
    return any(key and _normalize_scalar(key) in normalized_value for key in keys)


def _comparison_summary(report: dict[str, Any]) -> dict[str, Any]:
    missing_count = sum(
        len(report.get(key, []))
        for key in [
            "missing_form_fields",
            "missing_flow_nodes",
            "missing_submit_paths",
            "missing_roles",
            "missing_attachments",
            "missing_required_clarifications",
        ]
    )
    difference_count = sum(
        len(report.get(key, []))
        for key in [
            "schema_errors",
            "business_validation_issues",
            "extra_form_fields",
            "field_differences",
            "extra_flow_nodes",
            "node_differences",
            "extra_submit_paths",
            "path_condition_differences",
            "extra_roles",
            "role_differences",
            "extra_attachments",
            "attachment_differences",
            "forbidden_clarification_hits",
        ]
    )
    return {
        "has_differences": bool(missing_count or difference_count),
        "missing_count": missing_count,
        "difference_count": difference_count,
        "ambiguous_path_match_count": len(report.get("ambiguous_path_matches", [])),
        "matched_path_count": len(report.get("matched_paths", [])),
        "matched_required_clarification_count": len(report.get("matched_required_clarifications", [])),
    }


def _score_deterministic(
    actual: ProcessDefinition | None,
    expected: ProcessDefinition,
    comparison: dict[str, Any],
    weights: dict[str, float],
) -> dict[str, Any]:
    if actual is None:
        details = {
            key: {"score": 0.0, "weight": weights[key], "reason": "actual ProcessDefinition missing"}
            for key in ("meta", "form_fields", "flow_nodes", "submit_paths", "attachments", "roles")
        }
        return {"score": 0.0, "details": details}

    details = {
        "meta": _score_meta(actual, expected, weights["meta"]),
        "form_fields": _score_list(
            ListEvalSpec(
                key="form_fields",
                weight_key="form_fields",
                missing_key="missing_form_fields",
                extra_key="extra_form_fields",
                diff_key="field_differences",
                target_count=len(expected.form_fields),
                actual_count=len(actual.form_fields),
                attr_count=8,
            ),
            comparison,
            weights,
        ),
        "flow_nodes": _score_list(
            ListEvalSpec(
                key="flow_nodes",
                weight_key="flow_nodes",
                missing_key="missing_flow_nodes",
                extra_key="extra_flow_nodes",
                diff_key="node_differences",
                target_count=len(expected.flow_nodes),
                actual_count=len(actual.flow_nodes),
                attr_count=7,
            ),
            comparison,
            weights,
        ),
        "submit_paths": _score_list(
            ListEvalSpec(
                key="submit_paths",
                weight_key="submit_paths",
                missing_key="missing_submit_paths",
                extra_key="extra_submit_paths",
                diff_key="path_condition_differences",
                target_count=sum(len(node.submit_paths) for node in expected.flow_nodes),
                actual_count=sum(len(node.submit_paths) for node in actual.flow_nodes),
                attr_count=1,
            ),
            comparison,
            weights,
        ),
        "attachments": _score_list(
            ListEvalSpec(
                key="attachments",
                weight_key="attachments",
                missing_key="missing_attachments",
                extra_key="extra_attachments",
                diff_key="attachment_differences",
                target_count=len(expected.attachments or []),
                actual_count=len(actual.attachments or []),
                attr_count=2,
            ),
            comparison,
            weights,
        ),
        "roles": _score_list(
            ListEvalSpec(
                key="roles",
                weight_key="roles",
                missing_key="missing_roles",
                extra_key="extra_roles",
                diff_key="role_differences",
                target_count=len(expected.roles or []),
                actual_count=len(actual.roles or []),
                attr_count=3,
            ),
            comparison,
            weights,
        ),
    }
    return {"score": sum(item["score"] for item in details.values()), "details": details}


def _score_meta(actual: ProcessDefinition, expected: ProcessDefinition, weight: float) -> dict[str, Any]:
    expected_data = expected.meta.model_dump(mode="json")
    actual_data = actual.meta.model_dump(mode="json")
    fields = list(expected_data)
    diffs = [
        {"attribute": key, "expected": expected_data.get(key), "actual": actual_data.get(key)}
        for key in fields
        if _normalize_scalar(expected_data.get(key)) != _normalize_scalar(actual_data.get(key))
    ]
    accuracy = (len(fields) - len(diffs)) / len(fields) if fields else 1.0
    return {"score": weight * accuracy, "weight": weight, "accuracy": accuracy, "differences": diffs}


def _score_list(spec: ListEvalSpec, comparison: dict[str, Any], weights: dict[str, float]) -> dict[str, Any]:
    weight = weights[spec.weight_key]
    missing = comparison.get(spec.missing_key, [])
    extra = comparison.get(spec.extra_key, [])
    diffs = comparison.get(spec.diff_key, [])
    matched_target = max(0, spec.target_count - len(missing))
    matched_actual = max(0, spec.actual_count - len(extra))
    recall = matched_target / spec.target_count if spec.target_count else 1.0
    precision = matched_actual / spec.actual_count if spec.actual_count else 1.0
    matched = max(matched_target, 0)
    total_attrs = matched * spec.attr_count
    diff_attrs = sum(len(item.get("differences", [])) for item in diffs)
    if spec.diff_key == "path_condition_differences":
        diff_attrs = len(diffs)
    attribute_accuracy = max(0.0, (total_attrs - diff_attrs) / total_attrs) if total_attrs else (1.0 if spec.target_count == 0 else 0.0)
    score = weight * (0.60 * recall + 0.30 * attribute_accuracy + 0.10 * precision)
    return {
        "score": score,
        "weight": weight,
        "target_count": spec.target_count,
        "actual_count": spec.actual_count,
        "missing_count": len(missing),
        "extra_count": len(extra),
        "difference_count": len(diffs),
        "recall": recall,
        "precision": precision,
        "attribute_accuracy": attribute_accuracy,
    }


def _score_clarifications(
    comparison: dict[str, Any],
    actual_requests: list[dict[str, Any]],
    weight: float,
) -> dict[str, Any]:
    required = comparison.get("missing_required_clarifications", []) + comparison.get("matched_required_clarifications", [])
    matched = comparison.get("matched_required_clarifications", [])
    forbidden = comparison.get("forbidden_clarification_hits", [])
    required_ratio = len(matched) / len(required) if required else 1.0
    required_score = weight * 0.60 * required_ratio
    quality_ratio = _clarification_quality_ratio(actual_requests)
    quality_score = weight * 0.25 * quality_ratio
    forbidden_score = weight * 0.15 if not forbidden else max(0.0, weight * 0.15 - len(forbidden) * weight * 0.075)
    return {
        "score": required_score + quality_score + forbidden_score,
        "weight": weight,
        "required_recall": required_ratio,
        "quality_ratio": quality_ratio,
        "forbidden_hits": len(forbidden),
        "matched_required_count": len(matched),
        "missing_required_count": len(comparison.get("missing_required_clarifications", [])),
    }


def _clarification_quality_ratio(requests: list[dict[str, Any]]) -> float:
    if not requests:
        return 1.0
    passed = 0
    for item in requests:
        checks = [
            bool(item.get("question") or item.get("title") or item.get("message")),
            bool(item.get("recommendation") or item.get("suggested_answer") or item.get("suggestion")),
            bool(item.get("options") or item.get("choices")),
            bool(item.get("evidence") or item.get("source_hint") or item.get("source_hints")),
        ]
        if sum(checks) >= 2:
            passed += 1
    return passed / len(requests)


def _score_evidence_and_guardrails(
    *,
    comparison: dict[str, Any],
    weight: float,
) -> dict[str, Any]:
    # 曾经是"证据覆盖率(50%) + 护栏达标(50%)"。证据覆盖率查的是"这次运行的源材料是否
    # 齐全"——测试设置的属性，不是模型输出的属性；正常全量跑（不消融）时 gold 的
    # evidence_map 必然对得上本案例的标准 source 集合，恒为 100%，对分数毫无区分度。
    # 已抠出打分公式，权重全部给"护栏"（不踩禁止澄清）；源材料清单改在报告顶层
    # `source_files` 展示，供人读，不进分。
    forbidden_hits = len(comparison.get("forbidden_clarification_hits", []))
    guardrail_ratio = 0.0 if forbidden_hits else 1.0
    score = weight * guardrail_ratio
    return {
        "score": score,
        "weight": weight,
        "forbidden_hits": forbidden_hits,
        "guardrail_ratio": guardrail_ratio,
    }


def _available_source_files(run_dir: Path) -> set[str]:
    path = None
    for candidate in (
        source_ingestion_dir(run_dir) / "source_file_manifest.json",
        run_dir / "source_ingestion" / "source_file_manifest.json",
    ):
        if candidate.exists():
            path = candidate
            break
    if path is None:
        return set()
    payload = json.loads(path.read_text(encoding="utf-8"))
    files: set[str] = set()
    for item in payload if isinstance(payload, list) else []:
        if isinstance(item, dict):
            files.add(str(item.get("file_name") or ""))
    return files


def _hard_failures(actual: ProcessDefinition | None, comparison: dict[str, Any]) -> list[str]:
    failures: list[str] = []
    if actual is None:
        failures.append("actual ProcessDefinition missing")
        return failures
    if comparison.get("schema_errors"):
        failures.append("actual ProcessDefinition has schema/reference errors")
    node_ids = {node.node_id for node in actual.flow_nodes}
    if not any(node.is_draft for node in actual.flow_nodes):
        failures.append("missing draft node")
    if "END" not in {path.target_node_id for node in actual.flow_nodes for path in node.submit_paths}:
        failures.append("missing end path")
    if len(node_ids) <= 1:
        failures.append("missing core approval nodes")
    return failures


def _score_caps(expected: ProcessDefinition, actual: ProcessDefinition | None, comparison: dict[str, Any]) -> list[dict[str, Any]]:
    caps: list[dict[str, Any]] = []
    if actual is None:
        return caps
    if expected.attachments and not actual.attachments:
        caps.append({"reason": "target has attachments but actual extracted none", "cap": 80.0})
    if expected.roles and not actual.roles:
        caps.append({"reason": "target has custom roles but actual extracted none", "cap": 85.0})
    required_total = len(comparison.get("missing_required_clarifications", [])) + len(comparison.get("matched_required_clarifications", []))
    if required_total and not comparison.get("matched_required_clarifications"):
        caps.append({"reason": "all required clarifications are missing", "cap": 80.0})
    if comparison.get("forbidden_clarification_hits"):
        caps.append({"reason": "forbidden clarification hit", "cap": 85.0})
    return caps


def _status(score: float, hard_failures: list[str]) -> str:
    if hard_failures:
        return "fail"
    if score >= 90:
        return "pass"
    if score >= 80:
        return "needs_review"
    return "fail"


def _recommendations(comparison: dict[str, Any], hard_failures: list[str], caps: list[dict[str, Any]]) -> list[str]:
    recommendations: list[str] = []
    if hard_failures:
        recommendations.append("先修复 hard failure，再看细分评分。")
    if comparison.get("missing_form_fields"):
        recommendations.append("优化 extraction prompt 对表单字段的覆盖。")
    if comparison.get("missing_submit_paths") or comparison.get("path_condition_differences"):
        recommendations.append("优化路径条件抽取，特别是金额/天数边界和退回路径。")
    if comparison.get("missing_attachments") or comparison.get("attachment_differences"):
        recommendations.append("补强附件配置抽取和附件条件的待确认逻辑。")
    if comparison.get("missing_roles") or comparison.get("role_differences"):
        recommendations.append("区分通用组织角色和流程自定义角色。")
    if comparison.get("missing_required_clarifications"):
        recommendations.append("business_validation_agent 需要为 source 未明确但影响发布的问题生成待确认卡片。")
    if caps:
        recommendations.append("存在 score cap，需优先处理 cap 对应问题。")
    return recommendations


def _normalize_scalar(value: Any) -> str:
    return normalize_text(value)


def _append_items(lines: list[str], title: str, items: list[Any]) -> None:
    if not items:
        return
    lines.extend([f"## {title}", ""])
    lines.extend(f"- {item}" for item in items)
    lines.append("")


def _format_diff(item: dict[str, Any], key: str) -> str:
    diffs = item.get("differences", [])
    detail = "；".join(
        f"{diff.get('attribute')}: expected={diff.get('expected')} actual={diff.get('actual')}"
        for diff in diffs
    )
    return f"{item.get(key)}: {detail}"


def _format_path(item: dict[str, Any]) -> str:
    condition = item.get("condition") or "无条件"
    return f"{item.get('node_id')} / {item.get('path_name')} -> {item.get('target_node_id')} ({condition})"


def _format_path_diff(item: dict[str, Any]) -> str:
    return (
        f"{item.get('node_id')} / {item.get('path_name')} -> {item.get('target_node_id')}: "
        f"expected={item.get('expected_condition') or '无条件'} actual={item.get('actual_condition') or '无条件'}"
    )


def _format_ambiguous_path(item: dict[str, Any]) -> str:
    expected = item.get("expected", {})
    return (
        f"{_format_path(expected)}: {item.get('match_basis')} "
        f"候选 {item.get('candidate_count', 0)} 个"
    )


def _format_override(item: dict[str, Any]) -> str:
    return (
        f"{item.get('item_type')}: {item.get('target_key')} -> {item.get('actual_key')} "
        f"({item.get('original_judgement')} => {item.get('human_judgement')})；"
        f"{item.get('reason') or ''}"
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Evaluate a LangGraph run against a target JSON.")
    parser.add_argument("--run", required=True, help="Run directory.")
    parser.add_argument("--target", required=True, help="Target JSON path.")
    parser.add_argument("--out", help="Output directory. Defaults to <run>/eval.")
    parser.add_argument("--overrides", help="Optional human_review_overrides.json path.")
    parser.add_argument(
        "--allow-draft-target",
        action="store_true",
        help="Allow exploratory scoring against draft target. Formal eval rejects draft targets by default.",
    )
    args = parser.parse_args()

    report = evaluate_run_against_target(
        args.run,
        args.target,
        overrides_path=args.overrides,
        allow_draft_target=args.allow_draft_target,
    )
    output_dir = Path(args.out) if args.out else Path(args.run) / "eval"
    paths = write_evaluation_report(report, output_dir)
    print(json.dumps({"status": report["status"], "score": report["score"], "paths": {k: str(v) for k, v in paths.items()}}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
