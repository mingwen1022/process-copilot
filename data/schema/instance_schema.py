"""流程实例快照数据模型（模块4 · 流程参与者副驾 · 实例接口/Instance Port）。

端口-适配器：三个副驾（运维/发起问答/在办任务）只依赖这份抽象快照，不直接依赖任何
具体运行时（冻结的 OA 引擎 / 未来的飞书）。当前只有 Mock 实现（app/copilots/diagnostics.py
里的 build_instance_context，用 condition_eval + resolve_node_assignees 现算）。

见 doc/流程参与者副驾-设计.md 第 2 节。
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

from .process_schema import ProcessDefinition


class PathEvaluation(BaseModel):
    """当前环节某条提交路径的求值结果——供诊断"为什么卡在这"用。"""

    path_name: str
    target_node_id: str
    condition: str | None = Field(default=None, description="该路径的触发条件；None 表示无条件")
    matched: bool = Field(description="该条件在当前表单值下是否成立")
    reason: str | None = Field(default=None, description="不成立时的原因说明（来自 condition_eval）")


class ApproverResolution(BaseModel):
    """某环节的处理人解析结果——供诊断"选不到人"用。"""

    node_id: str
    node_name: str
    resolved_user_ids: list[str] = Field(default_factory=list)
    is_empty: bool = Field(description="解析结果是否为空（组织中无匹配岗位/成员）")
    reason: str | None = Field(default=None, description="为空时的说明")


class InstanceHistoryEntry(BaseModel):
    """实例流转历史的一条记录（只读，供上下文展示，不参与诊断计算）。

    opinion/comment 只在这条记录是"某环节给出的结论性意见"时才有值（如审批通过/驳回）；
    其它类型的历史动作（如撤回、改派）留空。"""

    node_id: str
    node_name: str
    actor_user_id: str | None = None
    action: str
    occurred_at: str | None = None
    opinion: str | None = Field(default=None, description="该环节给出的结论性意见（如'同意'/'不同意'），非审批类动作留空")
    comment: str | None = Field(default=None, description="意见内容/说明文字，可选填——尤其给出'不同意'一类结论时应说明理由")


class InstanceContext(BaseModel):
    """流程实例的只读快照——三个副驾的统一输入契约。"""

    instance_id: str
    process: ProcessDefinition
    current_node_id: str
    initiator_user_id: str
    form_values: dict[str, Any] = Field(default_factory=dict)
    uploaded_materials: list[str] = Field(default_factory=list, description="已上传的附件类型名（供在办副驾查材料齐不齐）")
    history: list[InstanceHistoryEntry] = Field(default_factory=list)
    path_evaluations: list[PathEvaluation] = Field(default_factory=list)
    approver_resolution: ApproverResolution | None = Field(
        default=None, description="当前环节的处理人解析结果；起草环节等无审批人的环节为 None"
    )

    def blocked_paths(self) -> list[PathEvaluation]:
        """有条件但当前不满足的路径——"为什么走不了"的候选原因。"""
        return [p for p in self.path_evaluations if p.condition and not p.matched]

    def has_any_matched_path(self) -> bool:
        return any(p.matched for p in self.path_evaluations)


class AuthorizationRequest(BaseModel):
    """运维授权工单：agent 给出了具体的 override 方案，但破坏流程走向属高风险，
    须由流程负责人/管理员人工签字。这是"升级人工"的结构化落地——带具体动作 + 依据 +
    审批闭环，而不是一句"已升级"。动作本体（InstanceAction）由服务端按 request_id 另存。"""

    request_id: str
    instance_id: str
    instance_title: str
    action_summary: str = Field(description="拟执行的 override 摘要，供授权人一眼看懂")
    reason: str = Field(description="agent 的依据（引用了哪条运维规则、为什么这么处理）")
    requested_by: str = Field(description="发起人（被卡住的用户）")
    assigned_to: str = Field(default="流程负责人", description="指派给谁授权（owner/管理员）")
    status: Literal["pending", "approved", "rejected"] = "pending"
    decision_note: str | None = None
