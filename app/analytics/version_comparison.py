"""v1 vs v2 效能对比（分析侧闭环 Phase 5 · headline）。

把改进前（v1）和改进后（v2）两份指标放一起算 delta，量化"流程被分析→被改进"
到底带来多少提升。纯确定性，无 LLM。

指标方向：办结时长/流转环节数/返工率/退回率/停留时长/人工介入/违规跳级 →
越低越好；SLA 达成率/办结率 → 越高越好。case-mix（假期类型分布、实例量）一并
输出，证明两版可比（同输入分布下的聚合对比）。
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

# 指标 key → (中文名, 方向)；方向 "down"=越低越好，"up"=越高越好
_OVERVIEW_METRICS = {
    "avg_case_duration_hours": ("平均办结时长(小时)", "down"),
    "avg_node_visits": ("平均流转环节数", "down"),
    "avg_last_node_dwell_hours": ("最后环节平均停滞(小时)", "down"),
    "manual_intervention_case_ratio": ("人工介入比例", "down"),
    "completion_rate": ("办结率", "up"),
}
_PATH_METRICS = {
    "rework_rate": ("返工率", "down"),
    "conformance_violation_count": ("违规跳级次数", "down"),
}
_NODE_METRICS = {
    "avg_dwell_hours": ("平均处理时长(小时)", "down"),
    "sla_achievement_rate": ("时限达成率", "up"),
    "return_rate": ("退回率", "down"),
}


class MetricDelta(BaseModel):
    key: str
    label: str
    direction: str
    v1: float | None
    v2: float | None
    delta: float | None = Field(description="v2 - v1")
    pct_change: float | None = Field(description="相对 v1 的变化比例；v1 为 0/None 时为 None")
    improved: bool | None = Field(description="按方向判断是否改善；无法判断时 None")


class NodeComparison(BaseModel):
    node_id: str
    node_name: str
    metrics: list[MetricDelta]


class VersionComparison(BaseModel):
    v1_case_count: int
    v2_case_count: int
    case_mix_v1: dict[str, Any] = Field(default_factory=dict)
    case_mix_v2: dict[str, Any] = Field(default_factory=dict)
    line_leader_reach_ratio_v1: float | None = None
    line_leader_reach_ratio_v2: float | None = None
    overview: list[MetricDelta] = Field(default_factory=list)
    path: list[MetricDelta] = Field(default_factory=list)
    nodes: list[NodeComparison] = Field(default_factory=list)
    headline: str = ""


def compare_versions(metrics_v1: dict[str, Any], metrics_v2: dict[str, Any]) -> VersionComparison:
    node_names = metrics_v2.get("node_names") or metrics_v1.get("node_names") or {}

    overview = [
        _delta(key, label, direction, metrics_v1.get("overview", {}).get(key), metrics_v2.get("overview", {}).get(key))
        for key, (label, direction) in _OVERVIEW_METRICS.items()
    ]
    path = [
        _delta(key, label, direction, metrics_v1.get("path_metrics", {}).get(key), metrics_v2.get("path_metrics", {}).get(key))
        for key, (label, direction) in _PATH_METRICS.items()
    ]

    v1_nodes, v2_nodes = metrics_v1.get("node_metrics", {}), metrics_v2.get("node_metrics", {})
    nodes: list[NodeComparison] = []
    for node_id in v1_nodes:
        node_deltas = [
            _delta(key, label, direction, v1_nodes[node_id].get(key), (v2_nodes.get(node_id) or {}).get(key))
            for key, (label, direction) in _NODE_METRICS.items()
            if v1_nodes[node_id].get(key) is not None or (v2_nodes.get(node_id) or {}).get(key) is not None
        ]
        nodes.append(NodeComparison(node_id=node_id, node_name=node_names.get(node_id, node_id), metrics=node_deltas))

    v1_count = metrics_v1.get("overview", {}).get("case_count", 0)
    v2_count = metrics_v2.get("overview", {}).get("case_count", 0)
    reach_v1 = _reach_ratio(v1_nodes, v1_count, "line_leader")
    reach_v2 = _reach_ratio(v2_nodes, v2_count, "line_leader")

    duration = next((d for d in overview if d.key == "avg_case_duration_hours"), None)
    rework = next((d for d in path if d.key == "rework_rate"), None)
    headline = _headline(duration, rework)

    return VersionComparison(
        v1_case_count=v1_count,
        v2_case_count=v2_count,
        case_mix_v1=metrics_v1.get("origination_metrics", {}).get("by_case_attribute", {}).get("请假类型", {}),
        case_mix_v2=metrics_v2.get("origination_metrics", {}).get("by_case_attribute", {}).get("请假类型", {}),
        line_leader_reach_ratio_v1=reach_v1,
        line_leader_reach_ratio_v2=reach_v2,
        overview=overview,
        path=path,
        nodes=nodes,
        headline=headline,
    )


def _delta(key: str, label: str, direction: str, v1: Any, v2: Any) -> MetricDelta:
    v1f = float(v1) if isinstance(v1, (int, float)) else None
    v2f = float(v2) if isinstance(v2, (int, float)) else None
    delta = round(v2f - v1f, 4) if v1f is not None and v2f is not None else None
    pct = round((v2f - v1f) / v1f, 4) if v1f not in (None, 0) and v2f is not None else None
    improved: bool | None = None
    if delta is not None:
        improved = delta < 0 if direction == "down" else delta > 0
        if delta == 0:
            improved = None
    return MetricDelta(key=key, label=label, direction=direction, v1=v1f, v2=v2f, delta=delta, pct_change=pct, improved=improved)


def _reach_ratio(node_metrics: dict[str, Any], case_count: int, node_id: str) -> float | None:
    node = node_metrics.get(node_id)
    if not node or not case_count:
        return None
    visits = node.get("visits")
    per_case = node.get("avg_visits_per_case") or 1
    touched = visits / per_case if per_case else visits
    return round(touched / case_count, 4)


def _headline(duration: MetricDelta | None, rework: MetricDelta | None) -> str:
    parts = []
    if duration and duration.v1 and duration.v2 is not None:
        pct = abs(duration.pct_change or 0) * 100
        parts.append(f"平均办结时长 {duration.v1:.0f}h → {duration.v2:.0f}h（{'↓' if duration.improved else '↑'}{pct:.0f}%）")
    if rework and rework.v1 is not None and rework.v2 is not None:
        parts.append(f"返工率 {rework.v1 * 100:.0f}% → {rework.v2 * 100:.0f}%")
    return "；".join(parts)


def render_comparison_markdown(cmp: VersionComparison) -> str:
    lines = [
        "# 请假流程 v1 vs v2 效能对比",
        "",
        f"**{cmp.headline}**",
        "",
        f"- 实例量：v1 {cmp.v1_case_count} / v2 {cmp.v2_case_count}（同输入分布、同种子；聚合对比）",
        f"- 到达条线分管领导比例：v1 {_pct(cmp.line_leader_reach_ratio_v1)} → v2 {_pct(cmp.line_leader_reach_ratio_v2)}（升级门槛 >7天→>14天的结构性效果）",
        f"- case-mix（请假类型）v1：{cmp.case_mix_v1}",
        f"- case-mix（请假类型）v2：{cmp.case_mix_v2}",
        "",
        "## 总览指标",
        "",
        _table_header(),
    ]
    lines += [_row(d) for d in cmp.overview]
    lines += ["", "## 路径指标", "", _table_header()]
    lines += [_row(d) for d in cmp.path]
    lines += ["", "## 各环节", ""]
    for node in cmp.nodes:
        lines.append(f"### {node.node_name}")
        lines.append("")
        lines.append(_table_header())
        lines += [_row(d) for d in node.metrics]
        lines.append("")
    return "\n".join(lines)


def _table_header() -> str:
    return "| 指标 | v1 | v2 | 变化 |\n|---|---|---|---|"


def _row(d: MetricDelta) -> str:
    mark = "✅" if d.improved else ("⚠️" if d.improved is False else "—")
    pct = f"（{d.pct_change * 100:+.0f}%）" if d.pct_change is not None else ""
    return f"| {d.label} | {_fmt(d.v1)} | {_fmt(d.v2)} | {mark} {_fmt(d.delta, signed=True)}{pct} |"


def _fmt(value: float | None, signed: bool = False) -> str:
    if value is None:
        return "—"
    return f"{value:+.2f}" if signed else f"{value:.2f}"


def _pct(value: float | None) -> str:
    return f"{value * 100:.1f}%" if value is not None else "—"
