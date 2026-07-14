from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any

from app.io_utils import write_json
from app.reporting.normalization import match_clarification, normalize_text
from app.run_layout import logs_dir, product_dir
from data.schema import FlowNode, FormField, ProcessDefinition


def evaluate_run_against_eval_schema(run_dir: str | Path, target_path: str | Path) -> dict[str, Any]:
    run = Path(run_dir)
    target_file = Path(target_path)
    target = json.loads(target_file.read_text(encoding="utf-8"))
    actual = _load_actual_process(run)
    clarifications = _load_clarifications(run)

    scored_items = [
        item
        for item in target.get("scored_expected_items", [])
        if item.get("score_enabled") and item.get("process_definition_projection")
    ]
    item_results = [_evaluate_scored_item(item, actual) for item in scored_items]
    clarification_results = [
        _evaluate_clarification_target(item, clarifications)
        for item in target.get("clarification_targets", [])
    ]
    forbidden_results = [
        _evaluate_forbidden_item(item, actual)
        for item in target.get("forbidden_items", [])
    ]

    deterministic_possible = sum(result["weight"] for result in item_results)
    deterministic_earned = sum(result["score"] for result in item_results)
    clarification_possible = sum(result["weight"] for result in clarification_results)
    clarification_earned = sum(result["score"] for result in clarification_results)
    guardrail_possible = max(1.0, float(target.get("eval_weights", {}).get("guardrails", 5)))
    guardrail_penalty = min(guardrail_possible, sum(result["penalty"] for result in forbidden_results))
    guardrail_earned = guardrail_possible - guardrail_penalty

    total_possible = deterministic_possible + clarification_possible + guardrail_possible
    total_earned = deterministic_earned + clarification_earned + guardrail_earned
    score = round((total_earned / total_possible * 100) if total_possible else 0.0, 2)

    report = {
        "case_id": target.get("case_id") or run.name,
        "target_file": str(target_file),
        "run_dir": str(run),
        "status": _status(score, item_results, clarification_results, forbidden_results),
        "score": {
            "total": score,
            "earned": round(total_earned, 4),
            "possible": round(total_possible, 4),
            "deterministic": round(deterministic_earned, 4),
            "deterministic_possible": round(deterministic_possible, 4),
            "clarifications": round(clarification_earned, 4),
            "clarifications_possible": round(clarification_possible, 4),
            "guardrails": round(guardrail_earned, 4),
            "guardrails_possible": round(guardrail_possible, 4),
        },
        "actual_summary": _actual_summary(actual, clarifications),
        "scored_item_results": item_results,
        "clarification_results": clarification_results,
        "forbidden_results": forbidden_results,
        "schema_gap_items": target.get("schema_gap_items", []),
        "context_only": target.get("context_only", []),
        "recommendations": _recommendations(item_results, clarification_results, forbidden_results),
    }
    return report


def write_eval_schema_report(report: dict[str, Any], out_dir: str | Path) -> dict[str, Path]:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    json_path = write_json(report, out / "eval_schema_report.json")
    md_path = out / "eval_schema_report.md"
    md_path.write_text(render_eval_schema_markdown(report), encoding="utf-8")
    return {"json": json_path, "markdown": md_path}


def render_eval_schema_markdown(report: dict[str, Any]) -> str:
    lines = [
        f"# Eval Schema Report: {report.get('case_id')}",
        "",
        f"- 状态：{report.get('status')}",
        f"- 总分：{report.get('score', {}).get('total')} / 100",
        f"- 确定性项：{report.get('score', {}).get('deterministic')} / {report.get('score', {}).get('deterministic_possible')}",
        f"- 待确认项：{report.get('score', {}).get('clarifications')} / {report.get('score', {}).get('clarifications_possible')}",
        f"- Guardrail：{report.get('score', {}).get('guardrails')} / {report.get('score', {}).get('guardrails_possible')}",
        "",
        "## Misses",
        "",
    ]
    misses = [item for item in report.get("scored_item_results", []) if item.get("score", 0) < item.get("weight", 0)]
    if misses:
        for item in misses:
            lines.append(f"- {item.get('id')}: {item.get('status')} ({item.get('score')}/{item.get('weight')})")
            for detail in item.get("details", []):
                lines.append(f"  - {detail}")
    else:
        lines.append("- 无")
    lines.extend(["", "## Missing Clarifications", ""])
    missing_clarifications = [item for item in report.get("clarification_results", []) if not item.get("matched")]
    if missing_clarifications:
        for item in missing_clarifications:
            lines.append(f"- {item.get('id')}: {item.get('topic')}")
    else:
        lines.append("- 无")
    lines.extend(["", "## Forbidden Hits", ""])
    hits = [item for item in report.get("forbidden_results", []) if item.get("hit")]
    if hits:
        for item in hits:
            lines.append(f"- {item.get('id')}: {item.get('reason')}")
    else:
        lines.append("- 无")
    lines.extend(["", "## Recommendations", ""])
    recommendations = report.get("recommendations", [])
    lines.extend(f"- {item}" for item in recommendations) if recommendations else lines.append("- 无")
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
        data = payload.get("process_definition") if isinstance(payload, dict) else payload
        if not data:
            continue
        return ProcessDefinition.model_validate(data)
    return None


def _load_clarifications(run_dir: Path) -> list[dict[str, Any]]:
    for path in (
        product_dir(run_dir) / "user_clarification_requests.json",
        product_dir(run_dir) / "workflow_design_output.json",
        logs_dir(run_dir) / "workflow_design_output_writer" / "user_clarification_requests.json",
        logs_dir(run_dir) / "workflow_design_output_writer" / "workflow_design_output.json",
        run_dir / "workflow_design_output_writer" / "user_clarification_requests.json",
        run_dir / "workflow_design_output_writer" / "workflow_design_output.json",
    ):
        if not path.exists():
            continue
        payload = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(payload, list):
            return payload
        value = payload.get("user_clarification_requests", []) if isinstance(payload, dict) else []
        if isinstance(value, list):
            return value
    return []


def _evaluate_scored_item(item: dict[str, Any], actual: ProcessDefinition | None) -> dict[str, Any]:
    weight = float(item.get("score_weight", 1))
    if actual is None:
        return _item_result(item, weight, 0.0, "missing_actual", ["未生成 ProcessDefinition。"])
    category = item.get("category")
    if category == "meta":
        return _evaluate_meta_item(item, actual, weight)
    if category == "form_field":
        return _evaluate_form_field_item(item, actual, weight)
    if category == "flow_node":
        return _evaluate_flow_node_item(item, actual, weight)
    if category == "handler_source":
        return _evaluate_handler_item(item, actual, weight)
    if category == "submit_path":
        return _evaluate_submit_path_item(item, actual, weight)
    return _item_result(item, weight, 0.0, "unsupported_category", [f"暂不支持 category={category}。"])


def _evaluate_meta_item(item: dict[str, Any], actual: ProcessDefinition, weight: float) -> dict[str, Any]:
    expected = item.get("expected", {})
    value = expected.get("value")
    actual_value = actual.meta.process_name
    passed = _text_match(actual_value, value)
    return _item_result(
        item,
        weight,
        weight if passed else 0.0,
        "matched" if passed else "mismatch",
        [] if passed else [f"期望流程名称包含 {value!r}，实际为 {actual_value!r}。"],
    )


def _evaluate_form_field_item(item: dict[str, Any], actual: ProcessDefinition, weight: float) -> dict[str, Any]:
    field_name = str(item.get("expected", {}).get("field_name") or item.get("name") or "")
    field = _find_field(actual, field_name)
    if field is None:
        return _item_result(item, weight, 0.0, "missing", [f"缺少表单字段：{field_name}。"])
    checks: list[tuple[bool, str]] = []
    expected = item.get("expected", {})
    if "component_type" in expected:
        checks.append((_text_match(field.component_type.value, expected["component_type"]), f"组件类型 expected={expected['component_type']} actual={field.component_type.value}"))
    if "required" in expected:
        checks.append(((bool(field.required_stages) == bool(expected["required"])), f"必填 expected={expected['required']} actual={field.required_stages}"))
    if "required_stages" in expected:
        checks.append((_stage_list_match(field.required_stages, expected["required_stages"]), f"必填环节 expected={expected['required_stages']} actual={field.required_stages}"))
    if "visible_stages" in expected:
        checks.append((_stage_list_match(field.visible_stages, expected["visible_stages"]), f"可见环节 expected={expected['visible_stages']} actual={field.visible_stages}"))
    if "editable_stages" in expected:
        checks.append((_stage_list_match(field.editable_stages, expected["editable_stages"]), f"可编辑环节 expected={expected['editable_stages']} actual={field.editable_stages}"))
    if "options" in expected:
        actual_options = field.options or []
        checks.append((_options_cover(actual_options, expected["options"]), f"选项 expected={expected['options']} actual={actual_options}"))
    if "logic_keywords" in expected:
        text = f"{field.logic_description or ''} {field.default_value or ''}"
        checks.append((_contains_all(text, expected["logic_keywords"]), f"逻辑说明缺关键词 expected={expected['logic_keywords']} actual={text}"))
    return _checks_result(item, weight, checks)


def _evaluate_flow_node_item(item: dict[str, Any], actual: ProcessDefinition, weight: float) -> dict[str, Any]:
    node_name = str(item.get("expected", {}).get("node_name") or item.get("name") or "")
    node = _find_node(actual, node_name)
    if node is None:
        return _item_result(item, weight, 0.0, "missing", [f"缺少流程环节：{node_name}。"])
    expected = item.get("expected", {})
    checks: list[tuple[bool, str]] = []
    if "is_draft" in expected:
        checks.append((node.is_draft == expected["is_draft"], f"是否起草 expected={expected['is_draft']} actual={node.is_draft}"))
    if "opinion_label" in expected:
        checks.append((_text_match(node.opinion_label or "", expected["opinion_label"]), f"意见域 expected={expected['opinion_label']} actual={node.opinion_label}"))
    if "opinion" in expected:
        opinion_text = _opinion_text(node)
        checks.append((_opinion_match(node, str(expected["opinion"])), f"意见配置 expected={expected['opinion']} actual={opinion_text}"))
    if "handler_source_field" in expected:
        actual_field = node.handler.source_field if node.handler else ""
        checks.append((_text_match(actual_field or "", expected["handler_source_field"]), f"处理人来源字段 expected={expected['handler_source_field']} actual={actual_field}"))
    if "role_label" in expected:
        actual_role = node.handler.role if node.handler else ""
        checks.append((_text_match(actual_role, expected["role_label"]), f"处理角色 expected={expected['role_label']} actual={actual_role}"))
    return _checks_result(item, weight, checks)


def _evaluate_handler_item(item: dict[str, Any], actual: ProcessDefinition, weight: float) -> dict[str, Any]:
    node_name = _node_name_from_projection(item) or str(item.get("name") or "")
    node = _find_node(actual, node_name)
    if node is None or node.handler is None:
        return _item_result(item, weight, 0.0, "missing", [f"缺少处理人配置：{node_name}。"])
    expected = item.get("expected", {})
    checks: list[tuple[bool, str]] = []
    if "mode" in expected:
        checks.append((_text_match(node.handler.mode.value, expected["mode"]), f"处理模式 expected={expected['mode']} actual={node.handler.mode.value}"))
    if "source_field" in expected:
        checks.append((_text_match(node.handler.source_field or "", expected["source_field"]), f"source_field expected={expected['source_field']} actual={node.handler.source_field}"))
    if "role_label" in expected:
        checks.append((_text_match(node.handler.role, expected["role_label"]), f"role expected={expected['role_label']} actual={node.handler.role}"))
    return _checks_result(item, weight, checks)


def _evaluate_submit_path_item(item: dict[str, Any], actual: ProcessDefinition, weight: float) -> dict[str, Any]:
    expected = item.get("expected", {})
    source_node_name = str(expected.get("source_node_name") or "")
    path_name = str(expected.get("path_name") or item.get("name") or "")
    node = _find_node(actual, source_node_name)
    if node is None:
        return _item_result(item, weight, 0.0, "missing_source_node", [f"缺少来源环节：{source_node_name}。"])
    path = _find_path(node, path_name)
    if path is None:
        return _item_result(item, weight, 0.0, "missing", [f"缺少路径：{source_node_name} / {path_name}。"])
    checks: list[tuple[bool, str]] = []
    if "condition" in expected:
        checks.append((_condition_match(path.condition or "", expected["condition"]), f"路径条件 expected={expected['condition']} actual={path.condition}"))
    if "target_node_name" in expected:
        actual_target = _target_name(actual, path.target_node_id)
        checks.append((_text_match(actual_target, expected["target_node_name"]), f"目标环节 expected={expected['target_node_name']} actual={actual_target}"))
    return _checks_result(item, weight, checks)


def _evaluate_clarification_target(item: dict[str, Any], clarifications: list[dict[str, Any]]) -> dict[str, Any]:
    weight = float(item.get("score_weight", 1))
    needles = [str(token) for token in item.get("question_contains", []) if str(token).strip()]
    if not needles and item.get("topic"):
        needles = [str(item["topic"])]
    matched_text = ""
    matched = False
    target_payload = {**item, "expected_question_contains": needles}
    for card in clarifications:
        match = match_clarification(target_payload, card)
        if match:
            matched = True
            matched_text = match.get("actual_text") or _clarification_text(card)
            break
    return {
        "id": item.get("id"),
        "topic": item.get("topic"),
        "matched": matched,
        "score": weight if matched else 0.0,
        "weight": weight,
        "matched_text": matched_text,
        "expected_terms": needles,
    }


def _evaluate_forbidden_item(item: dict[str, Any], actual: ProcessDefinition | None) -> dict[str, Any]:
    item_type = item.get("type")
    hit = False
    reason = ""
    if actual is not None and item_type == "custom_role" and actual.roles:
        hit = True
        reason = f"实际输出了流程自定义角色：{[role.role_name for role in actual.roles]}"
    elif actual is not None and item_type == "attachment" and actual.attachments:
        hit = True
        reason = f"实际输出了附件配置：{[item.attachment_type for item in actual.attachments]}"
    penalty = float(item.get("penalty", 1.0)) if hit else 0.0
    return {
        "id": item.get("id"),
        "type": item_type,
        "description": item.get("description"),
        "hit": hit,
        "penalty": penalty,
        "reason": reason,
    }


def _checks_result(item: dict[str, Any], weight: float, checks: list[tuple[bool, str]]) -> dict[str, Any]:
    if not checks:
        return _item_result(item, weight, weight, "matched", [])
    passed = sum(1 for ok, _ in checks if ok)
    score = weight * passed / len(checks)
    details = [detail for ok, detail in checks if not ok]
    status = "matched" if passed == len(checks) else ("partial" if passed else "mismatch")
    return _item_result(item, weight, score, status, details)


def _item_result(item: dict[str, Any], weight: float, score: float, status: str, details: list[str]) -> dict[str, Any]:
    return {
        "id": item.get("id"),
        "category": item.get("category"),
        "name": item.get("name"),
        "status": status,
        "score": round(score, 4),
        "weight": weight,
        "details": details,
    }


def _find_field(process: ProcessDefinition, field_name: str) -> FormField | None:
    return _best_match(process.form_fields, field_name, lambda item: item.field_name)


def _find_node(process: ProcessDefinition, node_name: str) -> FlowNode | None:
    return _best_match(process.flow_nodes, node_name, lambda item: item.node_name)


def _find_path(node: FlowNode, path_name: str) -> Any | None:
    return _best_match(node.submit_paths, path_name, lambda item: item.path_name)


def _best_match(items: list[Any], expected: str, key_fn: Any) -> Any | None:
    if not expected:
        return None
    expected_norm = _norm(expected)
    for item in items:
        if _norm(key_fn(item)) == expected_norm:
            return item
    for item in items:
        actual_norm = _norm(key_fn(item))
        if expected_norm in actual_norm or actual_norm in expected_norm:
            return item
    return None


def _target_name(process: ProcessDefinition, target_node_id: str) -> str:
    if target_node_id == "END":
        return "流程结束"
    if target_node_id == "DRAFT":
        return "退回起草"
    node = process.get_node_by_id(target_node_id)
    return node.node_name if node else target_node_id


def _node_name_from_projection(item: dict[str, Any]) -> str:
    path = item.get("process_definition_projection", {}).get("path", "")
    match = re.search(r"flow_nodes\[node_name=([^\]]+)\]", path)
    return match.group(1) if match else ""


def _opinion_text(node: FlowNode) -> str:
    if node.opinion is None:
        return ""
    return (
        f"结论性意见={'必填' if node.opinion.conclusive_required else '非必填'}；"
        f"意见详情={'必填' if node.opinion.detail_required else '非必填'}；"
        f"选项={'、'.join(node.opinion.conclusive_options)}"
    )


def _opinion_match(node: FlowNode, expected: str) -> bool:
    if node.opinion is None:
        return not expected.strip()
    expected_norm = _norm(expected)
    checks: list[bool] = []
    if "非结论性意见" in expected_norm or "非结论性" in expected_norm:
        checks.append(node.opinion.conclusive_required is False)
    elif "结论性意见" in expected_norm or "结论性" in expected_norm:
        checks.append(node.opinion.conclusive_required is True)
    if "意见详情非必填" in expected_norm or "详情非必填" in expected_norm:
        checks.append(node.opinion.detail_required is False)
    elif "意见详情必填" in expected_norm or "详情必填" in expected_norm:
        checks.append(node.opinion.detail_required is True)
    if checks:
        return all(checks)
    return _contains_any(_opinion_text(node), _tokens(expected))


def _stage_list_match(actual: list[str], expected: list[str]) -> bool:
    return {_norm(item) for item in actual} == {_norm(item) for item in expected}


def _options_cover(actual: list[str], expected: list[str]) -> bool:
    actual_norm = {_norm(item) for item in actual}
    return all(_norm(item) in actual_norm for item in expected)


def _condition_match(actual: str, expected: str) -> bool:
    if _text_match(actual, expected):
        return True
    actual_norm = _norm(actual).replace("且", "").replace("并且", "")
    expected_norm = _norm(expected).replace("且", "").replace("并且", "")
    return expected_norm in actual_norm or actual_norm in expected_norm


def _text_match(actual: Any, expected: Any) -> bool:
    if expected is None:
        return actual is None
    actual_norm = _norm(actual)
    expected_norm = _norm(expected)
    return actual_norm == expected_norm or expected_norm in actual_norm or actual_norm in expected_norm


def _contains_all(text: str, tokens: list[str]) -> bool:
    norm_text = _norm(text)
    return all(_norm(token) in norm_text for token in tokens)


def _contains_any(text: str, tokens: list[str]) -> bool:
    norm_text = _norm(text)
    return any(_norm(token) in norm_text for token in tokens)


def _tokens(text: str) -> list[str]:
    return [token for token in re.split(r"[；;，,、\s]+", str(text)) if token]


def _clarification_text(card: dict[str, Any]) -> str:
    return " ".join(
        str(value)
        for value in [
            card.get("id"),
            card.get("topic"),
            card.get("question"),
            card.get("recommendation"),
            card.get("reason"),
            card.get("item"),
            card.get("message"),
            card.get("issue_type"),
            json.dumps(card.get("options", []), ensure_ascii=False),
        ]
        if value
    )


def _norm(value: Any) -> str:
    return normalize_text(value)


def _actual_summary(actual: ProcessDefinition | None, clarifications: list[dict[str, Any]]) -> dict[str, Any]:
    if actual is None:
        return {"process_definition_present": False, "clarification_count": len(clarifications)}
    return {
        "process_definition_present": True,
        "process_name": actual.meta.process_name,
        "field_count": len(actual.form_fields),
        "node_count": len(actual.flow_nodes),
        "path_count": sum(len(node.submit_paths) for node in actual.flow_nodes),
        "attachment_count": len(actual.attachments or []),
        "role_count": len(actual.roles or []),
        "clarification_count": len(clarifications),
    }


def _status(
    score: float,
    item_results: list[dict[str, Any]],
    clarification_results: list[dict[str, Any]],
    forbidden_results: list[dict[str, Any]],
) -> str:
    if any(item.get("hit") for item in forbidden_results):
        return "fail"
    if any(item.get("status") == "missing" and item.get("weight", 0) >= 4 for item in item_results):
        return "fail"
    if score >= 85 and all(item.get("matched") for item in clarification_results):
        return "pass"
    if score >= 70:
        return "needs_review"
    return "fail"


def _recommendations(
    item_results: list[dict[str, Any]],
    clarification_results: list[dict[str, Any]],
    forbidden_results: list[dict[str, Any]],
) -> list[str]:
    recommendations: list[str] = []
    if any(item.get("status") == "missing" for item in item_results):
        recommendations.append("优先检查缺失的字段、环节或路径是否在 source 中被解析进 source_context。")
    if any(item.get("status") == "partial" for item in item_results):
        recommendations.append("对 partial 项检查属性级差异，通常是组件类型、路径条件或处理人来源表达不完整。")
    if any(not item.get("matched") for item in clarification_results):
        recommendations.append("BusinessValidationAgent 需要把 GoldSource 不确定项转成待确认卡片。")
    if any(item.get("hit") for item in forbidden_results):
        recommendations.append("抽取 prompt 需要继续收紧，禁止编造 source 没有的附件或流程自定义角色。")
    return recommendations


def main() -> None:
    parser = argparse.ArgumentParser(description="Evaluate a LangGraph run against eval-schema target JSON.")
    parser.add_argument("--run", required=True, help="Run directory.")
    parser.add_argument("--target", required=True, help="Eval target JSON path.")
    parser.add_argument("--out", help="Output directory. Defaults to <run>/eval_schema.")
    args = parser.parse_args()

    report = evaluate_run_against_eval_schema(args.run, args.target)
    out_dir = Path(args.out) if args.out else Path(args.run) / "eval_schema"
    paths = write_eval_schema_report(report, out_dir)
    print(
        json.dumps(
            {"status": report["status"], "score": report["score"], "paths": {key: str(value) for key, value in paths.items()}},
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
