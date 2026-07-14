from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any
from uuid import uuid4


class InstanceStatus(str, Enum):
    DRAFT = "DRAFT"
    ACTIVE = "ACTIVE"
    RETURNED = "RETURNED"
    COMPLETED = "COMPLETED"
    CANCELED = "CANCELED"


class TaskStatus(str, Enum):
    OPEN = "OPEN"
    COMPLETED = "COMPLETED"
    SKIPPED = "SKIPPED"
    CANCELED = "CANCELED"


@dataclass(slots=True)
class RuntimeTask:
    instance_id: str
    node_id: str
    node_name: str
    assignee_id: str
    assignee_name: str
    task_id: str = field(default_factory=lambda: f"task_{uuid4().hex[:12]}")
    status: TaskStatus = TaskStatus.OPEN
    created_at: str = field(default_factory=lambda: now_iso())
    completed_at: str | None = None
    handler_role: str | None = None
    handler_source: str | None = None

    def complete(self) -> None:
        self.status = TaskStatus.COMPLETED
        self.completed_at = now_iso()

    def skip(self) -> None:
        self.status = TaskStatus.SKIPPED
        self.completed_at = now_iso()

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["status"] = self.status.value
        return data

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "RuntimeTask":
        data = dict(payload)
        data["status"] = TaskStatus(data.get("status", TaskStatus.OPEN))
        return cls(**data)


@dataclass(slots=True)
class AuditLog:
    event_type: str
    message: str
    actor_id: str | None = None
    actor_name: str | None = None
    node_id: str | None = None
    path_name: str | None = None
    from_node_id: str | None = None
    to_node_id: str | None = None
    payload: dict[str, Any] = field(default_factory=dict)
    created_at: str = field(default_factory=lambda: now_iso())

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "AuditLog":
        return cls(**dict(payload))


@dataclass(slots=True)
class ProcessInstance:
    process_id: str
    process_name: str
    initiator_id: str
    initiator_name: str
    form_values: dict[str, Any]
    instance_id: str = field(default_factory=lambda: f"inst_{uuid4().hex[:12]}")
    status: InstanceStatus = InstanceStatus.DRAFT
    current_node_id: str = "draft"
    tasks: list[RuntimeTask] = field(default_factory=list)
    audit_logs: list[AuditLog] = field(default_factory=list)
    created_at: str = field(default_factory=lambda: now_iso())
    updated_at: str = field(default_factory=lambda: now_iso())

    def open_tasks(self) -> list[RuntimeTask]:
        return [task for task in self.tasks if task.status == TaskStatus.OPEN]

    def to_dict(self) -> dict[str, Any]:
        return {
            "instance_id": self.instance_id,
            "process_id": self.process_id,
            "process_name": self.process_name,
            "initiator_id": self.initiator_id,
            "initiator_name": self.initiator_name,
            "form_values": self.form_values,
            "status": self.status.value,
            "current_node_id": self.current_node_id,
            "tasks": [task.to_dict() for task in self.tasks],
            "audit_logs": [log.to_dict() for log in self.audit_logs],
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "ProcessInstance":
        data = dict(payload)
        data["status"] = InstanceStatus(data.get("status", InstanceStatus.DRAFT))
        data["tasks"] = [RuntimeTask.from_dict(item) for item in data.get("tasks", [])]
        data["audit_logs"] = [AuditLog.from_dict(item) for item in data.get("audit_logs", [])]
        return cls(**data)


@dataclass(frozen=True, slots=True)
class AvailablePath:
    path_name: str
    target_node_id: str
    enabled: bool
    reason: str | None = None


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()
