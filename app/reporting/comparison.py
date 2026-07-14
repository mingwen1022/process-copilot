from __future__ import annotations

import json
from typing import Any

from pydantic import ValidationError

from app.reporting.normalization import (
    DRAFT_CANONICAL,
    END_CANONICAL,
    canonical_node_token,
    flatten_text,
    match_clarification,
    normalize_condition,
    normalize_key,
    normalize_text,
)
from data.schema import ProcessDefinition


def compare_processes(
    actual: ProcessDefinition | None,
    expected: ProcessDefinition,
    *,
    schema_errors: list[dict[str, Any]] | None = None,
    business_validation_issues: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    report: dict[str, Any] = {
        "schema_errors": schema_errors or [],
        "business_validation_issues": business_validation_issues or [],
        "missing_form_fields": [],
        "extra_form_fields": [],
        "field_differences": [],
        "missing_flow_nodes": [],
        "extra_flow_nodes": [],
        "node_differences": [],
        "missing_submit_paths": [],
        "extra_submit_paths": [],
        "path_condition_differences": [],
        "missing_roles": [],
        "extra_roles": [],
        "role_differences": [],
        "missing_attachments": [],
        "extra_attachments": [],
        "attachment_differences": [],
        "missing_required_clarifications": [],
        "matched_required_clarifications": [],
        "forbidden_clarification_hits": [],
        "alignment_summary": {},
        "node_alias_map": {},
        "matched_paths": [],
        "ambiguous_path_matches": [],
    }
    if actual is None:
        report["summary"] = _summary(report)
        return report

    _compare_form_fields(actual, expected, report)
    node_alignment = _build_node_alignment(actual, expected)
    _compare_flow_nodes(actual, expected, report, node_alignment)
    _compare_submit_paths(actual, expected, report, node_alignment)
    _compare_roles(actual, expected, report)
    _compare_attachments(actual, expected, report)
    report["summary"] = _summary(report)
    return report


def merge_clarification_eval(
    report: dict[str, Any],
    actual_requests: list[dict[str, Any]] | None,
    standard_target: dict[str, Any],
) -> dict[str, Any]:
    actual = actual_requests or []

    missing_required: list[dict[str, Any]] = []
    matched_required: list[dict[str, Any]] = []
    for target in standard_target.get("clarification_targets", []) or []:
        if not isinstance(target, dict):
            continue
        expected_terms = _target_terms(target)
        match = _find_clarification_match(target, actual)
        payload = {
            "id": target.get("id"),
            "question": target.get("question") or target.get("expected_question"),
            "expected_terms": expected_terms,
        }
        if match:
            payload.update(
                {
                    "match_basis": match["match_basis"],
                    "matched_topic": match.get("matched_topic"),
                    "matched_terms": match.get("matched_terms", []),
                    "matched_actual_question": match.get("matched_actual_question"),
                }
            )
            matched_required.append(payload)
        else:
            missing_required.append(payload)

    forbidden_hits: list[dict[str, Any]] = []
    for target in standard_target.get("forbidden_clarifications", []) or []:
        target_payload = target if isinstance(target, dict) else {"description": str(target), "must_contain": [str(target)]}
        match = _find_clarification_match(target_payload, actual)
        if match:
            forbidden_hits.append(
                {
                    "id": target.get("id") if isinstance(target, dict) else None,
                    "description": target.get("description") if isinstance(target, dict) else str(target),
                    "matched_terms": match.get("matched_terms", []),
                    "match_basis": match.get("match_basis"),
                    "matched_actual_question": match.get("matched_actual_question"),
                }
            )

    report["missing_required_clarifications"] = missing_required
    report["matched_required_clarifications"] = matched_required
    report["forbidden_clarification_hits"] = forbidden_hits
    report["summary"] = _summary(report)
    return report


def report_for_schema_error(exc: ValidationError, expected: ProcessDefinition) -> dict[str, Any]:
    return compare_processes(None, expected, schema_errors=_validation_errors(exc))


def render_missing_report(report: dict[str, Any]) -> str:
    lines = ["# 缺失/差异报告", ""]
    summary = report.get("summary", {})
    lines.append(f"- 是否存在差异：{'是' if summary.get('has_differences') else '否'}")
    lines.append(f"- Schema 错误数：{len(report.get('schema_errors', []))}")
    lines.append(f"- 缺失项总数：{summary.get('missing_count', 0)}")
    lines.append(f"- 差异项总数：{summary.get('difference_count', 0)}")
    lines.append("")

    _append_list(lines, "Schema 校验错误", _format_schema_errors(report.get("schema_errors", [])))
    _append_list(lines, "业务校验问题", [_format_business_issue(item) for item in report.get("business_validation_issues", [])])
    _append_list(lines, "缺失表单字段", report.get("missing_form_fields", []))
    _append_list(lines, "多余表单字段", report.get("extra_form_fields", []))
    _append_list(lines, "字段差异", [_format_field_diff(item) for item in report.get("field_differences", [])])
    _append_list(lines, "缺失流程节点", report.get("missing_flow_nodes", []))
    _append_list(lines, "多余流程节点", report.get("extra_flow_nodes", []))
    _append_list(lines, "环节差异", [_format_named_diff(item, "node_id") for item in report.get("node_differences", [])])
    _append_list(lines, "缺失提交路径", [_format_path(item) for item in report.get("missing_submit_paths", [])])
    _append_list(lines, "多余提交路径", [_format_path(item) for item in report.get("extra_submit_paths", [])])
    _append_list(lines, "路径候选不唯一", [_format_ambiguous_path(item) for item in report.get("ambiguous_path_matches", [])])
    _append_list(lines, "路径条件差异", [_format_path_diff(item) for item in report.get("path_condition_differences", [])])
    _append_list(lines, "缺失角色", report.get("missing_roles", []))
    _append_list(lines, "多余角色", report.get("extra_roles", []))
    _append_list(lines, "角色差异", [_format_named_diff(item, "role_name") for item in report.get("role_differences", [])])
    _append_list(lines, "缺失附件配置", report.get("missing_attachments", []))
    _append_list(lines, "多余附件配置", report.get("extra_attachments", []))
    _append_list(lines, "附件差异", [_format_named_diff(item, "attachment_type") for item in report.get("attachment_differences", [])])
    _append_list(
        lines,
        "缺失待确认项",
        [_format_clarification_miss(item) for item in report.get("missing_required_clarifications", [])],
    )
    _append_list(
        lines,
        "禁止出现的待确认项",
        [_format_forbidden_clarification(item) for item in report.get("forbidden_clarification_hits", [])],
    )

    if not summary.get("has_differences"):
        lines.append("未发现缺失或差异。")
        lines.append("")
    return "\n".join(lines)


def _compare_form_fields(actual: ProcessDefinition, expected: ProcessDefinition, report: dict[str, Any]) -> None:
    pairs, missing, extra = _align_named_items(
        actual.form_fields,
        expected.form_fields,
        expected_key=lambda item: item.field_name,
        actual_key=lambda item: item.field_name,
    )
    report["missing_form_fields"] = missing
    report["extra_form_fields"] = extra

    for expected_field, actual_field in pairs:
        diffs = _diff_attrs(
            actual_field,
            expected_field,
            ["component_type", "required_stages", "visible_stages", "editable_stages", "logic_description", "default_value", "options", "max_length"],
        )
        if diffs:
            report["field_differences"].append({"field_name": expected_field.field_name, "differences": diffs})


def _compare_flow_nodes(
    actual: ProcessDefinition,
    expected: ProcessDefinition,
    report: dict[str, Any],
    node_alignment: dict[str, Any],
) -> None:
    pairs = node_alignment["pairs"]
    missing = node_alignment["missing"]
    extra = node_alignment["extra"]
    report["missing_flow_nodes"] = missing
    report["extra_flow_nodes"] = extra
    report["node_alias_map"] = node_alignment["node_alias_map"]
    report["alignment_summary"] = {
        "matched_node_count": len(pairs),
        "missing_node_count": len(missing),
        "extra_node_count": len(extra),
        "node_name_fallback_count": sum(
            1
            for item in node_alignment["node_alias_map"].values()
            if item.get("match_basis") == "node_name_fallback"
        ),
    }

    for expected_node, actual_node in pairs:
        diffs = _diff_attrs(actual_node, expected_node, ["node_name", "is_draft", "opinion_label", "time_limit_days"])
        diffs.extend(_diff_nested("handler", actual_node.handler, expected_node.handler, ["mode", "source", "role", "source_field"]))
        diffs.extend(_diff_nested("opinion", actual_node.opinion, expected_node.opinion, ["conclusive_required", "detail_required", "conclusive_options"]))
        if diffs:
            report["node_differences"].append({"node_id": expected_node.node_id, "differences": diffs})


def _compare_submit_paths(
    actual: ProcessDefinition,
    expected: ProcessDefinition,
    report: dict[str, Any],
    node_alignment: dict[str, Any],
) -> None:
    actual_paths = _path_list(actual)
    expected_paths = _path_list(expected)
    matched_actual: set[int] = set()

    for expected_path in expected_paths:
        actual_index, match_basis, ambiguous = _find_matching_path(
            expected_path,
            actual_paths,
            matched_actual,
            node_alignment,
        )
        if ambiguous:
            report["ambiguous_path_matches"].append(ambiguous)
        if actual_index is None:
            report["missing_submit_paths"].append(_path_payload(expected_path))
            continue
        matched_actual.add(actual_index)
        actual_path = actual_paths[actual_index]
        report["matched_paths"].append(
            {
                "expected": _path_payload(expected_path),
                "actual": _path_payload(actual_path),
                "match_basis": match_basis,
                "canonical_source": _canonical_path_node(expected_path, "node_id", "source_node_name", node_alignment, side="expected"),
                "canonical_target": _canonical_path_node(expected_path, "target_node_id", "target_node_name", node_alignment, side="expected"),
            }
        )
        actual_condition = _normalize_condition(actual_path["condition"])
        expected_condition = _normalize_condition(expected_path["condition"])
        if actual_condition != expected_condition:
            item = _path_payload(expected_path)
            item["expected_condition"] = expected_condition
            item["actual_condition"] = actual_condition
            report["path_condition_differences"].append(item)

    for index, actual_path in enumerate(actual_paths):
        if index not in matched_actual:
            report["extra_submit_paths"].append(_path_payload(actual_path))


def _compare_roles(actual: ProcessDefinition, expected: ProcessDefinition, report: dict[str, Any]) -> None:
    actual_roles = {role.role_name: role for role in actual.roles or []}
    expected_roles = {role.role_name: role for role in expected.roles or []}
    report["missing_roles"] = sorted(set(expected_roles) - set(actual_roles))
    report["extra_roles"] = sorted(set(actual_roles) - set(expected_roles))
    for name in sorted(set(expected_roles) & set(actual_roles)):
        diffs = _diff_attrs(actual_roles[name], expected_roles[name], ["role_type", "members", "department"])
        if diffs:
            report["role_differences"].append({"role_name": name, "differences": diffs})


def _compare_attachments(actual: ProcessDefinition, expected: ProcessDefinition, report: dict[str, Any]) -> None:
    actual_items = {item.attachment_type: item for item in actual.attachments or []}
    expected_items = {item.attachment_type: item for item in expected.attachments or []}
    report["missing_attachments"] = sorted(set(expected_items) - set(actual_items))
    report["extra_attachments"] = sorted(set(actual_items) - set(expected_items))
    for name in sorted(set(expected_items) & set(actual_items)):
        diffs = _diff_attrs(actual_items[name], expected_items[name], ["upload_stages", "required_stages"])
        if diffs:
            report["attachment_differences"].append({"attachment_type": name, "differences": diffs})


def _path_map(process: ProcessDefinition) -> dict[tuple[str, str, str], dict[str, str | None]]:
    paths: dict[tuple[str, str, str], dict[str, str | None]] = {}
    for node in process.flow_nodes:
        for path in node.submit_paths:
            key = (node.node_id, path.path_name, path.target_node_id)
            paths[key] = {
                "node_id": node.node_id,
                "path_name": path.path_name,
                "target_node_id": path.target_node_id,
                "condition": path.condition,
            }
    return paths


def _path_list(process: ProcessDefinition) -> list[dict[str, str | None]]:
    paths: list[dict[str, str | None]] = []
    for node in process.flow_nodes:
        for path in node.submit_paths:
            paths.append(
                {
                    "node_id": node.node_id,
                    "source_node_name": node.node_name,
                    "path_name": path.path_name,
                    "target_node_id": path.target_node_id,
                    "target_node_name": process.get_node_name(path.target_node_id),
                    "condition": path.condition,
                }
            )
    return paths


def _find_matching_path(
    expected: dict[str, str | None],
    actual_paths: list[dict[str, str | None]],
    matched_actual: set[int],
    node_alignment: dict[str, Any],
) -> tuple[int | None, str | None, dict[str, Any] | None]:
    modes = [
        ("source_target_path_name", True, False, False),
        ("source_target_condition", False, True, False),
        ("source_target_unique", False, False, True),
    ]
    for match_basis, include_path_name, include_condition, require_unique in modes:
        expected_key = _path_key(
            expected,
            include_path_name=include_path_name,
            include_condition=include_condition,
            node_alignment=node_alignment,
            side="expected",
        )
        candidates: list[int] = []
        for index, actual in enumerate(actual_paths):
            if index in matched_actual:
                continue
            actual_key = _path_key(
                actual,
                include_path_name=include_path_name,
                include_condition=include_condition,
                node_alignment=node_alignment,
                side="actual",
            )
            if actual_key == expected_key:
                candidates.append(index)
        if len(candidates) == 1:
            return candidates[0], match_basis, None
        if len(candidates) > 1 or (require_unique and candidates):
            return None, None, {
                "expected": _path_payload(expected),
                "match_basis": match_basis,
                "candidate_count": len(candidates),
                "candidates": [_path_payload(actual_paths[index]) for index in candidates],
            }
    return None, None, None


def _target_terms(target: dict[str, Any]) -> list[str]:
    explicit = target.get("expected_question_contains") or target.get("must_contain")
    if isinstance(explicit, list):
        return [str(item).strip() for item in explicit if str(item).strip()]
    if isinstance(explicit, str) and explicit.strip():
        return [explicit.strip()]

    terms: list[str] = []
    for key in ["question", "expected_question", "target", "topic", "field", "id"]:
        value = target.get(key)
        if isinstance(value, str) and value.strip():
            terms.append(value.strip())
    return terms[:3]


def _find_clarification_match(target: dict[str, Any], actual_requests: list[dict[str, Any]]) -> dict[str, Any] | None:
    for request in actual_requests:
        match = match_clarification(target, request)
        if match:
            return {
                **match,
                "matched_actual_question": request.get("question") or request.get("title") or request.get("message"),
            }
    return None


def _matches_terms(text: str, terms: list[str]) -> bool:
    if not terms:
        return False
    normalized = text.replace(" ", "")
    hits = 0
    for term in terms:
        value = str(term).strip().replace(" ", "")
        if value and value in normalized:
            hits += 1
    return hits == len(terms)


def _flatten_text(payload: Any) -> str:
    return flatten_text(payload)


def _path_payload(item: dict[str, str | None]) -> dict[str, str | None]:
    return {
        "node_id": item["node_id"],
        "path_name": item["path_name"],
        "target_node_id": item["target_node_id"],
        "condition": item["condition"],
    }


def _build_node_alignment(actual: ProcessDefinition, expected: ProcessDefinition) -> dict[str, Any]:
    actual_by_id: dict[str, Any] = {}
    actual_by_name: dict[str, Any] = {}
    for node in actual.flow_nodes:
        actual_by_id[canonical_node_token(node.node_id, node.node_name)] = node
        actual_by_id[_normalize_key(node.node_id)] = node
        actual_by_name[_normalize_key(node.node_name)] = node

    matched_actual_ids: set[str] = set()
    pairs: list[tuple[Any, Any]] = []
    missing: list[str] = []
    node_alias_map: dict[str, Any] = {}
    expected_id_to_canonical: dict[str, str] = {}
    expected_name_to_canonical: dict[str, str] = {}
    actual_id_to_canonical: dict[str, str] = {}
    actual_name_to_canonical: dict[str, str] = {}

    for expected_node in expected.flow_nodes:
        expected_canonical = canonical_node_token(expected_node.node_id, expected_node.node_name)
        if expected_canonical not in {DRAFT_CANONICAL, END_CANONICAL}:
            expected_canonical = _normalize_key(expected_node.node_id)
        expected_id_to_canonical[_normalize_key(expected_node.node_id)] = expected_canonical
        expected_name_to_canonical[_normalize_key(expected_node.node_name)] = expected_canonical

        actual_node = actual_by_id.get(canonical_node_token(expected_node.node_id, expected_node.node_name))
        match_basis = "node_id"
        if actual_node is None:
            actual_node = actual_by_id.get(_normalize_key(expected_node.node_id))
        if actual_node is None:
            actual_node = actual_by_name.get(_normalize_key(expected_node.node_name))
            match_basis = "node_name_fallback"
        if actual_node is None:
            missing.append(expected_node.node_id)
            continue

        pairs.append((expected_node, actual_node))
        matched_actual_ids.add(_normalize_key(actual_node.node_id))
        actual_id_to_canonical[_normalize_key(actual_node.node_id)] = expected_canonical
        actual_name_to_canonical[_normalize_key(actual_node.node_name)] = expected_canonical
        node_alias_map[expected_node.node_id] = {
            "canonical": expected_canonical,
            "expected_node_id": expected_node.node_id,
            "expected_node_name": expected_node.node_name,
            "actual_node_id": actual_node.node_id,
            "actual_node_name": actual_node.node_name,
            "match_basis": match_basis,
        }

    extra = [
        node.node_id
        for node in actual.flow_nodes
        if _normalize_key(node.node_id) not in matched_actual_ids
    ]
    return {
        "pairs": pairs,
        "missing": sorted(missing),
        "extra": sorted(extra),
        "node_alias_map": node_alias_map,
        "expected_id_to_canonical": expected_id_to_canonical,
        "expected_name_to_canonical": expected_name_to_canonical,
        "actual_id_to_canonical": actual_id_to_canonical,
        "actual_name_to_canonical": actual_name_to_canonical,
    }


def _canonical_path_node(
    item: dict[str, str | None],
    id_key: str,
    name_key: str,
    node_alignment: dict[str, Any],
    *,
    side: str,
) -> str:
    node_id = item.get(id_key)
    node_name = item.get(name_key)
    special = canonical_node_token(node_id, node_name)
    if special in {DRAFT_CANONICAL, END_CANONICAL}:
        return special

    id_norm = _normalize_key(node_id)
    name_norm = _normalize_key(node_name)
    if side == "actual":
        return (
            node_alignment["actual_id_to_canonical"].get(id_norm)
            or node_alignment["actual_name_to_canonical"].get(name_norm)
            or id_norm
            or name_norm
        )
    return (
        node_alignment["expected_id_to_canonical"].get(id_norm)
        or node_alignment["expected_name_to_canonical"].get(name_norm)
        or id_norm
        or name_norm
    )


def _path_key(
    item: dict[str, str | None],
    *,
    include_path_name: bool,
    include_condition: bool,
    node_alignment: dict[str, Any],
    side: str,
) -> tuple[str, ...]:
    values = [
        _canonical_path_node(item, "node_id", "source_node_name", node_alignment, side=side),
        _canonical_path_node(item, "target_node_id", "target_node_name", node_alignment, side=side),
    ]
    if include_path_name:
        values.append(_normalize_key(item.get("path_name")))
    if include_condition:
        values.append(_normalize_condition(item.get("condition")))
    return tuple(values)


def _diff_attrs(actual: Any, expected: Any, attrs: list[str]) -> list[dict[str, Any]]:
    diffs: list[dict[str, Any]] = []
    for attr in attrs:
        actual_value = _json_value(getattr(actual, attr))
        expected_value = _json_value(getattr(expected, attr))
        if _normalize_for_compare(actual_value) != _normalize_for_compare(expected_value):
            diffs.append({"attribute": attr, "expected": expected_value, "actual": actual_value})
    return diffs


def _diff_nested(prefix: str, actual: Any, expected: Any, attrs: list[str]) -> list[dict[str, Any]]:
    if actual is None and expected is None:
        return []
    if actual is None or expected is None:
        return [{"attribute": prefix, "expected": _json_value(expected), "actual": _json_value(actual)}]
    diffs = _diff_attrs(actual, expected, attrs)
    for item in diffs:
        item["attribute"] = f"{prefix}.{item['attribute']}"
    return diffs


def _json_value(value: Any) -> Any:
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    if hasattr(value, "value"):
        return value.value
    if isinstance(value, list):
        return [_json_value(item) for item in value]
    return value


def _normalize_empty(value: str | None) -> str | None:
    normalized = _normalize_for_compare(value)
    return normalized or None


def _align_named_items(
    actual_items: list[Any],
    expected_items: list[Any],
    *,
    expected_key: Any,
    actual_key: Any,
    expected_fallback: Any | None = None,
    actual_fallback: Any | None = None,
) -> tuple[list[tuple[Any, Any]], list[str], list[str]]:
    actual_by_key = {_normalize_key(actual_key(item)): item for item in actual_items}
    expected_by_key = {_normalize_key(expected_key(item)): item for item in expected_items}
    matched_actual: set[str] = set()
    pairs: list[tuple[Any, Any]] = []
    missing: list[str] = []

    for expected_norm, expected_item in expected_by_key.items():
        actual_item = actual_by_key.get(expected_norm)
        actual_norm = expected_norm if actual_item is not None else None
        if actual_item is None and expected_fallback and actual_fallback:
            expected_fallback_norm = _normalize_key(expected_fallback(expected_item))
            for candidate in actual_items:
                candidate_norm = _normalize_key(actual_fallback(candidate))
                if candidate_norm == expected_fallback_norm:
                    actual_item = candidate
                    actual_norm = _normalize_key(actual_key(candidate))
                    break
        if actual_item is None:
            missing.append(str(expected_key(expected_item)))
            continue
        matched_actual.add(actual_norm or _normalize_key(actual_key(actual_item)))
        pairs.append((expected_item, actual_item))

    extra = [
        str(actual_key(item))
        for item in actual_items
        if _normalize_key(actual_key(item)) not in matched_actual
    ]
    return pairs, sorted(missing), sorted(extra)


def _normalize_for_compare(value: Any) -> Any:
    if value is None:
        return ""
    if isinstance(value, str):
        return _normalize_text(value)
    if hasattr(value, "value"):
        return _normalize_text(value.value)
    if isinstance(value, list):
        return sorted(_normalize_for_compare(item) for item in value if _normalize_for_compare(item) != "")
    if isinstance(value, dict):
        return {key: _normalize_for_compare(val) for key, val in sorted(value.items())}
    return value


def _normalize_key(value: Any) -> str:
    return normalize_key(value)


def _normalize_condition(value: Any) -> str:
    return normalize_condition(value)


def _normalize_text(value: Any) -> str:
    return normalize_text(value)


def _validation_errors(exc: ValidationError) -> list[dict[str, Any]]:
    return json.loads(exc.json())


def _summary(report: dict[str, Any]) -> dict[str, Any]:
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


def _append_list(lines: list[str], title: str, items: list[Any]) -> None:
    if not items:
        return
    lines.append(f"## {title}")
    for item in items:
        lines.append(f"- {item}")
    lines.append("")


def _format_schema_errors(errors: list[dict[str, Any]]) -> list[str]:
    return [f"{'.'.join(map(str, err.get('loc', [])))}: {err.get('msg')}" for err in errors]


def _format_business_issue(item: dict[str, Any]) -> str:
    issue_type = item.get("issue_type", "")
    target = item.get("item", "")
    evidence = item.get("evidence_status", "")
    message = item.get("message", "")
    return f"{issue_type}/{target} [{evidence}]: {message}"


def _format_field_diff(item: dict[str, Any]) -> str:
    return _format_named_diff(item, "field_name")


def _format_named_diff(item: dict[str, Any], key: str) -> str:
    parts = []
    for diff in item.get("differences", []):
        parts.append(f"{diff['attribute']} 期望={diff['expected']} 实际={diff['actual']}")
    return f"{item[key]}: " + "；".join(parts)


def _format_path(item: dict[str, Any]) -> str:
    condition = item.get("condition") or "无条件"
    return f"{item['node_id']} / {item['path_name']} -> {item['target_node_id']} ({condition})"


def _format_path_diff(item: dict[str, Any]) -> str:
    return (
        f"{item['node_id']} / {item['path_name']} -> {item['target_node_id']}: "
        f"期望={item.get('expected_condition') or '无条件'} 实际={item.get('actual_condition') or '无条件'}"
    )


def _format_ambiguous_path(item: dict[str, Any]) -> str:
    expected = item.get("expected", {})
    candidates = item.get("candidates", [])
    return (
        f"{_format_path(expected)}：{item.get('match_basis')} 有 {len(candidates)} 个候选，"
        "需人工确认"
    )


def _format_clarification_miss(item: dict[str, Any]) -> str:
    question = item.get("question") or item.get("id") or "未命名待确认项"
    terms = "、".join(item.get("expected_terms", []))
    return f"{question}（期望包含：{terms}）"


def _format_forbidden_clarification(item: dict[str, Any]) -> str:
    description = item.get("description") or item.get("id") or "未命名禁止项"
    terms = "、".join(item.get("matched_terms", []))
    return f"{description}（命中：{terms}）"
