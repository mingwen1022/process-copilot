from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class ConditionResult:
    matched: bool
    reason: str | None = None


def evaluate_condition(condition: str | None, context: dict[str, Any]) -> ConditionResult:
    if not condition:
        return ConditionResult(True)

    text = _normalize(condition)
    if text in {"上游环节退回时"}:
        return ConditionResult(bool(context.get("upstream_return")), None if context.get("upstream_return") else "需要上游退回触发")
    if text in {"需退回修改时"}:
        return ConditionResult(bool(context.get("return_requested")), None if context.get("return_requested") else "需要明确退回修改")

    return _evaluate_expr(text, context)


def _evaluate_expr(text: str, context: dict[str, Any]) -> ConditionResult:
    """paren-aware 布尔表达式求值：顶层 且 全真；其内 或 任一真；支持 非(...) 与 (...) 分组。"""
    and_parts = _split_top_level(text, {"且"})
    if len(and_parts) > 1:
        for part in and_parts:
            result = _evaluate_expr(part, context)
            if not result.matched:
                return result
        return ConditionResult(True)

    or_parts = _split_top_level(text, {"或"})
    if len(or_parts) > 1 and all(_looks_like_condition(part) for part in or_parts):
        last = ConditionResult(False, f"暂不支持条件: {text}")
        for part in or_parts:
            last = _evaluate_expr(part, context)
            if last.matched:
                return ConditionResult(True)
        return last

    unit = text.strip()
    if unit.startswith("非(") and unit.endswith(")"):
        inner = _evaluate_expr(unit[2:-1], context)
        return ConditionResult(not inner.matched, None if not inner.matched else f"非(...) 内条件成立: {unit[2:-1]}")
    if unit.startswith("(") and unit.endswith(")") and _is_balanced_wrap(unit):
        return _evaluate_expr(unit[1:-1], context)
    return _evaluate_part(unit, context)


def _split_top_level(text: str, delimiters: set[str]) -> list[str]:
    parts: list[str] = []
    depth = 0
    current: list[str] = []
    for char in text:
        if char in "({":
            depth += 1
        elif char in ")}":
            depth = max(0, depth - 1)
        if depth == 0 and char in delimiters:
            if current:
                parts.append("".join(current))
            current = []
            continue
        current.append(char)
    if current:
        parts.append("".join(current))
    return [part for part in (item.strip() for item in parts) if part]


def _looks_like_condition(part: str) -> bool:
    """片段是否像一个独立条件（有比较算子/关键字）。用于区分'条件 或 条件'与'字段=值1或值2'。"""
    if part.startswith("(") or part.startswith("非("):
        return True
    if part in {"最后一人", "非最后一人"}:
        return True
    return bool(re.search(r"[=∈∉≥≤><＞＜]", part))


def _is_balanced_wrap(text: str) -> bool:
    """整个字符串是否被最外层一对括号包裹（避免把 (A)且(B) 误判为分组）。"""
    depth = 0
    for index, char in enumerate(text):
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth == 0 and index != len(text) - 1:
                return False
    return depth == 0


def _evaluate_part(part: str, context: dict[str, Any]) -> ConditionResult:
    if part == "最后一人":
        return ConditionResult(bool(context.get("is_last_task")), None if context.get("is_last_task") else "当前不是最后一人")
    if part == "非最后一人":
        return ConditionResult(not bool(context.get("is_last_task")), None if not context.get("is_last_task") else "当前是最后一人")

    opinion = context.get("结论性意见") or context.get("conclusive_opinion")
    if part.startswith("结论性意见="):
        expected = part.split("=", 1)[1]
        return _match_text_value("结论性意见", opinion, expected)

    membership = re.match(r"^(.+?)([∈∉])\{?(.+?)\}?$", part)
    if membership:
        field, operator, inner = membership.groups()
        options = [item for item in re.split(r"[，,、/]", inner) if item]
        actual_text = str(_context_value(context, field) or "")
        in_set = actual_text in options
        matched = in_set if operator == "∈" else not in_set
        reason = None if matched else f"{field}={actual_text!r} {'不在' if operator == '∈' else '在'} {options}"
        return ConditionResult(matched, reason)

    numeric = re.match(r"^(.+?)(>=|<=|≥|≤|>|<|＞|＜)(\d+(?:\.\d+)?)(?:元|万元|万|天|日|小时|人|次|个)?$", part)
    if numeric:
        field, operator, expected_raw = numeric.groups()
        actual_value = _context_value(context, field)
        actual = _number_value(actual_value)
        expected = float(expected_raw)
        matched = _compare_number(actual, operator, expected)
        reason = None if matched else f"{field}={actual_value!r} 不满足 {operator}{expected_raw}"
        return ConditionResult(matched, reason)

    equality = re.match(r"^(.+?)=(.+)$", part)
    if equality:
        field, expected = equality.groups()
        return _match_text_value(field, _context_value(context, field), expected)

    return ConditionResult(False, f"暂不支持条件: {part}")


def _match_text_value(field: str, actual: Any, expected: str) -> ConditionResult:
    actual_text = str(actual or "")
    options = [item for item in re.split(r"或|/", expected) if item]
    matched = actual_text in options if options else actual_text == expected
    reason = None if matched else f"{field}={actual_text!r} 不在 {options or [expected]}"
    return ConditionResult(matched, reason)


def _number_value(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, int | float):
        return float(value)
    match = re.search(r"\d+(?:\.\d+)?", str(value).replace(",", ""))
    return float(match.group(0)) if match else None


def _context_value(context: dict[str, Any], field: str) -> Any:
    if field in context:
        return context[field]
    normalized = _strip_unit_suffix(field)
    for key, value in context.items():
        if _strip_unit_suffix(str(key)) == normalized:
            return value
    for key, value in context.items():
        key_text = _strip_unit_suffix(str(key))
        if normalized and (normalized in key_text or key_text in normalized):
            return value
    return None


def _strip_unit_suffix(text: str) -> str:
    return re.sub(r"[（(].*?[）)]", "", text).strip()


def _compare_number(actual: float | None, operator: str, expected: float) -> bool:
    if actual is None:
        return False
    if operator in {">", "＞"}:
        return actual > expected
    if operator in {"<", "＜"}:
        return actual < expected
    if operator in {">=", "≥"}:
        return actual >= expected
    if operator in {"<=", "≤"}:
        return actual <= expected
    return False


def _normalize(text: str) -> str:
    return (
        text.strip()
        .replace(" ", "")
        .replace("大于等于", "≥")
        .replace("小于等于", "≤")
        .replace("大于", "＞")
        .replace("小于", "＜")
    )
