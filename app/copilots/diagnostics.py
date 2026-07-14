"""构造 InstanceContext（Mock adapter）——确定性诊断，不调用 LLM。

复用两块已有的确定性能力，不重新发明：
- app.runtime.condition_eval.evaluate_condition：判每条 submit_path 是否满足、为什么不满足
- app.runtime.handler_resolver.resolve_node_assignees：解析某环节的处理人（供"选不到人"诊断）

这个模块就是"实例接口"当前唯一的实现（Mock）：给定一份 ProcessDefinition + 表单值 +
当前环节，现算出一份只读快照。未来接真实引擎/飞书时，只需另写一个同签名的 adapter。
"""

from __future__ import annotations

from typing import Any

from app.runtime.condition_eval import evaluate_condition
from app.runtime.handler_resolver import resolve_node_assignees
from data.schema import ApproverResolution, InstanceContext, InstanceHistoryEntry, PathEvaluation, ProcessDefinition


def build_instance_context(
    *,
    instance_id: str,
    process: ProcessDefinition,
    current_node_id: str,
    form_values: dict[str, Any],
    org_data: dict[str, Any],
    initiator_user_id: str,
    uploaded_materials: list[str] | None = None,
    history: list[InstanceHistoryEntry] | list[dict[str, Any]] | None = None,
) -> InstanceContext:
    node = process.get_node_by_id(current_node_id)
    if node is None:
        raise ValueError(f"流程 {process.meta.process_id} 中不存在环节 {current_node_id!r}")

    path_evaluations = [
        _evaluate_path(path.path_name, path.target_node_id, path.condition, form_values)
        for path in node.submit_paths
    ]

    approver_resolution = None
    if not node.is_draft:
        resolved = resolve_node_assignees(
            org_data, process, node, initiator_user_id=initiator_user_id, form_values=form_values
        )
        approver_resolution = ApproverResolution(
            node_id=node.node_id,
            node_name=node.node_name,
            resolved_user_ids=[u.user_id for u in resolved],
            is_empty=not resolved,
            reason=None if resolved else "该环节处理人角色解析为空（组织中无匹配岗位/成员，或该角色未挂接人员）",
        )

    history_entries = [
        h if isinstance(h, InstanceHistoryEntry) else InstanceHistoryEntry.model_validate(h)
        for h in (history or [])
    ]

    return InstanceContext(
        instance_id=instance_id,
        process=process,
        current_node_id=current_node_id,
        initiator_user_id=initiator_user_id,
        form_values=form_values,
        uploaded_materials=uploaded_materials or [],
        path_evaluations=path_evaluations,
        approver_resolution=approver_resolution,
        history=history_entries,
    )


def _evaluate_path(path_name: str, target_node_id: str, condition: str | None, form_values: dict[str, Any]) -> PathEvaluation:
    result = evaluate_condition(condition, form_values)
    return PathEvaluation(
        path_name=path_name,
        target_node_id=target_node_id,
        condition=condition,
        matched=result.matched,
        reason=result.reason,
    )


def resolve_node_approver(
    process: ProcessDefinition,
    node_id: str,
    *,
    org_data: dict[str, Any],
    initiator_user_id: str,
    form_values: dict[str, Any],
) -> ApproverResolution:
    """诊断/校验目标环节（如要跳转/改派的目的地）的处理人是否能解析到——
    跳转前必须确认目标环节不是另一个"选不到人"的坑。"""
    node = process.get_node_by_id(node_id)
    if node is None:
        raise ValueError(f"流程 {process.meta.process_id} 中不存在环节 {node_id!r}")
    if node.is_draft:
        return ApproverResolution(node_id=node.node_id, node_name=node.node_name, resolved_user_ids=[], is_empty=False)
    resolved = resolve_node_assignees(org_data, process, node, initiator_user_id=initiator_user_id, form_values=form_values)
    return ApproverResolution(
        node_id=node.node_id,
        node_name=node.node_name,
        resolved_user_ids=[u.user_id for u in resolved],
        is_empty=not resolved,
        reason=None if resolved else "该环节处理人角色解析为空（组织中无匹配岗位/成员，或该角色未挂接人员）",
    )
