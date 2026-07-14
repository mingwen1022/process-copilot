"""InsightStore：反哺洞察的存储（内存形态，与 copilots 一致；SQLite 留后续替换）。

- record()：幂等 upsert——按 (workflow_definition_id, node_id, kind) 聚合，命中活跃洞察则
  occurrences++ / 刷新 last_seen / evidence；否则新建一条。resolved 的保留在列表里（供版本对比）。
- open_for()：设计侧/流程管理读取该流程的 open+acknowledged 洞察。
- by_channel()：多渠道分诊——流程管理卡片按 channel 分类展示。
- 生命周期：acknowledge / resolve。

kind→channel 是**固定映射表**（确定性，非判断）：能确定根因的落对应渠道，二义的落 triage。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

from data.schema import (
    InsightChannel,
    InsightKind,
    InsightSource,
    InsightStatus,
    ProcessInsight,
)

# kind → channel 固定映射（详见 doc/反哺闭环-设计.md §7）
_KIND_CHANNEL: dict[InsightKind, InsightChannel] = {
    InsightKind.COVERAGE_GAP: InsightChannel.DESIGN,
    InsightKind.RECURRING_DATA_FIX: InsightChannel.DESIGN,
    InsightKind.RECURRING_OVERRIDE: InsightChannel.DESIGN,
    InsightKind.HIGH_RETURN: InsightChannel.DESIGN,
    InsightKind.ORG_GAP: InsightChannel.ORG,
    InsightKind.SLOW_NODE: InsightChannel.TRIAGE,   # 设计 or 资源，确定性判不了
    InsightKind.SLA_BREACH: InsightChannel.TRIAGE,
    InsightKind.DEAD_NODE: InsightChannel.TRIAGE,
    InsightKind.CONFORMANCE_VIOLATION: InsightChannel.DESIGN,  # 路由条件跟实际流转对不上，根因明确是设计
    InsightKind.HIGH_REWORK: InsightChannel.TRIAGE,   # 表单质量/审核标准/设计都可能，确定性判不了
    InsightKind.MANUAL_INTERVENTION: InsightChannel.TRIAGE,  # 系统稳定性/数据/流程设计都可能，确定性判不了
}


def channel_for(kind: InsightKind) -> InsightChannel:
    return _KIND_CHANNEL.get(kind, InsightChannel.TRIAGE)


class InsightStore:
    def __init__(self) -> None:
        self._insights: list[ProcessInsight] = []
        self._seq = 0

    def clear(self) -> None:
        """清空所有洞察，回到刚构造时的空态。供演示"切回起始态"重建内存用——
        清完由调用方重跑启动播种（seed_reback_history 等），等价于进程刚起来的样子。"""
        self._insights = []
        self._seq = 0

    # ——— 写入（幂等 upsert） ———

    def record(
        self,
        *,
        workflow_definition_id: str,
        node_id: Optional[str],
        kind: InsightKind,
        severity: str,
        source: InsightSource,
        evidence: Optional[dict[str, Any]] = None,
        window: str = "",
        headline: str = "",
        suggested_fix: Optional[str] = None,
        pertains_to_version: Optional[str] = None,
        at: Optional[str] = None,
    ) -> ProcessInsight:
        """按聚合键 upsert。命中活跃洞察→累加；否则新建。at 可注入（供测试确定性）。"""
        at = at or datetime.now().isoformat(timespec="seconds")
        key = (workflow_definition_id, node_id, kind)
        existing = self._active(key)
        if existing is not None:
            existing.occurrences += 1
            existing.last_seen = at
            if evidence:
                existing.evidence = evidence  # 刷新为最新证据
            if headline:
                existing.headline = headline
            return existing

        self._seq += 1
        insight = ProcessInsight(
            insight_id=f"insight_{self._seq}",
            workflow_definition_id=workflow_definition_id,
            node_id=node_id,
            kind=kind,
            channel=channel_for(kind),
            severity=severity,
            source=source,
            evidence=evidence or {},
            occurrences=1,
            window=window,
            headline=headline,
            suggested_fix=suggested_fix,
            status=InsightStatus.OPEN,
            first_seen=at,
            last_seen=at,
            pertains_to_version=pertains_to_version,
        )
        self._insights.append(insight)
        return insight

    # ——— 读取 ———

    def open_for(self, workflow_definition_id: str) -> list[ProcessInsight]:
        """该流程未消解（open + acknowledged）的洞察，供设计侧/流程管理读取。"""
        return [
            i
            for i in self._insights
            if i.workflow_definition_id == workflow_definition_id
            and i.status != InsightStatus.RESOLVED
        ]

    def by_channel(self, workflow_definition_id: str, channel: InsightChannel) -> list[ProcessInsight]:
        return [i for i in self.open_for(workflow_definition_id) if i.channel == channel]

    def channel_counts(self, workflow_definition_id: str) -> dict[str, int]:
        """流程管理卡片「运行反馈」用：分渠道计数。"""
        counts: dict[str, int] = {}
        for i in self.open_for(workflow_definition_id):
            counts[i.channel.value] = counts.get(i.channel.value, 0) + 1
        return counts

    def all_insights(self) -> list[ProcessInsight]:
        return list(self._insights)

    def get(self, insight_id: str) -> Optional[ProcessInsight]:
        return next((i for i in self._insights if i.insight_id == insight_id), None)

    # ——— 生命周期 ———

    def acknowledge(self, insight_id: str) -> None:
        i = self.get(insight_id)
        if i is not None and i.status == InsightStatus.OPEN:
            i.status = InsightStatus.ACKNOWLEDGED

    def resolve(self, insight_id: str) -> None:
        i = self.get(insight_id)
        if i is not None:
            i.status = InsightStatus.RESOLVED

    # ——— 内部 ———

    def _active(self, key: tuple) -> Optional[ProcessInsight]:
        """某聚合键当前活跃（非 resolved）的洞察。resolved 后同键复发会新建一条。"""
        return next(
            (i for i in self._insights if i.key() == key and i.status != InsightStatus.RESOLVED),
            None,
        )
