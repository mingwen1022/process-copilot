"""流程实例上的结构化动作（Instance Port 的输出契约）。

与 process_edit_tools.py 同一套心法：agent（LLM）只负责把诊断结论 + 用户诉求
解析成这里定义的 typed 操作；操作合不合法、怎么应用到 InstanceContext 上，是
纯确定性代码，不再经过任何 LLM 判断。找不到引用的环节/字段就报错，不臆造。

当前是 Mock 应用（改内存快照，不接任何真实运行时）；未来接真实引擎/飞书时，
只需另写一个同签名的 apply 函数。
"""

from __future__ import annotations

from typing import Annotated, Any, Literal, Union

from pydantic import BaseModel, Field

from app.copilots.diagnostics import build_instance_context, resolve_node_approver
from app.runtime.condition_eval import evaluate_condition
from data.schema import InstanceContext

# 撤回在这套 Mock 里的简化语义：回到起草环节（复用 process_schema 里 "DRAFT" 这个
# 既有的特殊 target_node_id 约定），代表"实例被拉回、可重新编辑提交"。


class JumpToNode(BaseModel):
    op: Literal["jump_to_node"] = "jump_to_node"
    target_node_id: str = Field(description="要跳转到的环节 node_id，或 'END'")


class SkipCurrentNode(BaseModel):
    op: Literal["skip_current_node"] = "skip_current_node"


class Withdraw(BaseModel):
    op: Literal["withdraw"] = "withdraw"


class ReassignApprover(BaseModel):
    op: Literal["reassign_approver"] = "reassign_approver"
    user_id: str = Field(description="改派给的用户 id；MVP 仅支持改派当前环节")


class SubmitDecision(BaseModel):
    """当前处理人对本环节给出结论性意见（同意/不同意），走该环节真实定义的提交路径——
    不是 override：目标环节由确定性条件求值算出，不是 agent/用户指定的，跟原生 OA
    表单点"提交/退回"走的是同一套 submit_paths，只是由 agent 读实际单据内容后推荐。"""

    op: Literal["submit_decision"] = "submit_decision"
    opinion: Literal["同意", "不同意"]
    comment: str | None = Field(default=None, description="审批意见/驳回或补充说明文字，记入表单值不参与路由判断")


class UpdateFieldValue(BaseModel):
    op: Literal["update_field_value"] = "update_field_value"
    field_name: str
    value: Any


class UpdateApprovalHistoryOpinion(BaseModel):
    """订正历史意见记录（如某环节的结论性意见/说明文字录错了）——纯粹修正审批历史台账，
    不重新触发路由：该环节早就放行到下一步，改的只是"当时记的意见"这条记录本身，不影响
    实例当前所在环节或已经产生的流转事实。"""

    op: Literal["update_approval_history_opinion"] = "update_approval_history_opinion"
    node_id: str = Field(description="要订正意见的环节 node_id（必须是历史记录里真实存在的）")
    opinion: str | None = Field(default=None, description="订正后的结论性意见；不改结论只改说明文字时留空")
    comment: str | None = Field(default=None, description="订正后的说明文字；不改说明只改结论时留空")


class AnswerOnly(BaseModel):
    op: Literal["answer_only"] = "answer_only"
    text: str


InstanceAction = Annotated[
    Union[
        JumpToNode, SkipCurrentNode, Withdraw, ReassignApprover, UpdateFieldValue,
        UpdateApprovalHistoryOpinion, SubmitDecision, AnswerOnly,
    ],
    Field(discriminator="op"),
]


class InstanceActionResult(BaseModel):
    context: InstanceContext
    applied: bool
    errors: list[str] = Field(default_factory=list)


def apply_instance_action(
    context: InstanceContext, action: InstanceAction, *, org_data: dict[str, Any], allow_stage_override: bool = False
) -> InstanceActionResult:
    """allow_stage_override：#3 运维副驾的纠错是**特权操作**——运维本来就有权在非
    draft 环节修正数据（现实里运维能做普通用户在标准 UI 上做不到的事），所以对
    UpdateFieldValue 跳过 editable_stages 校验；#2 在办副驾"协助用户编辑自己的
    表单"走的是普通用户权限，应保持默认 False、遵守 editable_stages。"""
    process = context.process

    if isinstance(action, AnswerOnly):
        return InstanceActionResult(context=context, applied=True)

    if isinstance(action, JumpToNode):
        if action.target_node_id != "END" and process.get_node_by_id(action.target_node_id) is None:
            return InstanceActionResult(context=context, applied=False, errors=[f"目标环节不存在：{action.target_node_id!r}"])
        return _rebuild_at(context, action.target_node_id, org_data=org_data)

    if isinstance(action, SkipCurrentNode):
        node = process.get_node_by_id(context.current_node_id)
        forward_targets = {p.target_node_id for p in node.submit_paths if p.target_node_id != "DRAFT"}
        if not forward_targets:
            return InstanceActionResult(context=context, applied=False, errors=["当前环节没有可跳过的前进路径"])
        if len(forward_targets) > 1:
            return InstanceActionResult(
                context=context, applied=False,
                errors=["当前环节有多条不同流向的路径，无法确定跳过后去哪，请改用 jump_to_node 明确指定目标环节"],
            )
        return _rebuild_at(context, next(iter(forward_targets)), org_data=org_data)

    if isinstance(action, Withdraw):
        draft_node = next((n for n in process.flow_nodes if n.is_draft), None)
        if draft_node is None:
            return InstanceActionResult(context=context, applied=False, errors=["流程定义中没有起草环节，无法撤回"])
        return _rebuild_at(context, draft_node.node_id, org_data=org_data)

    if isinstance(action, ReassignApprover):
        if not action.user_id.strip():
            return InstanceActionResult(context=context, applied=False, errors=["改派的用户 id 不能为空"])
        from app.org_knowledge import user_exists

        if not user_exists(org_data, action.user_id):
            return InstanceActionResult(context=context, applied=False, errors=[f"改派目标 {action.user_id!r} 不是组织中真实存在的用户"])
        approver = resolve_node_approver(
            process, context.current_node_id, org_data=org_data,
            initiator_user_id=context.initiator_user_id, form_values=context.form_values,
        )
        approver = approver.model_copy(update={"resolved_user_ids": [action.user_id], "is_empty": False, "reason": "人工改派"})
        new_context = context.model_copy(update={"approver_resolution": approver})
        return InstanceActionResult(context=new_context, applied=True)

    if isinstance(action, UpdateFieldValue):
        field = next((f for f in process.form_fields if f.field_name == action.field_name), None)
        if field is None:
            return InstanceActionResult(context=context, applied=False, errors=[f"字段不存在：{action.field_name!r}"])
        # 枚举类字段（下拉单选/多选、单选按钮）改成的值必须真的在 options 里——运维副驾现在
        # 能看到 options 了，可能会直接提议改成某个选项，但那是 LLM 给的候选值，不能不校验就
        # 信；这里确定性核对，不在允许清单内就拒绝，不让编造的枚举值悄悄写进数据。
        if field.options:
            candidates = {str(v).strip() for v in action.value} if isinstance(action.value, list) else {
                part.strip() for part in str(action.value).split("、") if part.strip()
            } or {str(action.value).strip()}
            invalid = candidates - set(field.options)
            if invalid:
                return InstanceActionResult(
                    context=context, applied=False,
                    errors=[f"字段「{action.field_name}」的值 {sorted(invalid)!r} 不在允许的可选值 {field.options} 内"],
                )
        editable = allow_stage_override or "all" in field.editable_stages or context.current_node_id in field.editable_stages
        if not editable:
            return InstanceActionResult(
                context=context, applied=False,
                errors=[f"字段「{action.field_name}」在当前环节（{context.current_node_id}）不可编辑"],
            )
        new_form_values = {**context.form_values, action.field_name: action.value}
        return _rebuild_at(context, context.current_node_id, org_data=org_data, form_values=new_form_values)

    if isinstance(action, UpdateApprovalHistoryOpinion):
        entries = list(context.history)
        idx = next((i for i, h in enumerate(entries) if h.node_id == action.node_id), None)
        if idx is None:
            return InstanceActionResult(
                context=context, applied=False,
                errors=[f"未找到环节「{action.node_id}」的历史意见记录"],
            )
        # 结论性意见必须真的是该环节 opinion 配置里允许的选项——不能让编造的结论悄悄写进
        # 历史台账（跟 UpdateFieldValue 对枚举字段 options 的校验同一个原则）。
        if action.opinion is not None:
            node = process.get_node_by_id(action.node_id)
            allowed = node.opinion.conclusive_options if node and node.opinion else None
            if allowed and action.opinion not in allowed:
                return InstanceActionResult(
                    context=context, applied=False,
                    errors=[f"结论性意见 {action.opinion!r} 不在环节「{action.node_id}」允许的选项 {allowed} 内"],
                )
        if action.opinion is None and action.comment is None:
            return InstanceActionResult(context=context, applied=False, errors=["未指定要订正的结论或说明文字"])
        updates: dict[str, Any] = {}
        if action.opinion is not None:
            updates["opinion"] = action.opinion
        if action.comment is not None:
            updates["comment"] = action.comment
        entries[idx] = entries[idx].model_copy(update=updates)
        new_context = context.model_copy(update={"history": entries})
        return InstanceActionResult(context=new_context, applied=True)

    if isinstance(action, SubmitDecision):
        node = process.get_node_by_id(context.current_node_id)
        if node.is_draft:
            return InstanceActionResult(context=context, applied=False, errors=["起草环节不产生结论性意见，无需走该动作"])
        eval_values = {**context.form_values, "结论性意见": action.opinion}
        matched = [p for p in node.submit_paths if evaluate_condition(p.condition, eval_values).matched]
        if not matched:
            return InstanceActionResult(
                context=context, applied=False,
                errors=[f"按「{action.opinion}」在当前环节找不到匹配的提交路径，可能还需要更具体的信息（如金额/天数等条件）才能确定去向"],
            )
        if len(matched) > 1:
            return InstanceActionResult(
                context=context, applied=False,
                errors=["按当前意见匹配到多条可走的路径，无法唯一确定去向，需要更具体的判断依据"],
            )
        # 结论性意见只用于这次路径匹配，不带进下一环节的 form_values——那是下一个
        # 处理人自己的决定，不该被上一环节的意见污染（否则下一环节的 path_evaluations
        # 会误以为"已经有了结论"）。
        carried_values = {k: v for k, v in context.form_values.items() if k != "结论性意见"}
        if action.comment:
            carried_values = {**carried_values, f"{context.current_node_id}_审批意见": action.comment}
        target = matched[0].target_node_id
        if target == "DRAFT":  # 哨兵值：退回起草，解析成真实起草环节 node_id（同 Withdraw 的处理）
            draft_node = next((n for n in process.flow_nodes if n.is_draft), None)
            if draft_node is None:
                return InstanceActionResult(context=context, applied=False, errors=["流程定义中没有起草环节，无法退回"])
            target = draft_node.node_id
        return _rebuild_at(context, target, org_data=org_data, form_values=carried_values)

    return InstanceActionResult(context=context, applied=False, errors=[f"未知动作类型：{action!r}"])


def _rebuild_at(
    context: InstanceContext, node_id: str, *, org_data: dict[str, Any], form_values: dict[str, Any] | None = None
) -> InstanceActionResult:
    """移动到 node_id（或原地，仅表单值变了）后，重新现算 path_evaluations/approver_resolution
    ——这样调用方能立刻看到"改完之后，原本卡住的路径是不是通了"。"""
    process = context.process
    values = form_values if form_values is not None else context.form_values
    if node_id == "END":
        new_context = context.model_copy(update={
            "current_node_id": "END", "form_values": values, "path_evaluations": [], "approver_resolution": None,
        })
        return InstanceActionResult(context=new_context, applied=True)
    new_context = build_instance_context(
        instance_id=context.instance_id, process=process, current_node_id=node_id,
        form_values=values, org_data=org_data, initiator_user_id=context.initiator_user_id,
        uploaded_materials=context.uploaded_materials,
    )
    return InstanceActionResult(context=new_context, applied=True)
