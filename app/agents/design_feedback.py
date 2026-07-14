"""闭环反馈编排：把分析 agent 的诊断建议，交给设计 agent 自动改成 v2 草稿（模块 8）。

这是"AI 诊断 → AI 改流程"的桥，同时复用两个 agent：分析侧 `ProcessAnalysisAgent`
产出每个堵点的自然语言修改建议（`suggested_process_edit_instruction`），本编排把
被采纳的建议逐条喂给设计侧 `DesignEditAgent`——由它（LLM）解析成 typed 编辑操作，
再确定性地 `apply_edit_operations` 累加到流程定义上，得到 v2 草稿。

分工严格：
- 翻译（自然语言建议 → 结构化编辑）是**设计 agent 的活**，不是人肉手写。
- 人只做 human-in-the-loop 的判断：**采纳哪几条建议、是否上线**（adopted_bottleneck_ids）。
- 每条建议的实际效果如实记录（成功转成几个操作、是否真改动、有没有报错、是否
  被判 out_of_scope），含糊到转不出编辑的建议照实标出来，不替 agent 圆场。
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from app.agents.design_edit_agent import DesignEditAgent
from app.reporting.process_diff import diff_process_definitions
from app.tools.process_edit_tools import apply_edit_operations
from data.schema import ProcessDefinition


class AppliedSuggestion(BaseModel):
    bottleneck_id: str
    category: str | None = None
    instruction: str
    agent_reply: str = ""
    operation_count: int = 0
    has_changes: bool = False
    out_of_scope: bool = False
    errors: list[str] = Field(default_factory=list)


class FeedbackResult(BaseModel):
    v2_process: ProcessDefinition
    applied: list[AppliedSuggestion] = Field(default_factory=list)
    total_operations: int = 0
    changed_suggestion_count: int = 0


def apply_diagnosis_feedback(
    *,
    process: ProcessDefinition,
    diagnosis_items: list[dict[str, Any]],
    adopted_bottleneck_ids: list[str] | None,
    clarifications: dict[str, str] | None = None,
    edit_agent: DesignEditAgent | None = None,
) -> FeedbackResult:
    """把被采纳的诊断建议逐条经设计 agent 转成编辑并累加，产出 v2 草稿。

    adopted_bottleneck_ids=None 表示采纳全部有可执行建议的堵点（脚本默认）；
    实际产品里这个列表来自 human-in-the-loop 的勾选。

    clarifications：human-in-the-loop 对某些堵点补充的**业务决策**（如"升级门槛设
    为14天"）。当设计 agent 对含糊建议反问时（M4 的澄清守卫），人给的是业务参数、
    不是编辑本身——参数仍由设计 agent 翻成结构化编辑。这正是"人确认/决策、agent
    翻译"的分工。
    """
    agent = edit_agent or DesignEditAgent()
    clarifications = clarifications or {}
    current = process
    applied: list[AppliedSuggestion] = []
    total_ops = 0

    for item in diagnosis_items:
        bottleneck_id = item.get("bottleneck_id", "")
        instruction = (item.get("suggested_process_edit_instruction") or "").strip()
        if not instruction:
            continue  # 该堵点没有可执行的结构化建议
        if adopted_bottleneck_ids is not None and bottleneck_id not in adopted_bottleneck_ids:
            continue
        clarification = clarifications.get(bottleneck_id)
        if clarification:
            instruction = f"{instruction}\n（业务决策补充：{clarification}）"

        proposal = agent.run(process=current, instruction=instruction)
        result = apply_edit_operations(current, proposal.operations)
        diff = diff_process_definitions(current, result.process)
        total_ops += len(proposal.operations)
        applied.append(
            AppliedSuggestion(
                bottleneck_id=bottleneck_id,
                category=item.get("category"),
                instruction=instruction,
                agent_reply=proposal.reply,
                operation_count=len(proposal.operations),
                has_changes=diff["has_changes"],
                out_of_scope=proposal.out_of_scope,
                errors=result.errors,
            )
        )
        # 只在真的改动了才推进 current，避免把无效/空操作也算进版本演进
        if diff["has_changes"]:
            current = result.process

    return FeedbackResult(
        v2_process=current,
        applied=applied,
        total_operations=total_ops,
        changed_suggestion_count=sum(1 for a in applied if a.has_changes),
    )
