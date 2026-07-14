from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from app.runtime.models import ProcessInstance, now_iso
from data.schema import ProcessDefinition


class SQLiteRuntimeStore:
    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.init_schema()

    def init_schema(self) -> None:
        with self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS runtime_instances (
                    instance_id TEXT PRIMARY KEY,
                    process_id TEXT NOT NULL,
                    process_name TEXT NOT NULL,
                    status TEXT NOT NULL,
                    initiator_id TEXT NOT NULL,
                    current_node_id TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS runtime_tasks (
                    task_id TEXT PRIMARY KEY,
                    instance_id TEXT NOT NULL,
                    node_id TEXT NOT NULL,
                    assignee_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    FOREIGN KEY(instance_id) REFERENCES runtime_instances(instance_id)
                );

                CREATE TABLE IF NOT EXISTS audit_logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    instance_id TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    actor_id TEXT,
                    node_id TEXT,
                    created_at TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    FOREIGN KEY(instance_id) REFERENCES runtime_instances(instance_id)
                );

                CREATE TABLE IF NOT EXISTS process_definitions (
                    process_key TEXT PRIMARY KEY,
                    process_id TEXT NOT NULL,
                    process_name TEXT NOT NULL,
                    version TEXT NOT NULL,
                    responsible_dept TEXT NOT NULL,
                    source_type TEXT NOT NULL,
                    source_case_dir TEXT,
                    process_json TEXT NOT NULL,
                    artifact_paths_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS design_runs (
                    run_id TEXT PRIMARY KEY,
                    case_dir TEXT NOT NULL,
                    status TEXT NOT NULL,
                    out_dir TEXT,
                    candidate_process_json TEXT,
                    report_json TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS uploaded_sources (
                    source_id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL,
                    filename TEXT NOT NULL,
                    text TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(run_id) REFERENCES design_runs(run_id)
                );
                """
            )

    def save_process_definition(
        self,
        process_key: str,
        process: ProcessDefinition,
        *,
        source_type: str,
        source_case_dir: str | None = None,
        artifact_paths: dict[str, str] | None = None,
    ) -> None:
        now = now_iso()
        payload = process.model_dump(mode="json")
        with self._connect() as conn:
            existing = conn.execute(
                "SELECT created_at FROM process_definitions WHERE process_key = ?",
                (process_key,),
            ).fetchone()
            conn.execute(
                """
                INSERT INTO process_definitions (
                    process_key, process_id, process_name, version, responsible_dept,
                    source_type, source_case_dir, process_json, artifact_paths_json,
                    created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(process_key) DO UPDATE SET
                    process_id=excluded.process_id,
                    process_name=excluded.process_name,
                    version=excluded.version,
                    responsible_dept=excluded.responsible_dept,
                    source_type=excluded.source_type,
                    source_case_dir=excluded.source_case_dir,
                    process_json=excluded.process_json,
                    artifact_paths_json=excluded.artifact_paths_json,
                    updated_at=excluded.updated_at
                """,
                (
                    process_key,
                    process.meta.process_id,
                    process.meta.process_name,
                    process.meta.version,
                    process.meta.responsible_dept,
                    source_type,
                    source_case_dir,
                    json.dumps(payload, ensure_ascii=False),
                    json.dumps(artifact_paths or {}, ensure_ascii=False),
                    existing["created_at"] if existing else now,
                    now,
                ),
            )

    def list_process_definitions(self) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT process_key, process_id, process_name, version, responsible_dept,
                       source_type, source_case_dir, artifact_paths_json, created_at, updated_at
                FROM process_definitions
                ORDER BY process_key
                """
            ).fetchall()
        return [
            {
                **dict(row),
                "artifact_paths": json.loads(row["artifact_paths_json"] or "{}"),
            }
            for row in rows
        ]

    def load_process_definition(self, process_key: str) -> ProcessDefinition:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT process_json FROM process_definitions WHERE process_key = ?",
                (process_key,),
            ).fetchone()
        if row is None:
            raise KeyError(f"process {process_key} not found")
        return ProcessDefinition.model_validate(json.loads(row["process_json"]))

    def load_process_definition_by_id(self, process_id: str) -> tuple[str, ProcessDefinition]:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT process_key, process_json
                FROM process_definitions
                WHERE process_id = ?
                ORDER BY updated_at DESC
                LIMIT 1
                """,
                (process_id,),
            ).fetchone()
        if row is None:
            raise KeyError(f"process_id {process_id} not found")
        return row["process_key"], ProcessDefinition.model_validate(json.loads(row["process_json"]))

    def save_instance(self, instance: ProcessInstance) -> None:
        payload = instance.to_dict()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO runtime_instances (
                    instance_id, process_id, process_name, status, initiator_id,
                    current_node_id, payload_json, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(instance_id) DO UPDATE SET
                    process_id=excluded.process_id,
                    process_name=excluded.process_name,
                    status=excluded.status,
                    initiator_id=excluded.initiator_id,
                    current_node_id=excluded.current_node_id,
                    payload_json=excluded.payload_json,
                    updated_at=excluded.updated_at
                """,
                (
                    instance.instance_id,
                    instance.process_id,
                    instance.process_name,
                    instance.status.value,
                    instance.initiator_id,
                    instance.current_node_id,
                    json.dumps(payload, ensure_ascii=False),
                    instance.updated_at,
                ),
            )
            conn.execute("DELETE FROM runtime_tasks WHERE instance_id = ?", (instance.instance_id,))
            conn.execute("DELETE FROM audit_logs WHERE instance_id = ?", (instance.instance_id,))
            conn.executemany(
                """
                INSERT INTO runtime_tasks (task_id, instance_id, node_id, assignee_id, status, payload_json)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        task.task_id,
                        instance.instance_id,
                        task.node_id,
                        task.assignee_id,
                        task.status.value,
                        json.dumps(task.to_dict(), ensure_ascii=False),
                    )
                    for task in instance.tasks
                ],
            )
            conn.executemany(
                """
                INSERT INTO audit_logs (instance_id, event_type, actor_id, node_id, created_at, payload_json)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        instance.instance_id,
                        log.event_type,
                        log.actor_id,
                        log.node_id,
                        log.created_at,
                        json.dumps(log.to_dict(), ensure_ascii=False),
                    )
                    for log in instance.audit_logs
                ],
            )

    def load_instance_snapshot(self, instance_id: str) -> dict[str, Any]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT payload_json FROM runtime_instances WHERE instance_id = ?",
                (instance_id,),
            ).fetchone()
        if row is None:
            raise KeyError(f"instance {instance_id} not found")
        return json.loads(row["payload_json"])

    def load_instance(self, instance_id: str) -> ProcessInstance:
        return ProcessInstance.from_dict(self.load_instance_snapshot(instance_id))

    def find_instance_id_by_task(self, task_id: str) -> str:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT instance_id FROM runtime_tasks WHERE task_id = ?",
                (task_id,),
            ).fetchone()
        if row is None:
            raise KeyError(f"task {task_id} not found")
        return row["instance_id"]

    def list_instances(self) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT instance_id, process_id, process_name, status, initiator_id, current_node_id, updated_at
                FROM runtime_instances
                ORDER BY updated_at DESC
                """
            ).fetchall()
        return [dict(row) for row in rows]

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn
