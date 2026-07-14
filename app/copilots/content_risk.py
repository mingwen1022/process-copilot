"""审批协助 · 内容风险检查（确定性）。

把表单填报值 vs 制度阈值比对——如"酒店 1000/晚 超差旅标准 800/晚"。
规则由代码经 RAG **检索后传入**（retrieve-then-read），本模块只做**确定性比对**；
LLM 只组织摘要，不判违规。判定归代码。详见 doc/流程助手-待办副驾-设计.md §4。

VALUE_THRESHOLD 规则约定：
- `applicability_condition`：仅当条件满足才检（写法同提交路径条件，如"出差城市=一线"）。
- `requirement.params`：{field, op(<=/>=/<'/'>/==), threshold, message?}。
"""

from __future__ import annotations

import re
from typing import Any, Callable

from app.runtime.condition_eval import evaluate_condition
from data.schema import AtomicRule, CheckKind

# 费用明细里逐项金额的写法约定：数字后跟"元"（如"酒店 3000 元"）。"3 晚"这类不带"元"
# 的数字不会被当成金额，规避误加。求和后与报销总额比对，这是确定性核对、不靠 LLM。
_AMOUNT_IN_DETAIL = re.compile(r"(\d+(?:\.\d+)?)\s*元")


def check_expense_total_consistency(
    form_values: dict[str, Any],
    *,
    detail_field: str = "费用明细",
    total_field: str = "报销总额",
    tolerance: float = 1.0,
) -> list[dict[str, Any]]:
    """核对报销总额与费用明细各项之和是否一致——总额虚高/漏项都能确定性抓到。
    明细取不到金额、总额缺失时稳妥跳过（不误报）。差额在 tolerance 内视为一致。"""
    detail = form_values.get(detail_field)
    total_raw = form_values.get(total_field)
    if not isinstance(detail, str) or total_raw is None:
        return []
    items = [float(m) for m in _AMOUNT_IN_DETAIL.findall(detail)]
    if not items:
        return []
    try:
        total = float(total_raw)
    except (TypeError, ValueError):
        return []
    itemized = round(sum(items), 2)
    diff = round(total - itemized, 2)
    if abs(diff) <= tolerance:
        return []
    direction = "高于" if diff > 0 else "低于"
    return [
        {
            "rule_id": "expense.total_matches_itemized",
            "title": "报销总额须与费用明细合计一致",
            "field": total_field,
            "value": total,
            "op": "==",
            "threshold": itemized,
            "message": f"报销总额 {total:g} 元{direction}费用明细合计 {itemized:g} 元，差 {abs(diff):g} 元，请核对明细或总额。",
            "clause": "费用报销制度 · 金额填报",
        }
    ]

_OPS: dict[str, Callable[[float, float], bool]] = {
    "<=": lambda v, t: v <= t,
    ">=": lambda v, t: v >= t,
    "<": lambda v, t: v < t,
    ">": lambda v, t: v > t,
    "==": lambda v, t: v == t,
}


def check_content_risk(form_values: dict[str, Any], rules: list[AtomicRule]) -> list[dict[str, Any]]:
    """对每条 VALUE_THRESHOLD 规则：适用条件满足则比对字段值，越界出一条 violation。合规/不适用/取不到值→跳过。"""
    violations: list[dict[str, Any]] = []
    for r in rules:
        req = r.requirement
        if req is None or req.kind != CheckKind.VALUE_THRESHOLD:
            continue
        params = req.params or {}
        field = params.get("field")
        op = params.get("op", "<=")
        threshold = params.get("threshold")
        if field is None or threshold is None:
            continue
        # 适用条件（如"出差城市=一线"）——不满足则本规则不适用
        cond = r.applicability_condition
        if cond and not evaluate_condition(cond, form_values).matched:
            continue
        raw = form_values.get(field)
        if raw is None:
            continue
        try:
            value = float(raw)
        except (TypeError, ValueError):
            continue
        checker = _OPS.get(op)
        if checker is None or checker(value, float(threshold)):
            continue  # 合规（或未知运算符，稳妥放过）
        violations.append(
            {
                "rule_id": r.rule_id,
                "title": r.title,
                "field": field,
                "value": value,
                "op": op,
                "threshold": threshold,
                "message": params.get("message") or f"{field}={value} 超标（应 {op} {threshold}）",
                "clause": f"{r.provenance.source_doc} · {r.provenance.clause}",
            }
        )
    return violations
