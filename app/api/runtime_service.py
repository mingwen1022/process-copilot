from __future__ import annotations

from pathlib import Path
from typing import Any

from app.api.process_catalog import import_standard_processes
from app.org_knowledge import get_user_assignments, load_org_seed
from app.runtime.engine import RuntimeEngine
from app.runtime.form_renderer import render_form_schema
from app.runtime.models import AvailablePath, ProcessInstance
from app.runtime.store import SQLiteRuntimeStore
from data.schema import FlowNode, ProcessDefinition


DEFAULT_DB_PATH = Path("data/runtime/runtime.db")


class RuntimeService:
    def __init__(self, db_path: str | Path = DEFAULT_DB_PATH):
        self.store = SQLiteRuntimeStore(db_path)
        self.org_data = load_org_seed()
        self.engine = RuntimeEngine(self.org_data)

    def ensure_standard_processes(self) -> list[dict[str, Any]]:
        return import_standard_processes(self.store)

    def list_processes(self) -> list[dict[str, Any]]:
        processes = self.store.list_process_definitions()
        if not processes:
            self.ensure_standard_processes()
            processes = self.store.list_process_definitions()
        return [self._process_row_with_counts(row) for row in processes]

    def get_process(self, process_key: str) -> dict[str, Any]:
        process = self.store.load_process_definition(process_key)
        row = next((item for item in self.list_processes() if item["process_key"] == process_key), {})
        return {
            **row,
            "definition": process.model_dump(mode="json"),
            "form_schema": render_form_schema(process, node_id="draft"),
            "node_summaries": [_node_summary(node) for node in process.flow_nodes],
        }

    def _process_row_with_counts(self, row: dict[str, Any]) -> dict[str, Any]:
        process = self.store.load_process_definition(row["process_key"])
        cleaned = _clean_process_row(row)
        cleaned.update(
            {
                "field_count": len(process.form_fields),
                "node_count": len(process.flow_nodes),
                "path_count": sum(len(node.submit_paths) for node in process.flow_nodes),
            }
        )
        return cleaned

    def list_users(self) -> list[dict[str, Any]]:
        users: list[dict[str, Any]] = []
        for item in self.org_data.get("users", []):
            assignments = get_user_assignments(self.org_data, item["user_id"])
            users.append(
                {
                    "user_id": item["user_id"],
                    "name": item["name"],
                    "employee_no": item.get("employee_no"),
                    "assignments": assignments,
                    "display_role": _display_role(assignments),
                }
            )
        return users

    def get_org(self) -> dict[str, Any]:
        return self.org_data

    def create_instance(
        self,
        *,
        process_key: str,
        initiator_user_id: str,
        form_values: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if not self.store.list_process_definitions():
            self.ensure_standard_processes()
        process = self.store.load_process_definition(process_key)
        values = default_form_values(process)
        values.update(form_values or {})
        instance = self.engine.start_instance(process, initiator_user_id=initiator_user_id, form_values=values)
        self.store.save_instance(instance)
        return self.instance_response(instance, process_key=process_key, process=process)

    def list_instances(self) -> list[dict[str, Any]]:
        return self.store.list_instances()

    def get_instance(self, instance_id: str) -> dict[str, Any]:
        instance = self.store.load_instance(instance_id)
        process_key, process = self.store.load_process_definition_by_id(instance.process_id)
        return self.instance_response(instance, process_key=process_key, process=process)

    def tasks(self, *, assignee_id: str | None = None) -> list[dict[str, Any]]:
        tasks: list[dict[str, Any]] = []
        for row in self.store.list_instances():
            instance = self.store.load_instance(row["instance_id"])
            for task in instance.open_tasks():
                if assignee_id and task.assignee_id != assignee_id:
                    continue
                tasks.append(
                    {
                        **task.to_dict(),
                        "process_id": instance.process_id,
                        "process_name": instance.process_name,
                        "initiator_id": instance.initiator_id,
                        "initiator_name": instance.initiator_name,
                    }
                )
        return tasks

    def available_paths(
        self,
        *,
        instance_id: str,
        task_id: str,
        conclusive_opinion: str | None = None,
        context_flags: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        instance = self.store.load_instance(instance_id)
        _, process = self.store.load_process_definition_by_id(instance.process_id)
        paths = self.engine.available_paths(
            instance,
            process,
            task_id,
            conclusive_opinion=conclusive_opinion,
            context_flags=context_flags or {},
        )
        return [_available_path_payload(path, process) for path in paths]

    def complete_task(
        self,
        *,
        task_id: str,
        actor_user_id: str,
        path_name: str,
        conclusive_opinion: str | None = None,
        form_updates: dict[str, Any] | None = None,
        comment: str | None = None,
        context_flags: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        instance_id = self.store.find_instance_id_by_task(task_id)
        instance = self.store.load_instance(instance_id)
        process_key, process = self.store.load_process_definition_by_id(instance.process_id)
        instance = self.engine.complete_task(
            instance,
            process,
            task_id,
            actor_user_id=actor_user_id,
            path_name=path_name,
            conclusive_opinion=conclusive_opinion,
            form_updates=form_updates or {},
            comment=comment,
            context_flags=context_flags or {},
        )
        self.store.save_instance(instance)
        return self.instance_response(instance, process_key=process_key, process=process)

    def instance_response(
        self,
        instance: ProcessInstance,
        *,
        process_key: str,
        process: ProcessDefinition,
    ) -> dict[str, Any]:
        return {
            **instance.to_dict(),
            "process_key": process_key,
            "open_tasks": [task.to_dict() for task in instance.open_tasks()],
            "form_schema": render_form_schema(process, node_id=instance.current_node_id if instance.current_node_id != "END" else "draft"),
            "node_summaries": [_node_summary(node) for node in process.flow_nodes],
        }


def default_form_values(process: ProcessDefinition) -> dict[str, Any]:
    values: dict[str, Any] = {}
    for field in process.form_fields:
        if field.default_value and not _looks_like_auto_rule(field.default_value):
            values[field.field_name] = field.default_value
        elif field.options:
            values[field.field_name] = field.options[0]
        else:
            values[field.field_name] = ""
    return values


def _clean_process_row(row: dict[str, Any]) -> dict[str, Any]:
    return {
        key: value
        for key, value in row.items()
        if key != "artifact_paths_json"
    }


def _node_summary(node: FlowNode) -> dict[str, Any]:
    return {
        "node_id": node.node_id,
        "node_name": node.node_name,
        "is_draft": node.is_draft,
        "handler_role": node.handler.role if node.handler else "起草人",
        "submit_paths": [path.model_dump(mode="json") for path in node.submit_paths],
    }


def _available_path_payload(path: AvailablePath, process: ProcessDefinition) -> dict[str, Any]:
    return {
        "path_name": path.path_name,
        "target_node_id": path.target_node_id,
        "target_node_name": process.get_node_name(path.target_node_id),
        "enabled": path.enabled,
        "reason": path.reason,
    }


def _display_role(assignments: dict[str, list[dict[str, Any]]]) -> str:
    primary = assignments.get("primary") or []
    concurrent = assignments.get("concurrent") or []
    first = primary[0] if primary else concurrent[0] if concurrent else None
    if not first:
        return "未配置岗位"
    suffix = f" + {len(concurrent)} 个兼岗" if concurrent else ""
    return f"{first.get('dept_name', first.get('dept_id'))} / {first.get('position_name', first.get('position_code'))}{suffix}"


def _looks_like_auto_rule(value: str) -> bool:
    return any(token in value for token in ("系统自动", "自动带出", "当前登录", "自动生成"))
