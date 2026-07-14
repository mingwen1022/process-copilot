"""候选堵点识别（分析侧闭环 Phase 3.1 · 确定性部分）。

从 compute_metrics() 的输出里，用可配置阈值规则筛出"值得关注的候选堵点"。
这一层不含任何 LLM——是什么、有多严重，全部是确定性判断；LLM 只在下一层
（process_analysis_agent）负责"为什么"和"怎么改"。

候选堵点的 category 刻意跟 Phase 1 的注入病灶类型对齐（DefectType），这样
Phase 4 评测时可以直接用"注入了哪些病灶 vs 检出了哪些候选"算检出率。

已知缺口（诚实标注，不假装覆盖）：僵尸/积压实例（ZOMBIE_INSTANCE）需要
"在途实例已滞留多久"这个信号才能跟"正常的近期在途"区分开，而当前指标层
只有状态计数、没有在途时长，所以这里暂不检测僵尸——留给后续给指标层补一个
在途滞留时长后再加（Phase 4 会如实反映这一项未覆盖）。
"""

from __future__ import annotations

from statistics import median
from typing import Any

from pydantic import BaseModel, Field

# 候选堵点类别（与 data.schema.DefectType 语义对齐，便于 Phase 4 检出率映射）
CATEGORY_SLOW_NODE = "slow_node"
CATEGORY_HIGH_RETURN_RATE = "high_return_rate"
CATEGORY_SLA_BREACH = "sla_breach"
CATEGORY_CONFORMANCE_VIOLATION = "conformance_violation"
CATEGORY_MANUAL_INTERVENTION = "manual_intervention"
CATEGORY_HIGH_REWORK = "high_rework"


class ThresholdConfig(BaseModel):
    """候选堵点判定阈值，全部可配置，默认值针对请假 v1 场景标定。"""

    slow_node_median_multiple: float = Field(default=3.0, description="环节平均处理时长超过全流程中位数的多少倍算慢")
    slow_node_min_hours: float = Field(default=24.0, description="慢环节的绝对下限（小时），避免整体都很快时误报")
    slow_node_high_severity_multiple: float = Field(default=6.0, description="超过中位数多少倍算 high 严重度")
    high_return_rate: float = Field(default=0.2, description="环节退回率超过多少算高退回")
    high_return_high_severity: float = Field(default=0.35, description="退回率超过多少算 high 严重度")
    sla_breach_rate: float = Field(default=0.8, description="SLA 达成率低于多少算超时违约")
    sla_breach_high_severity: float = Field(default=0.5, description="SLA 达成率低于多少算 high 严重度")
    manual_intervention_ratio: float = Field(default=0.05, description="人工介入案例比例超过多少算异常")
    high_rework_rate: float = Field(default=0.3, description="整体返工率超过多少算高返工")


class CandidateBottleneck(BaseModel):
    bottleneck_id: str = Field(description="稳定 id，形如 'slow_node:line_leader'，供 LLM 归因与 Phase4 映射引用")
    category: str
    node_id: str | None = None
    node_name: str | None = None
    severity: str = Field(description="high / medium，确定性判定，不经 LLM")
    description: str = Field(description="确定性生成的中文描述，含具体数字")
    metric_reference: dict[str, Any] = Field(default_factory=dict, description="支撑该候选的关键指标数字")


def detect_candidates(metrics: dict[str, Any], config: ThresholdConfig | None = None) -> list[CandidateBottleneck]:
    config = config or ThresholdConfig()
    node_names = metrics.get("node_names", {})
    node_metrics = metrics.get("node_metrics", {})
    candidates: list[CandidateBottleneck] = []

    candidates.extend(_detect_slow_nodes(node_metrics, node_names, config))
    candidates.extend(_detect_high_return_nodes(node_metrics, node_names, config))
    candidates.extend(_detect_sla_breach_nodes(node_metrics, node_names, config))
    candidates.extend(_detect_conformance(metrics, config))
    candidates.extend(_detect_manual_intervention(metrics, config))
    candidates.extend(_detect_high_rework(metrics, config))

    severity_rank = {"high": 0, "medium": 1}
    candidates.sort(key=lambda c: (severity_rank.get(c.severity, 9), c.bottleneck_id))
    return candidates


def _node_label(node_id: str, node_names: dict[str, str]) -> str:
    return node_names.get(node_id, node_id)


def _detect_slow_nodes(node_metrics, node_names, config) -> list[CandidateBottleneck]:
    dwells = [m["avg_dwell_hours"] for m in node_metrics.values() if m.get("avg_dwell_hours") is not None]
    if not dwells:
        return []
    median_dwell = median(dwells)
    threshold = max(median_dwell * config.slow_node_median_multiple, config.slow_node_min_hours)
    result: list[CandidateBottleneck] = []
    for node_id, m in node_metrics.items():
        dwell = m.get("avg_dwell_hours")
        if dwell is None or dwell < threshold:
            continue
        multiple = dwell / median_dwell if median_dwell else 0.0
        severity = "high" if multiple >= config.slow_node_high_severity_multiple else "medium"
        result.append(
            CandidateBottleneck(
                bottleneck_id=f"{CATEGORY_SLOW_NODE}:{node_id}",
                category=CATEGORY_SLOW_NODE,
                node_id=node_id,
                node_name=_node_label(node_id, node_names),
                severity=severity,
                description=f"环节“{_node_label(node_id, node_names)}”平均处理时长 {dwell:.1f} 小时，"
                f"约为全流程各环节中位数（{median_dwell:.1f} 小时）的 {multiple:.1f} 倍。",
                metric_reference={"avg_dwell_hours": dwell, "median_dwell_hours": round(median_dwell, 2), "multiple": round(multiple, 2)},
            )
        )
    return result


def _detect_high_return_nodes(node_metrics, node_names, config) -> list[CandidateBottleneck]:
    result: list[CandidateBottleneck] = []
    for node_id, m in node_metrics.items():
        rate = m.get("return_rate")
        if rate is None or rate < config.high_return_rate:
            continue
        severity = "high" if rate >= config.high_return_high_severity else "medium"
        result.append(
            CandidateBottleneck(
                bottleneck_id=f"{CATEGORY_HIGH_RETURN_RATE}:{node_id}",
                category=CATEGORY_HIGH_RETURN_RATE,
                node_id=node_id,
                node_name=_node_label(node_id, node_names),
                severity=severity,
                description=f"环节“{_node_label(node_id, node_names)}”退回率 {rate * 100:.1f}%，高于告警阈值 {config.high_return_rate * 100:.0f}%。",
                metric_reference={"return_rate": rate, "avg_visits_per_case": m.get("avg_visits_per_case")},
            )
        )
    return result


def _detect_sla_breach_nodes(node_metrics, node_names, config) -> list[CandidateBottleneck]:
    result: list[CandidateBottleneck] = []
    for node_id, m in node_metrics.items():
        rate = m.get("sla_achievement_rate")
        if rate is None or rate >= config.sla_breach_rate:
            continue
        severity = "high" if rate < config.sla_breach_high_severity else "medium"
        result.append(
            CandidateBottleneck(
                bottleneck_id=f"{CATEGORY_SLA_BREACH}:{node_id}",
                category=CATEGORY_SLA_BREACH,
                node_id=node_id,
                node_name=_node_label(node_id, node_names),
                severity=severity,
                description=f"环节“{_node_label(node_id, node_names)}”时限达成率仅 {rate * 100:.1f}%，低于目标 {config.sla_breach_rate * 100:.0f}%。",
                metric_reference={"sla_achievement_rate": rate},
            )
        )
    return result


def _detect_conformance(metrics, config) -> list[CandidateBottleneck]:
    path = metrics.get("path_metrics", {})
    count = path.get("conformance_violation_count", 0)
    if not count:
        return []
    return [
        CandidateBottleneck(
            bottleneck_id=f"{CATEGORY_CONFORMANCE_VIOLATION}:process",
            category=CATEGORY_CONFORMANCE_VIOLATION,
            severity="high",
            description=f"检出 {count} 次结构性违规跳级（实际流转与流程定义的路由条件不一致）。",
            metric_reference={"conformance_violation_count": count},
        )
    ]


def _detect_manual_intervention(metrics, config) -> list[CandidateBottleneck]:
    overview = metrics.get("overview", {})
    ratio = overview.get("manual_intervention_case_ratio")
    if ratio is None or ratio < config.manual_intervention_ratio:
        return []
    manual = metrics.get("manual_intervention", {})
    return [
        CandidateBottleneck(
            bottleneck_id=f"{CATEGORY_MANUAL_INTERVENTION}:process",
            category=CATEGORY_MANUAL_INTERVENTION,
            severity="medium",
            description=f"人工运维介入比例 {ratio * 100:.1f}%，高于告警阈值 {config.manual_intervention_ratio * 100:.0f}%，"
            f"共 {overview.get('manual_intervention_case_count', 0)} 个案例需线下补登。",
            metric_reference={
                "manual_intervention_case_ratio": ratio,
                "by_prior_node": manual.get("by_prior_node", {}),
            },
        )
    ]


def _detect_high_rework(metrics, config) -> list[CandidateBottleneck]:
    path = metrics.get("path_metrics", {})
    rate = path.get("rework_rate")
    if rate is None or rate < config.high_rework_rate:
        return []
    return [
        CandidateBottleneck(
            bottleneck_id=f"{CATEGORY_HIGH_REWORK}:process",
            category=CATEGORY_HIGH_REWORK,
            severity="medium",
            description=f"整体返工率 {rate * 100:.1f}%（含至少一次退回的案例占比），高于告警阈值 {config.high_rework_rate * 100:.0f}%。",
            metric_reference={"rework_rate": rate, "rework_case_count": path.get("rework_case_count")},
        )
    ]
