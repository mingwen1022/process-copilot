"""反哺闭环 · 运行洞察数据模型（模块8 · 闭环反馈）。

运行侧（运维/分析）确定性地把发现沉淀为「流程洞察」，设计侧在编辑该流程时读出并回流。
心法与全项目一致：检测 / 严重度 / 聚合去重 / 生命周期 = 确定性代码；LLM 只组织 headline 措辞。
聚合键 = (workflow_definition_id, node_id, kind)。详见 doc/反哺闭环-设计.md。
"""

from __future__ import annotations

from enum import Enum
from typing import Any, Optional

from pydantic import BaseModel, Field


class InsightKind(str, Enum):
    """洞察类型（有限集，与运维根因 + 分析检测器一一对应）。"""

    COVERAGE_GAP = "coverage_gap"              # 覆盖漏洞：区间无路径（运维 design_gap）
    ORG_GAP = "org_gap"                        # 岗位空缺无人可派（运维）
    RECURRING_DATA_FIX = "recurring_data_fix"  # 反复错填同一字段（运维决策段）
    RECURRING_OVERRIDE = "recurring_override"  # 反复同类 override（运维决策段）
    SLOW_NODE = "slow_node"                    # 环节停留过久（分析）
    HIGH_RETURN = "high_return"                # 退回率过高（分析）
    SLA_BREACH = "sla_breach"                  # 超时达成率低（分析）
    DEAD_NODE = "dead_node"                    # 某环节零/近零流量（分析）
    CONFORMANCE_VIOLATION = "conformance_violation"  # 结构性违规跳级：实际流转与路由条件不一致（分析，流程级）
    HIGH_REWORK = "high_rework"                # 整体返工率过高（分析，流程级）
    MANUAL_INTERVENTION = "manual_intervention"  # 人工运维介入比例过高（分析，流程级）


class InsightSource(str, Enum):
    OPS = "ops"
    ANALYTICS = "analytics"


class InsightChannel(str, Enum):
    """处置渠道（多渠道分诊）。本期只 design 走完整闭环，其余只暴露分类。"""

    DESIGN = "design"
    POLICY = "policy"
    ORG = "org"
    DATA = "data"
    OPS = "ops"
    TRIAGE = "triage"   # 根因二义，确定性判不了，待人分诊


class InsightStatus(str, Enum):
    OPEN = "open"                  # 新发现 / 仍存在
    ACKNOWLEDGED = "acknowledged"  # 设计者已看到 / 已进入修订
    RESOLVED = "resolved"          # 修订后确定性复检已消解


class ProcessInsight(BaseModel):
    """一条聚合后的流程洞察。原始事件（OpsIncident/CandidateBottleneck）过闸+聚合后落成这个。"""

    insight_id: str
    workflow_definition_id: str = Field(description="归属哪个流程（与设计 session 同一把 key）")
    node_id: Optional[str] = Field(default=None, description="归属哪个环节；None=流程级")
    kind: InsightKind
    channel: InsightChannel
    severity: str = Field(description="high / medium —— 确定性判定")
    source: InsightSource
    evidence: dict[str, Any] = Field(default_factory=dict, description="确定性支撑数字")
    occurrences: int = Field(default=1, description="命中次数：ops=卡住单数；analytics=样本量")
    window: str = Field(default="", description="统计窗口，如 '近30天'")
    headline: str = Field(default="", description="一句话人话（LLM 或模板）")
    suggested_fix: Optional[str] = Field(default=None, description="建议措辞（LLM，可空）")
    status: InsightStatus = InsightStatus.OPEN
    first_seen: str = ""
    last_seen: str = ""
    pertains_to_version: Optional[str] = Field(default=None, description="针对哪个发布版本观测到")

    def key(self) -> tuple[str, Optional[str], InsightKind]:
        return (self.workflow_definition_id, self.node_id, self.kind)
