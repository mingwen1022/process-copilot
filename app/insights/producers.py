"""反哺 producers：把运行侧原始事件映射成 InsightStore 记录（过闸1：kind gate）。

OpsInsightProducer（运维臂）：诊断命中**设计相关**根因（design_gap/org_gap）时写/累加；
data_error（用户填错）不写——那是用户的事、不是设计缺陷。详见 doc/反哺闭环-设计.md §4.1/§4.4。
"""

from __future__ import annotations

from typing import Any, Optional

from app.insights.store import InsightStore
from data.schema import InsightKind, InsightSource, ProcessInsight

# 运维 stuck_kind → InsightKind（闸1：只放行设计相关；data_error 不在表里→不反哺）
_STUCK_KIND_MAP: dict[str, InsightKind] = {
    "design_gap": InsightKind.COVERAGE_GAP,
    "org_gap": InsightKind.ORG_GAP,
}


class OpsInsightProducer:
    """运维臂：诊断结论（确定性 stuck_kind）→ 反哺洞察。"""

    @staticmethod
    def record_from_diagnosis(
        store: InsightStore,
        *,
        workflow_definition_id: str,
        node_id: Optional[str],
        stuck_kind: Optional[str],
        node_name: Optional[str] = None,
        blocked_paths: Optional[list[dict[str, Any]]] = None,
        version: Optional[str] = None,
        at: Optional[str] = None,
    ) -> Optional[ProcessInsight]:
        """命中设计相关根因→记录（幂等聚合）；否则返回 None（不反哺）。"""
        kind = _STUCK_KIND_MAP.get(stuck_kind or "")
        if kind is None:
            return None  # data_error / 未卡住 / 其他 → 不反哺
        label = node_name or node_id  # 有环节名用名，回退到 node_id
        headline = {
            InsightKind.COVERAGE_GAP: f"「{label}」存在覆盖漏洞：某区间无审批路径，落进的申请卡住",
            InsightKind.ORG_GAP: f"「{label}」审批人解析为空，无人可派",
        }[kind]
        return store.record(
            workflow_definition_id=workflow_definition_id,
            node_id=node_id,
            kind=kind,
            severity="high",
            source=InsightSource.OPS,
            evidence={"blocked_paths": blocked_paths or []},
            window="近30天",
            headline=headline,
            pertains_to_version=version,
            at=at,
        )


# 分析候选 category → InsightKind。"dead_node" 从未被 detect_candidates 产出过，之前是个
# 幽灵映射，不放进来。conformance_violation/high_rework/manual_intervention 三类是**流程级**
# 候选（不落在单个环节上，node_id 恒为 None）——ProcessInsight.node_id 本就支持 None（"流程级"），
# 所以这三类不需要额外的 node_id 才能反哺，落地方式跟其余三类一致，只是记录时 node_id=None。
_CATEGORY_MAP: dict[str, InsightKind] = {
    "slow_node": InsightKind.SLOW_NODE,
    "high_return_rate": InsightKind.HIGH_RETURN,
    "sla_breach": InsightKind.SLA_BREACH,
    "conformance_violation": InsightKind.CONFORMANCE_VIOLATION,
    "high_rework": InsightKind.HIGH_REWORK,
    "manual_intervention": InsightKind.MANUAL_INTERVENTION,
}

# 供调用方（如对话式反哺工具）判断"这个候选类别能不能反哺"，不用各处重复这份白名单。
SUPPORTED_ANALYTICS_CATEGORIES: frozenset[str] = frozenset(_CATEGORY_MAP)


class AnalyticsInsightProducer:
    """分析臂：detect_candidates 的越阈候选（确定性）→ 反哺洞察。与运维臂对称。"""

    @staticmethod
    def record_from_candidates(
        store: InsightStore,
        *,
        workflow_definition_id: str,
        candidates: list[Any],
        window: str = "近30天",
        version: Optional[str] = None,
        at: Optional[str] = None,
    ) -> list[ProcessInsight]:
        recorded: list[ProcessInsight] = []
        for c in candidates:
            kind = _CATEGORY_MAP.get(getattr(c, "category", ""))
            if kind is None:
                continue  # 只反哺有对应 InsightKind 的候选类别；node_id 可以是 None（流程级）
            recorded.append(
                store.record(
                    workflow_definition_id=workflow_definition_id,
                    node_id=getattr(c, "node_id", None),
                    kind=kind,
                    severity=getattr(c, "severity", "medium"),
                    source=InsightSource.ANALYTICS,
                    evidence=getattr(c, "metric_reference", {}) or {},
                    window=window,
                    headline=getattr(c, "description", "") or "",
                    pertains_to_version=version,
                    at=at,
                )
            )
        return recorded
