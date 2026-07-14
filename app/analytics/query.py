"""临时/自定义指标查询（分析侧闭环 Phase 3.2 · 确定性执行层）。

刻意保持"只有一种查询形状"（过滤 + 聚合 + 可选分组），不做成通用 SQL/代码
生成 agent——因为类型化查询可以穷举测试、错不了；LLM 现写 SQL 会"错得很安静"
（算错的数字看起来和算对的一样权威，用户没有对照物）。真遇到答不了的问题，
往这个 schema 加字段/枚举值，不换架构。

同一套执行逻辑既服务"临时一次性提问"，也服务"转正的常态化指标"——区别只在
persist 开关：persist=True 的查询会存进 custom_metrics.json，之后看板和后续
对话都会自动带上它，不用建两套机制。
"""

from __future__ import annotations

import json
from pathlib import Path
from statistics import mean
from typing import Any, Literal

from pydantic import BaseModel, Field

from app.generation.event_log_generator import OPS_NODE_ID
from data.schema import ActionCategory, CaseRecord, CaseStatus

FilterOperator = Literal["=", "!=", ">", ">=", "<", "<=", "in"]


class MetricFilter(BaseModel):
    field: str = Field(description="要过滤的字段名，如 请假类型 / 请假天数 / node_name / action_category / case_status")
    operator: FilterOperator
    value: Any = Field(description="比较值；operator=in 时为列表")


class AdHocMetricQuery(BaseModel):
    name: str = Field(description="这个指标的简短中文名，如 超长请假快速通过数")
    scope: Literal["case", "event"] = Field(description="case=按流程实例统计；event=按环节任务统计")
    filters: list[MetricFilter] = Field(default_factory=list, description="过滤条件，多个之间是与关系")
    aggregate: Literal["count", "rate", "avg_dwell_hours"] = Field(
        description="count=计数；rate=占同 scope 全部的比例；avg_dwell_hours=平均时长（case 为办结时长，event 为环节停留时长）"
    )
    group_by: str | None = Field(default=None, description="可选：按某字段分组分别统计")
    persist: bool = Field(default=False, description="是否转正为常态化跟踪指标（存入 custom_metrics.json）")


class AdHocMetricResult(BaseModel):
    name: str
    scope: str
    aggregate: str
    group_by: str | None = None
    matched: int
    total: int
    value: Any = Field(description="标量结果（未分组）或 {分组值: 结果} 映射（分组）")


# ──────────────────────────────────────────────
# 行构建：把 CaseRecord 拍平成可过滤的行（case 粒度 / event 粒度）
# ──────────────────────────────────────────────

def _case_rows(cases: list[CaseRecord]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for case in cases:
        real_events = [e for e in case.events if e.node_id != OPS_NODE_ID]
        duration_hours = (
            (case.closed_at - case.created_at).total_seconds() / 3600
            if case.closed_at is not None
            else None
        )
        row = {
            "case_id": case.case_id,
            "case_status": case.case_status.value,
            "flow_code": case.flow_code,
            "flow_name": case.flow_name,
            "initiator_name": case.initiator_name,
            "initiator_dept_name": case.initiator_dept_name,
            "duration_hours": duration_hours,
            "node_visits": len(real_events),
            # 案例经过的环节集合（列表值）：让"这个实例有没有经过某环节"能在 case
            # 粒度上过滤，不用被迫下钻到 event 粒度。_match_filter 对列表值字段用
            # 成员关系判断（= 即"包含"），不是相等。
            "node_id": [e.node_id for e in real_events],
            "node_name": [e.node_name for e in real_events],
            "is_completed": case.case_status == CaseStatus.COMPLETED,
            "has_return": any(e.action_category == ActionCategory.RETURN for e in case.events),
            "has_manual_intervention": any(e.node_id == OPS_NODE_ID for e in case.events),
            **case.case_attributes,
        }
        rows.append(row)
    return rows


def _event_rows(cases: list[CaseRecord]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for case in cases:
        for event in case.events:
            rows.append(
                {
                    "case_id": case.case_id,
                    "node_id": event.node_id,
                    "node_name": event.node_name,
                    "action": event.action,
                    "action_category": event.action_category.value,
                    "resource_name": event.resource_name,
                    "resource_dept_name": event.resource_dept_name,
                    "task_order": event.task_order,
                    "dwell_hours": event.dwell_seconds / 3600 if event.dwell_seconds is not None else None,
                    "is_ops": event.node_id == OPS_NODE_ID,
                    **case.case_attributes,
                }
            )
    return rows


# ──────────────────────────────────────────────
# 过滤 + 聚合
# ──────────────────────────────────────────────

def _to_number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(str(value))
    except (TypeError, ValueError):
        return None


def _match_filter(row: dict[str, Any], flt: MetricFilter) -> bool:
    actual = row.get(flt.field)
    op = flt.operator
    if isinstance(actual, list):
        # 列表值字段（如案例经过的环节集合）：= / in 语义是"成员关系"（包含），
        # != 是"不包含"；数值比较对列表无意义，一律不匹配。
        values = flt.value if isinstance(flt.value, list) else [flt.value]
        contains = any(v in actual for v in values)
        if op in {"=", "in"}:
            return contains
        if op == "!=":
            return not contains
        return False
    if op == "in":
        options = flt.value if isinstance(flt.value, list) else [flt.value]
        return actual in options or str(actual) in {str(o) for o in options}
    if op in {">", ">=", "<", "<="}:
        a, b = _to_number(actual), _to_number(flt.value)
        if a is None or b is None:
            return False
        return {">": a > b, ">=": a >= b, "<": a < b, "<=": a <= b}[op]
    # = / != ：数值可比就按数值比，否则按字符串比
    a_num, b_num = _to_number(actual), _to_number(flt.value)
    if a_num is not None and b_num is not None:
        equal = a_num == b_num
    else:
        equal = str(actual) == str(flt.value)
    return equal if op == "=" else not equal


def _aggregate(rows: list[dict[str, Any]], aggregate: str, total: int) -> Any:
    if aggregate == "count":
        return len(rows)
    if aggregate == "rate":
        return round(len(rows) / total, 4) if total else None
    if aggregate == "avg_dwell_hours":
        key = "duration_hours" if rows and "duration_hours" in rows[0] else "dwell_hours"
        values = [r[key] for r in rows if r.get(key) is not None]
        return round(mean(values), 4) if values else None
    raise ValueError(f"不支持的聚合方式: {aggregate}")


def run_ad_hoc_query(query: AdHocMetricQuery, cases: list[CaseRecord]) -> AdHocMetricResult:
    rows = _case_rows(cases) if query.scope == "case" else _event_rows(cases)
    total = len(rows)
    matched = [row for row in rows if all(_match_filter(row, flt) for flt in query.filters)]

    if query.group_by:
        groups: dict[str, list[dict[str, Any]]] = {}
        for row in matched:
            groups.setdefault(str(row.get(query.group_by)), []).append(row)
        value: Any = {k: _aggregate(v, query.aggregate, total) for k, v in sorted(groups.items())}
    else:
        value = _aggregate(matched, query.aggregate, total)

    return AdHocMetricResult(
        name=query.name,
        scope=query.scope,
        aggregate=query.aggregate,
        group_by=query.group_by,
        matched=len(matched),
        total=total,
        value=value,
    )


# ──────────────────────────────────────────────
# 转正指标注册表（custom_metrics.json）
# ──────────────────────────────────────────────

def load_custom_metrics(path: str | Path) -> list[AdHocMetricQuery]:
    file = Path(path)
    if not file.exists():
        return []
    data = json.loads(file.read_text(encoding="utf-8"))
    return [AdHocMetricQuery.model_validate(item) for item in data]


def save_custom_metrics(path: str | Path, queries: list[AdHocMetricQuery]) -> None:
    file = Path(path)
    file.parent.mkdir(parents=True, exist_ok=True)
    payload = [q.model_dump(mode="json") for q in queries]
    file.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def upsert_custom_metric(path: str | Path, query: AdHocMetricQuery) -> list[AdHocMetricQuery]:
    """按 name 去重地登记一个转正指标（同名覆盖），返回登记后的完整列表。"""
    existing = load_custom_metrics(path)
    kept = [q for q in existing if q.name != query.name]
    stored = query.model_copy(update={"persist": True})
    kept.append(stored)
    save_custom_metrics(path, kept)
    return kept
