from __future__ import annotations

from typing import Any

from app.org_knowledge import ResolvedUser, load_org_seed, resolve_role
from app.runtime.condition_eval import evaluate_condition
from app.runtime.handler_resolver import resolve_node_assignees
from app.runtime.models import (
    AuditLog,
    AvailablePath,
    InstanceStatus,
    ProcessInstance,
    RuntimeTask,
    TaskStatus,
    now_iso,
)
from data.schema import FlowNode, ProcessDefinition, SubmitPath


class RuntimeEngine:
    def __init__(self, org_data: dict[str, Any] | None = None):
        self.org_data = org_data or load_org_seed()

    def start_instance(
        self,
        process: ProcessDefinition,
        *,
        initiator_user_id: str,
        form_values: dict[str, Any] | None = None,
    ) -> ProcessInstance:
        initiator = self._user(initiator_user_id)
        values = dict(form_values or {})
        draft = self._draft_node(process)
        instance = ProcessInstance(
            process_id=process.meta.process_id,
            process_name=process.meta.process_name,
            initiator_id=initiator.user_id,
            initiator_name=initiator.name,
            form_values=values,
            status=InstanceStatus.DRAFT,
            current_node_id=draft.node_id,
        )
        instance.audit_logs.append(
            AuditLog(
                event_type="INSTANCE_STARTED",
                actor_id=initiator.user_id,
                actor_name=initiator.name,
                node_id=draft.node_id,
                message=f"{initiator.name} 发起 {process.meta.process_name}",
            )
        )
        self._create_tasks_for_node(instance, process, draft, previous_actor_ids=set(), context={})
        return instance

    def complete_task(
        self,
        instance: ProcessInstance,
        process: ProcessDefinition,
        task_id: str,
        *,
        actor_user_id: str,
        path_name: str,
        conclusive_opinion: str | None = None,
        form_updates: dict[str, Any] | None = None,
        comment: str | None = None,
        context_flags: dict[str, Any] | None = None,
    ) -> ProcessInstance:
        task = self._open_task(instance, task_id)
        if task.assignee_id != actor_user_id:
            raise RuntimeError(f"task {task_id} is assigned to {task.assignee_id}, not {actor_user_id}")

        if form_updates:
            instance.form_values.update(form_updates)

        node = self._node(process, task.node_id)
        context = self._condition_context(
            instance,
            task,
            conclusive_opinion=conclusive_opinion,
            extra_flags=context_flags or {},
        )
        path = self._find_path(node, path_name)
        result = evaluate_condition(path.condition, context)
        if not result.matched:
            raise RuntimeError(f"path {path_name} is not available: {result.reason}")

        actor = self._user(actor_user_id)
        task.complete()
        instance.audit_logs.append(
            AuditLog(
                event_type="TASK_COMPLETED",
                actor_id=actor.user_id,
                actor_name=actor.name,
                node_id=node.node_id,
                path_name=path.path_name,
                from_node_id=node.node_id,
                to_node_id=path.target_node_id,
                message=f"{actor.name} 在 {node.node_name} 选择 {path.path_name}",
                payload={
                    "conclusive_opinion": conclusive_opinion,
                    "comment": comment,
                    "condition": path.condition,
                },
            )
        )

        if path.target_node_id == node.node_id:
            self._refresh_status(instance, node.node_id)
            return instance

        self._route_to_target(
            instance,
            process,
            path.target_node_id,
            previous_actor_ids={actor_user_id},
            context=context,
        )
        instance.updated_at = now_iso()
        return instance

    def available_paths(
        self,
        instance: ProcessInstance,
        process: ProcessDefinition,
        task_id: str,
        *,
        conclusive_opinion: str | None = None,
        context_flags: dict[str, Any] | None = None,
    ) -> list[AvailablePath]:
        task = self._open_task(instance, task_id)
        node = self._node(process, task.node_id)
        context = self._condition_context(
            instance,
            task,
            conclusive_opinion=conclusive_opinion,
            extra_flags=context_flags or {},
        )
        paths: list[AvailablePath] = []
        for path in node.submit_paths:
            result = evaluate_condition(path.condition, context)
            paths.append(
                AvailablePath(
                    path_name=path.path_name,
                    target_node_id=path.target_node_id,
                    enabled=result.matched,
                    reason=result.reason,
                )
            )
        return paths

    def tasks_for_user(self, instance: ProcessInstance, user_id: str) -> list[RuntimeTask]:
        return [task for task in instance.open_tasks() if task.assignee_id == user_id]

    def _route_to_target(
        self,
        instance: ProcessInstance,
        process: ProcessDefinition,
        target_node_id: str,
        *,
        previous_actor_ids: set[str],
        context: dict[str, Any],
    ) -> None:
        if target_node_id == "END":
            instance.status = InstanceStatus.COMPLETED
            instance.current_node_id = "END"
            instance.audit_logs.append(AuditLog(event_type="INSTANCE_COMPLETED", message="流程进入 END，实例完成", to_node_id="END"))
            return

        if target_node_id == "DRAFT":
            draft = self._draft_node(process)
            instance.status = InstanceStatus.RETURNED
            instance.current_node_id = draft.node_id
            self._create_tasks_for_node(instance, process, draft, previous_actor_ids=set(), context={**context, "upstream_return": True})
            instance.audit_logs.append(AuditLog(event_type="INSTANCE_RETURNED", node_id=draft.node_id, message="流程退回起草"))
            return

        target = self._node(process, target_node_id)
        created = self._create_tasks_for_node(instance, process, target, previous_actor_ids=previous_actor_ids, context=context)
        if created:
            return

        forward = self._first_enabled_forward_path(target, instance, context)
        if not forward:
            self._create_tasks_for_node(instance, process, target, previous_actor_ids=set(), context=context, allow_same_actor=True)
            return

        self._route_to_target(
            instance,
            process,
            forward.target_node_id,
            previous_actor_ids=previous_actor_ids,
            context=context,
        )

    def _create_tasks_for_node(
        self,
        instance: ProcessInstance,
        process: ProcessDefinition,
        node: FlowNode,
        *,
        previous_actor_ids: set[str],
        context: dict[str, Any],
        allow_same_actor: bool = False,
    ) -> list[RuntimeTask]:
        assignees = resolve_node_assignees(
            self.org_data,
            process,
            node,
            initiator_user_id=instance.initiator_id,
            form_values=instance.form_values,
        )
        if not assignees:
            raise RuntimeError(f"cannot resolve assignee for node {node.node_id}/{node.node_name}")

        same_actor = previous_actor_ids and {user.user_id for user in assignees}.issubset(previous_actor_ids)
        if same_actor and not allow_same_actor:
            for user in assignees:
                skipped = self._build_task(instance, node, user)
                skipped.skip()
                instance.tasks.append(skipped)
            instance.audit_logs.append(
                AuditLog(
                    event_type="SAME_ACTOR_SKIPPED",
                    node_id=node.node_id,
                    message=f"{node.node_name} 解析到上一处理人，需跳过或升级；V2.2 采用自动跳过",
                    payload={"assignee_ids": [user.user_id for user in assignees]},
                )
            )
            return []

        tasks = [self._build_task(instance, node, user) for user in assignees]
        instance.tasks.extend(tasks)
        if node.is_draft:
            instance.status = InstanceStatus.RETURNED if instance.status == InstanceStatus.RETURNED else InstanceStatus.DRAFT
        else:
            instance.status = InstanceStatus.ACTIVE
        instance.current_node_id = node.node_id
        instance.audit_logs.append(
            AuditLog(
                event_type="TASK_CREATED",
                node_id=node.node_id,
                message=f"{node.node_name} 生成 {len(tasks)} 个待办",
                payload={"assignee_ids": [task.assignee_id for task in tasks]},
            )
        )
        return tasks

    def _first_enabled_forward_path(self, node: FlowNode, instance: ProcessInstance, context: dict[str, Any]) -> SubmitPath | None:
        local_context = {**context, **instance.form_values, "is_last_task": True}
        for path in node.submit_paths:
            if path.target_node_id in {"DRAFT", node.node_id}:
                continue
            if evaluate_condition(path.condition, local_context).matched:
                return path
        return None

    def _build_task(self, instance: ProcessInstance, node: FlowNode, user: ResolvedUser) -> RuntimeTask:
        handler = node.handler
        return RuntimeTask(
            instance_id=instance.instance_id,
            node_id=node.node_id,
            node_name=node.node_name,
            assignee_id=user.user_id,
            assignee_name=user.name,
            handler_role=handler.role if handler else "起草人",
            handler_source=handler.source.value if handler else "不指定",
        )

    def _condition_context(
        self,
        instance: ProcessInstance,
        task: RuntimeTask,
        *,
        conclusive_opinion: str | None,
        extra_flags: dict[str, Any],
    ) -> dict[str, Any]:
        open_same_node = [
            item for item in instance.open_tasks()
            if item.node_id == task.node_id and item.status == TaskStatus.OPEN
        ]
        return {
            **instance.form_values,
            **extra_flags,
            "结论性意见": conclusive_opinion,
            "conclusive_opinion": conclusive_opinion,
            "is_last_task": len(open_same_node) <= 1,
        }

    def _refresh_status(self, instance: ProcessInstance, current_node_id: str) -> None:
        instance.current_node_id = current_node_id
        instance.status = InstanceStatus.ACTIVE if instance.open_tasks() else InstanceStatus.COMPLETED
        instance.updated_at = now_iso()

    def _find_path(self, node: FlowNode, path_name: str) -> SubmitPath:
        for path in node.submit_paths:
            if path.path_name == path_name:
                return path
        raise RuntimeError(f"path {path_name} not found in node {node.node_id}")

    def _open_task(self, instance: ProcessInstance, task_id: str) -> RuntimeTask:
        for task in instance.tasks:
            if task.task_id == task_id:
                if task.status != TaskStatus.OPEN:
                    raise RuntimeError(f"task {task_id} is not open")
                return task
        raise RuntimeError(f"task {task_id} not found")

    def _draft_node(self, process: ProcessDefinition) -> FlowNode:
        for node in process.flow_nodes:
            if node.is_draft or node.node_id == "draft":
                return node
        return process.flow_nodes[0]

    def _node(self, process: ProcessDefinition, node_id: str) -> FlowNode:
        node = process.get_node_by_id(node_id)
        if not node:
            raise RuntimeError(f"node {node_id} not found")
        return node

    def _user(self, user_id: str) -> ResolvedUser:
        users = resolve_role(self.org_data, "起草人", applicant_user_id=user_id)
        if not users:
            raise RuntimeError(f"user {user_id} not found")
        return users[0]
