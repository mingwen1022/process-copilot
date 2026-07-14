"""
分析侧事件日志 + 场景配置数据模型
版本: V1.0
说明: 100% 合成数据的契约定义。字段设计参照真实 EOA 任务级事件日志校准
（见 doc/产品功能全景.md 模块7/8 规划）：单一 enter/leave 停留区间（无法
拆分等待/处理）、outcome 由 action 承载、支持并行/抢办、允许未办结实例。
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field


# ──────────────────────────────────────────────
# 枚举类型定义
# ──────────────────────────────────────────────

class CaseStatus(str, Enum):
    """实例状态（对齐真实 EOA FLOW_STATUS_NAME 语义）"""
    COMPLETED = "正常结束"
    IN_PROGRESS = "进行中"
    TERMINATED = "终止"


class ActionCategory(str, Enum):
    """action 的语义分类；真实数据里这层语义全靠解析 PATH_NAME 文本得出，
    合成数据里因为是我们自己生成的，可以直接标注，供确定性指标层直接分组统计。"""
    ROUTE_FORWARD = "路由至下一环节"
    RETURN = "退回"
    END = "流程结束"
    TERMINATED_TIMEOUT = "系统终止"
    PREEMPTED_LOST = "抢办失败"
    MANUAL_INTERVENTION = "人工介入"
    OTHER = "其它"


class DefectType(str, Enum):
    """注入病灶类型；每种病灶都会在受影响事件/实例上写 injected_defect_labels，
    作为分析 agent 检出率评测的 ground truth。"""
    SLOW_NODE = "慢环节"
    HIGH_RETURN_RATE = "高退回率"
    SLA_BREACH = "超时违约"
    ILLEGAL_SKIP = "违规跳级"
    MANUAL_INTERVENTION = "线下人工介入"
    ZOMBIE_INSTANCE = "僵尸实例"


# ──────────────────────────────────────────────
# 事件日志（生成器输出）
# ──────────────────────────────────────────────

class EventRecord(BaseModel):
    """一条 = 一个实例经过一个环节的一次任务（真实 EOA 的 RECEIVE→SEND 一行）。"""

    event_id: str = Field(description="任务唯一标识")
    case_id: str = Field(description="所属实例 id")
    task_order: int = Field(description="任务序号，用于按顺序重建轨迹；同序号可能有多条（并行/抢办）")
    node_id: str = Field(description="环节 id，对应 ProcessDefinition.flow_nodes[].node_id")
    node_name: str = Field(description="环节名称")
    resource_user_id: Optional[str] = Field(default=None, description="经办人 id；抢办失败/系统动作可为 None")
    resource_name: Optional[str] = Field(default=None, description="经办人姓名")
    resource_dept_id: Optional[str] = Field(default=None, description="经办人部门 id")
    resource_dept_name: Optional[str] = Field(default=None, description="经办人部门名称")
    enter_time: datetime = Field(description="任务到达环节时刻")
    leave_time: Optional[datetime] = Field(
        default=None, description="任务办结/转出时刻；None 表示尚未办结（实例进行中或僵尸）"
    )
    dwell_seconds: Optional[float] = Field(
        default=None,
        description="leave_time-enter_time（秒）；真实数据只有这一个停留区间，不拆等待/处理，"
        "None 表示尚未办结无法计算",
    )
    action: str = Field(description="本次任务采取的动作/路径名，如'送部门总经理审批'、'退回起草'、'系统自动终止'")
    action_category: ActionCategory = Field(description="action 的语义分类，供确定性指标层直接分组")
    target_node_id: Optional[str] = Field(
        default=None, description="下一环节 node_id；'END'=流程结束；None 表示本条任务未产生路由（如抢办失败）"
    )
    injected_defect_labels: list[str] = Field(
        default_factory=list, description="命中的注入病灶 defect_id 列表；评测检出率的 ground truth"
    )


class CaseRecord(BaseModel):
    """一个流程实例的完整轨迹。"""

    case_id: str
    flow_code: str = Field(description="流程编号，对应 ProcessMeta.process_id")
    flow_name: str = Field(description="流程名称，对应 ProcessMeta.process_name")
    process_version: str = Field(description="流程版本，对应 ProcessMeta.version")
    case_status: CaseStatus
    initiator_user_id: str
    initiator_name: str
    initiator_dept_id: Optional[str] = None
    initiator_dept_name: Optional[str] = None
    case_attributes: dict[str, Any] = Field(
        default_factory=dict, description="案例级业务属性，如请假类型/请假天数/发起时间"
    )
    created_at: datetime = Field(description="实例发起时刻")
    closed_at: Optional[datetime] = Field(default=None, description="实例办结/终止时刻；进行中为 None")
    events: list[EventRecord] = Field(default_factory=list)
    injected_defect_ids: list[str] = Field(
        default_factory=list, description="本实例命中的注入病灶 defect_id 列表（去重后的案例级汇总）"
    )

    def to_flat_rows(self) -> list[dict[str, Any]]:
        """把案例级字段拍平进每一条事件行，得到类似真实 EOA 导出表的宽表，供指标层/导出用。"""
        case_fields = {
            "case_id": self.case_id,
            "flow_code": self.flow_code,
            "flow_name": self.flow_name,
            "process_version": self.process_version,
            "case_status": self.case_status.value,
            "initiator_user_id": self.initiator_user_id,
            "initiator_name": self.initiator_name,
            "initiator_dept_id": self.initiator_dept_id,
            "initiator_dept_name": self.initiator_dept_name,
            "created_at": self.created_at,
            "closed_at": self.closed_at,
            **{f"attr_{key}": value for key, value in self.case_attributes.items()},
        }
        return [{**case_fields, **event.model_dump()} for event in self.events]


# ──────────────────────────────────────────────
# 场景配置（生成器输入）
# ──────────────────────────────────────────────

class ArrivalConfig(BaseModel):
    """实例到达过程配置。"""

    rate_per_day: float = Field(description="泊松到达的基础日均实例数")
    peak_multiplier: dict[str, float] = Field(
        default_factory=dict,
        description="到达高峰倍数，key 为规则名（如 'month_end' 表示每月最后3天），value 为倍数",
    )


class DwellParam(BaseModel):
    """单个环节的停留时长分布（对数正态，单位：小时）。"""

    distribution: Literal["lognormal"] = "lognormal"
    mu: float = Field(description="lognormal 的 mu 参数（对数尺度均值）")
    sigma: float = Field(description="lognormal 的 sigma 参数（对数尺度标准差）")


class DefectSpec(BaseModel):
    """一条注入病灶的定义 = 一条评测 gold 标签。"""

    defect_id: str
    type: DefectType
    target_node_id: Optional[str] = Field(default=None, description="病灶作用的环节；None 表示案例级/全局")
    params: dict[str, Any] = Field(default_factory=dict, description="病灶参数，如倍数/概率/强制路由目标")
    affected_case_ratio: float = Field(
        default=1.0,
        ge=0.0,
        le=1.0,
        description="在满足触发条件（如经过 target_node_id）的样本里，实际命中该病灶的比例",
    )


class ScenarioConfig(BaseModel):
    """一次合成事件日志生成的完整配置；固定 random_seed 可复现。"""

    scenario_id: str
    process_target_path: str = Field(description="ProcessDefinition 来源，指向 case 的 standard/target.json")
    org_seed_path: str = Field(default="data/org/org_seed.json")
    case_count: int
    start_date: str = Field(description="到达时间窗起点，yyyy-MM-dd")
    end_date: str = Field(description="到达时间窗终点，yyyy-MM-dd")
    arrival: ArrivalConfig
    node_dwell_params: dict[str, DwellParam] = Field(description="各环节的停留时长分布，key 为 node_id")
    decision_probabilities: dict[str, float] = Field(
        description="各审批环节结论性意见=同意 的基础概率，key 为 node_id"
    )
    assumed_sla_days: dict[str, float] = Field(
        default_factory=dict,
        description="各环节假定的 SLA 天数（ProcessDefinition 本身未设 time_limit_days 时的分析假设），"
        "供 SLA_BREACH 病灶与后续指标层使用",
    )
    leave_type_weights: dict[str, float] = Field(
        default_factory=dict, description="请假类型抽样权重（请假场景专属，其它流程可忽略）"
    )
    leave_days_range: dict[str, tuple[int, int]] = Field(
        default_factory=dict, description="各请假类型对应的天数抽样区间（含端点）"
    )
    numeric_case_attributes: dict[str, tuple[float, float]] = Field(
        default_factory=dict,
        description="通用数值型案例属性抽样区间（如报销总额），key=字段名，value=(low, high) 均匀分布。"
        "非请假场景用这个驱动 submit_paths 条件判断，而非 leave_days_range。",
    )
    injected_defects: list[DefectSpec] = Field(default_factory=list)
    random_seed: int = 42
    max_resubmit_loops: int = Field(default=4, description="退回起草最多重提次数，防止高退回率病灶导致死循环")
