from __future__ import annotations

import json
import re
import sqlite3
import threading
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any
from uuid import uuid4

from app.api.slice1_service import LEAVE_CASE_DIR, LEAVE_WORKFLOW_DEFINITION_ID
from app.io_utils import find_standard_json, load_process_definition
from app.reporting.process_diff import diff_process_definitions
from app.runtime.models import now_iso
from app.tools.process_edit_tools import apply_edit_operations
from data.schema import ProcessDefinition


PROJECT_ROOT = Path(__file__).resolve().parents[2]
RAW_SOURCE_DIR = PROJECT_ROOT / "data/cases/EOA140_subsidiary_major_matter/raw_sources"
DESIGN_INITIALIZATION_RUN_DIR = PROJECT_ROOT / "data/runtime/design_initializations"
GraphInitializer = Callable[..., dict[str, Any]]
UploadedSourceFile = dict[str, Any]


class WorkflowDesignService:
    """Persistent design sessions for workflow draft generation."""

    def __init__(
        self,
        db_path: str | Path,
        graph_initializer: GraphInitializer | None = None,
        edit_agent: Any | None = None,
        insight_store: Any | None = None,
        compliance_judge: Any | None = None,
    ):
        self.db_path = Path(db_path)
        self._uses_default_graph_initializer = graph_initializer is None
        self.graph_initializer = graph_initializer or self._run_langgraph_initialization
        self._edit_agent = edit_agent  # 惰性构造，避免无编辑需求时也创建真实 Bedrock client
        self._insight_store = insight_store  # 反哺闭环：设计侧读取运行洞察（可空）
        self._compliance_judge = compliance_judge  # 定性规则 LLM-judge，惰性构造（可注入供测试）
        self._initialization_jobs: dict[str, dict[str, Any]] = {}
        self._initialization_job_events: dict[str, list[dict[str, Any]]] = {}
        self._initialization_job_condition = threading.Condition()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.init_schema()

    def init_schema(self) -> None:
        with self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS workflow_design_sessions (
                    id TEXT PRIMARY KEY,
                    workflow_definition_id TEXT NOT NULL,
                    mode TEXT NOT NULL,
                    status TEXT NOT NULL,
                    title TEXT NOT NULL,
                    base_version TEXT NOT NULL,
                    base_definition_json TEXT NOT NULL,
                    draft_definition_json TEXT NOT NULL,
                    validation_report_json TEXT NOT NULL,
                    generation_report_json TEXT NOT NULL,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY(workflow_definition_id) REFERENCES workflow_definitions(id)
                );

                CREATE TABLE IF NOT EXISTS workflow_design_sources (
                    id TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL,
                    title TEXT NOT NULL,
                    source_type TEXT NOT NULL,
                    content TEXT NOT NULL,
                    status TEXT NOT NULL,
                    summary TEXT NOT NULL,
                    original_filename TEXT,
                    mime_type TEXT,
                    size_bytes INTEGER,
                    storage_path TEXT,
                    parser_warnings_json TEXT NOT NULL DEFAULT '[]',
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(session_id) REFERENCES workflow_design_sessions(id)
                );

                CREATE TABLE IF NOT EXISTS workflow_design_messages (
                    id TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL,
                    role TEXT NOT NULL,
                    content TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(session_id) REFERENCES workflow_design_sessions(id)
                );
                """
            )
            self._ensure_source_metadata_columns(conn)
            self._ensure_session_edit_columns(conn)

    def _ensure_source_metadata_columns(self, conn: sqlite3.Connection) -> None:
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(workflow_design_sources)").fetchall()}
        additions = {
            "original_filename": "TEXT",
            "mime_type": "TEXT",
            "size_bytes": "INTEGER",
            "storage_path": "TEXT",
            "parser_warnings_json": "TEXT NOT NULL DEFAULT '[]'",
        }
        for column, ddl in additions.items():
            if column not in columns:
                conn.execute(f"ALTER TABLE workflow_design_sources ADD COLUMN {column} {ddl}")

    def _ensure_session_edit_columns(self, conn: sqlite3.Connection) -> None:
        """对话式修改所需的会话列：上一版草稿（供单步 undo）+ 已处理到的最新用户消息 id（避免重复应用同一条指令）+
        待确认的合规拦截草稿（新引入违规时先落在这里，不进 draft_definition_json，等用户确认/放弃）。"""
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(workflow_design_sessions)").fetchall()}
        additions = {
            "previous_draft_definition_json": "TEXT",
            "last_processed_message_id": "TEXT",
            "pending_definition_json": "TEXT",
            "pending_message_id": "TEXT",
        }
        for column, ddl in additions.items():
            if column not in columns:
                conn.execute(f"ALTER TABLE workflow_design_sessions ADD COLUMN {column} {ddl}")

    def create_or_open_session(
        self,
        *,
        workflow_definition_id: str,
        created_by: str,
        mode: str = "revise",
    ) -> dict[str, Any]:
        workflow = self._workflow_row(workflow_definition_id)
        self._require_user(created_by)
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT * FROM workflow_design_sessions
                WHERE workflow_definition_id = ? AND created_by = ?
                ORDER BY updated_at DESC
                LIMIT 1
                """,
                (workflow_definition_id, created_by),
            ).fetchone()
            if row is None:
                # 首次进入某流程的设计工作台：基于其当前已发布/草稿版本确定性创建一份 V1.1 设计草稿会话。
                # （原先只允许请假流程；放开后 EOA140/报销/采购/用印 等所有流程都可"继续设计"。）
                now = now_iso()
                base_definition = _json_loads(workflow["definition_json"])
                draft_definition = self._initial_draft(base_definition)
                session_id = f"wds_{uuid4().hex[:12]}"
                conn.execute(
                    """
                    INSERT INTO workflow_design_sessions (
                        id, workflow_definition_id, mode, status, title, base_version,
                        base_definition_json, draft_definition_json, validation_report_json,
                        generation_report_json, created_by, created_at, updated_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        session_id,
                        workflow_definition_id,
                        mode,
                        "DRAFT",
                        f"{workflow['name']} / V1.1 草稿",
                        workflow["version"],
                        _json_dumps(base_definition),
                        _json_dumps(draft_definition),
                        _json_dumps(self._validate_definition(draft_definition)),
                        _json_dumps({"summary": "基于当前发布版本创建设计草稿", "clarifications": []}),
                        created_by,
                        now,
                        now,
                    ),
                )
                # 仅请假流程附带演示 source（EOA140 样例源）；其余流程不塞无关 source
                if workflow["id"] == LEAVE_WORKFLOW_DEFINITION_ID:
                    self._seed_raw_sources(conn, session_id=session_id, created_by=created_by, created_at=now)
                self._append_message(
                    conn,
                    session_id=session_id,
                    role="assistant",
                    content=f"已基于「{workflow['name']}」当前版本创建 V1.1 设计草稿。描述你想改什么，我来结构化修改并生成新版本。",
                    payload={"event": "session_created"},
                    created_at=now,
                )
            else:
                session_id = row["id"]
        return self.session_payload(session_id)

    def initialize_new_session(
        self,
        *,
        created_by: str,
        workflow_type: str,
        workflow_name: str,
        category: str,
        instruction: str,
        sources: list[dict[str, Any]],
        uploaded_files: list[UploadedSourceFile] | None = None,
        progress_callback: Callable[[dict[str, Any]], None] | None = None,
        is_eval_run: bool = False,
    ) -> dict[str, Any]:
        self._require_user(created_by)
        if workflow_type != "approval":
            raise RuntimeError("当前版本只支持审批流程初始化")

        clean_instruction = instruction.strip()
        clean_sources = [
            {
                "title": (source.get("title") or "未命名 source").strip(),
                "content": (source.get("content") or "").strip(),
                "source_type": (source.get("source_type") or "text").strip() or "text",
                "kind": "text",
            }
            for source in sources
            if (source.get("content") or "").strip()
        ]
        if clean_instruction:
            clean_sources.insert(
                0,
                {
                    "title": "00_用户初始化说明.txt",
                    "content": clean_instruction,
                    "source_type": "text",
                    "kind": "text",
                },
            )
        clean_uploaded_files = [
            self._normalize_uploaded_file(file, index=index)
            for index, file in enumerate(uploaded_files or [], start=1)
            if file.get("content")
        ]
        source_assets = [*clean_sources, *clean_uploaded_files]
        if not source_assets:
            raise RuntimeError("请至少上传一个 source，或填写一段流程说明")

        now = now_iso()
        session_id = f"wds_{uuid4().hex[:12]}"
        workflow_definition_id = f"wfd_ai_{uuid4().hex[:12]}"
        workflow_id = f"AI-{uuid4().hex[:8].upper()}"
        process_name = workflow_name.strip() or "未命名审批流程"
        draft_definition, graph_report = self._initialize_definition_with_langgraph(
            session_id=session_id,
            process_id=workflow_id,
            process_name=process_name,
            category=category.strip() or "审批流程",
            sources=source_assets,
            progress_callback=progress_callback,
        )
        validation_report = self._validate_definition(draft_definition)
        generation_report = self._generation_report(draft_definition, validation_report)
        generation_report.update(
            {
                "provider": "langgraph",
                "langgraph": graph_report,
            }
        )
        assistant_message = graph_report.get("designer_assistant_message") or {
            "role": "assistant",
            "message_type": "initialization_result",
            "content": self._initialization_message(draft_definition, validation_report, len(clean_sources)),
            "clarification_cards": [],
        }
        uploaded_manifest_by_title = {
            item.get("title") or item.get("original_filename"): item
            for item in graph_report.get("uploaded_source_manifest", [])
        }
        if assistant_message.get("content"):
            generation_report["summary"] = assistant_message["content"]

        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO workflow_definitions (
                    id, workflow_id, code, name, category, owner_dept_id, owner_dept_name,
                    version, status, description, launch_scope_json, definition_json,
                    created_by, created_at, updated_at, published_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    workflow_definition_id,
                    workflow_id,
                    workflow_definition_id,
                    draft_definition["meta"]["process_name"],
                    "AI 新建",
                    None,
                    draft_definition["meta"]["responsible_dept"],
                    draft_definition["meta"]["version"],
                    "DRAFT",
                    draft_definition["meta"]["description"],
                    _json_dumps(
                        {"type": "draft_only", "source": "eval_run" if is_eval_run else "workflow_design_initialize"}
                    ),
                    _json_dumps(draft_definition),
                    created_by,
                    now,
                    now,
                    None,
                ),
            )
            conn.execute(
                """
                INSERT INTO workflow_design_sessions (
                    id, workflow_definition_id, mode, status, title, base_version,
                    base_definition_json, draft_definition_json, validation_report_json,
                    generation_report_json, created_by, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    session_id,
                    workflow_definition_id,
                    "initialize",
                    "NEEDS_REVIEW" if validation_report["issue_count"] else "GENERATED",
                    f"{draft_definition['meta']['process_name']} / V0.1 草稿",
                    "NEW",
                    _json_dumps({}),
                    _json_dumps(draft_definition),
                    _json_dumps(validation_report),
                    _json_dumps(generation_report),
                    created_by,
                    now,
                    now,
                ),
            )
            for source in source_assets:
                persisted = uploaded_manifest_by_title.get(source.get("title")) or {}
                conn.execute(
                    """
                    INSERT INTO workflow_design_sources (
                        id, session_id, title, source_type, content, status, summary,
                        original_filename, mime_type, size_bytes, storage_path, parser_warnings_json,
                        created_by, created_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        f"wsrc_{uuid4().hex[:12]}",
                        session_id,
                        source["title"],
                        source["source_type"],
                        self._persisted_source_content(source),
                        "已解析",
                        self._source_asset_summary(source),
                        persisted.get("original_filename") or source.get("original_filename") or source["title"],
                        persisted.get("mime_type") or source.get("mime_type"),
                        persisted.get("size_bytes") or source.get("size_bytes"),
                        persisted.get("storage_path") or source.get("storage_path"),
                        _json_dumps(persisted.get("parser_warnings") or []),
                        created_by,
                        now,
                    ),
                )
            self._append_message(
                conn,
                session_id=session_id,
                role="assistant",
                content=assistant_message.get(
                    "content",
                    self._initialization_message(draft_definition, validation_report, len(clean_sources)),
                ),
                payload={
                    "event": "draft_initialized",
                    "report": generation_report,
                    "assistant_message": assistant_message,
                    "clarification_cards": assistant_message.get("clarification_cards", []),
                },
                created_at=now,
            )

        return self.session_payload(session_id)

    def create_initialization_job(
        self,
        *,
        created_by: str,
        workflow_type: str,
        workflow_name: str,
        category: str,
        instruction: str,
        sources: list[dict[str, Any]],
        uploaded_files: list[UploadedSourceFile] | None = None,
        eval_case_id: str | None = None,
        excluded_case_sources: list[str] | None = None,
    ) -> dict[str, Any]:
        self._require_user(created_by)
        job_id = f"wdj_{uuid4().hex[:12]}"
        now = now_iso()
        merged_uploaded = list(uploaded_files or [])
        eval_context: dict[str, Any] | None = None
        # 评测模式：额外把选中 gold 案例的 raw_source 载入为初始化材料（消融=去掉部分），
        # 初始化照常建卡片/进工作台，完成后对 gold 打分入历史。评测运行不改设计侧行为。
        if eval_case_id:
            from app.eval import eval_run_service as _eval

            excluded_set = set(excluded_case_sources or [])
            case_files = _eval.load_case_source_files(eval_case_id, excluded_set)
            case_meta = _eval.case_meta(eval_case_id) or {}
            included_names = [f["filename"] for f in case_files]
            extra_names = [f.get("filename") or f.get("title") for f in (uploaded_files or [])]
            merged_uploaded = [*case_files, *merged_uploaded]
            eval_context = {
                "case_id": eval_case_id,
                "included": included_names + [n for n in extra_names if n],
                "excluded": sorted(excluded_set),
                "extra_count": len(uploaded_files or []),
                "case_name": case_meta.get("name") or eval_case_id,
            }
        job = {
            "job_id": job_id,
            "status": "queued",
            "created_by": created_by,
            "created_at": now,
            "updated_at": now,
            "session_id": None,
            "eval_case_id": eval_case_id,
            "eval_run_id": None,
            "error": None,
        }
        with self._initialization_job_condition:
            self._initialization_jobs[job_id] = job
            self._initialization_job_events[job_id] = [
                {
                    "sequence": 1,
                    "created_at": now,
                    "event_type": "job_created",
                    "kind": "job_status",
                    "status": "queued",
                    "message": "评测初始化任务已创建" if eval_case_id else "初始化任务已创建",
                }
            ]
            self._initialization_job_condition.notify_all()

        thread = threading.Thread(
            target=self._run_initialization_job,
            kwargs={
                "job_id": job_id,
                "created_by": created_by,
                "workflow_type": workflow_type,
                "workflow_name": workflow_name,
                "category": category,
                "instruction": instruction,
                "sources": sources,
                "uploaded_files": merged_uploaded,
                "eval_context": eval_context,
            },
            daemon=True,
        )
        thread.start()
        return self.initialization_job_payload(job_id)

    def initialization_job_payload(self, job_id: str) -> dict[str, Any]:
        with self._initialization_job_condition:
            job = self._initialization_jobs.get(job_id)
            if job is None:
                raise KeyError(f"workflow design initialization job {job_id} not found")
            return {
                **job,
                "events": list(self._initialization_job_events.get(job_id, [])),
            }

    def stream_initialization_job_events(self, job_id: str) -> Iterator[str]:
        self.initialization_job_payload(job_id)

        def generate() -> Iterator[str]:
            next_index = 0
            while True:
                heartbeat = False
                with self._initialization_job_condition:
                    while (
                        next_index >= len(self._initialization_job_events.get(job_id, []))
                        and self._initialization_jobs.get(job_id, {}).get("status")
                        not in {"completed", "failed"}
                    ):
                        self._initialization_job_condition.wait(timeout=0.5)
                        if (
                            next_index >= len(self._initialization_job_events.get(job_id, []))
                            and self._initialization_jobs.get(job_id, {}).get("status")
                            not in {"completed", "failed"}
                        ):
                            heartbeat = True
                            break
                    events = self._initialization_job_events.get(job_id, [])
                    pending = events[next_index:]
                    next_index = len(events)
                    status = self._initialization_jobs.get(job_id, {}).get("status")

                if heartbeat and not pending:
                    yield ": keep-alive\n\n"
                    continue
                for event in pending:
                    yield f"event: progress\ndata: {_json_dumps(event)}\n\n"
                if status in {"completed", "failed"} and not pending:
                    return

        return generate()

    def session_payload(self, session_id: str) -> dict[str, Any]:
        session = self._session_row(session_id)
        workflow = self._workflow_row(session["workflow_definition_id"])
        with self._connect() as conn:
            sources = [
                self._source_payload(dict(row))
                for row in conn.execute(
                    "SELECT * FROM workflow_design_sources WHERE session_id = ? ORDER BY created_at ASC, title ASC",
                    (session_id,),
                ).fetchall()
            ]
            messages = [
                self._message_payload(dict(row))
                for row in conn.execute(
                    "SELECT * FROM workflow_design_messages WHERE session_id = ? ORDER BY created_at ASC, id ASC",
                    (session_id,),
                ).fetchall()
            ]
        draft_definition = _json_loads(session["draft_definition_json"])
        validation_report = _json_loads(session["validation_report_json"], {})
        generation_report = _json_loads(session["generation_report_json"], {})
        # 反哺闭环：设计侧回流——按草稿 process_id 读该流程的开放洞察（运行侧回传 banner + 环节证据角标）
        _process_id = (draft_definition.get("meta") or {}).get("process_id") or session["workflow_definition_id"]
        insights = (
            [i.model_dump() for i in self._insight_store.open_for(_process_id)]
            if self._insight_store is not None else []
        )
        return {
            "session": {
                "session_id": session["id"],
                "workflow_definition_id": session["workflow_definition_id"],
                "mode": session["mode"],
                "status": session["status"],
                "title": session["title"],
                "base_version": session["base_version"],
                "draft_version": draft_definition.get("meta", {}).get("version", "V1.1.0-draft"),
                "created_by": session["created_by"],
                "created_at": session["created_at"],
                "updated_at": session["updated_at"],
                "can_undo": bool(session.get("previous_draft_definition_json")),
                "has_pending_edit": bool(session.get("pending_definition_json")),
            },
            "workflow": {
                "workflow_definition_id": workflow["id"],
                "workflow_id": workflow["workflow_id"],
                "code": workflow["code"],
                "name": workflow["name"],
                "category": workflow["category"],
                "owner_dept_name": workflow["owner_dept_name"],
                "version": workflow["version"],
                "status": workflow["status"],
                "description": workflow["description"],
            },
            "base_definition": _json_loads(session["base_definition_json"]),
            "draft_definition": draft_definition,
            "sources": sources,
            "messages": messages,
            "validation_report": validation_report,
            "generation_report": generation_report,
            "insights": insights,  # 反哺：运行侧回传的洞察（供 banner + 环节证据角标）
        }

    def add_source(
        self,
        *,
        session_id: str,
        title: str,
        content: str,
        created_by: str,
        source_type: str = "text",
    ) -> dict[str, Any]:
        self._session_row(session_id)
        self._require_user(created_by)
        clean_title = title.strip() or "补充 source"
        clean_content = content.strip()
        if not clean_content:
            raise RuntimeError("source 内容不能为空")
        now = now_iso()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO workflow_design_sources (
                    id, session_id, title, source_type, content, status, summary,
                    created_by, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    f"wsrc_{uuid4().hex[:12]}",
                    session_id,
                    clean_title,
                    source_type,
                    clean_content,
                    "待解析",
                    self._summarize_source(clean_content),
                    created_by,
                    now,
                ),
            )
            conn.execute("UPDATE workflow_design_sessions SET updated_at = ? WHERE id = ?", (now, session_id))
            self._append_message(
                conn,
                session_id=session_id,
                role="assistant",
                content=f"已接入 source：{clean_title}。生成草稿时会把它作为补充依据。",
                payload={"event": "source_added", "title": clean_title},
                created_at=now,
            )
        return self.session_payload(session_id)

    def add_source_files(
        self,
        *,
        session_id: str,
        files: list[UploadedSourceFile],
        created_by: str,
    ) -> dict[str, Any]:
        self._session_row(session_id)
        self._require_user(created_by)
        if not files:
            raise RuntimeError("请选择要上传的 source 文件")
        now = now_iso()
        upload_dir = DESIGN_INITIALIZATION_RUN_DIR / session_id / "session_uploaded_sources"
        upload_dir.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            for index, file in enumerate(files, start=1):
                normalized = self._normalize_uploaded_file(file, index=index)
                filename = _safe_source_filename(normalized["title"], fallback=f"source_{index}.{normalized['source_type']}")
                path = upload_dir / f"{uuid4().hex[:8]}_{filename}"
                path.write_bytes(normalized["content"])
                conn.execute(
                    """
                    INSERT INTO workflow_design_sources (
                        id, session_id, title, source_type, content, status, summary,
                        original_filename, mime_type, size_bytes, storage_path, parser_warnings_json,
                        created_by, created_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        f"wsrc_{uuid4().hex[:12]}",
                        session_id,
                        normalized["title"],
                        normalized["source_type"],
                        "",
                        "待解析",
                        self._source_asset_summary(normalized),
                        normalized["original_filename"],
                        normalized["mime_type"],
                        normalized["size_bytes"],
                        str(path),
                        "[]",
                        created_by,
                        now,
                    ),
                )
            conn.execute("UPDATE workflow_design_sessions SET updated_at = ? WHERE id = ?", (now, session_id))
            self._append_message(
                conn,
                session_id=session_id,
                role="assistant",
                content=f"已接入 {len(files)} 个 source 文件。下次生成草稿时会进入解析流程。",
                payload={"event": "source_files_added", "count": len(files)},
                created_at=now,
            )
        return self.session_payload(session_id)

    def delete_source(self, *, session_id: str, source_id: str) -> dict[str, Any]:
        self._session_row(session_id)
        now = now_iso()
        with self._connect() as conn:
            row = conn.execute(
                "SELECT title FROM workflow_design_sources WHERE id = ? AND session_id = ?",
                (source_id, session_id),
            ).fetchone()
            if row is None:
                raise KeyError(f"workflow design source {source_id} not found")
            conn.execute("DELETE FROM workflow_design_sources WHERE id = ? AND session_id = ?", (source_id, session_id))
            conn.execute("UPDATE workflow_design_sessions SET updated_at = ? WHERE id = ?", (now, session_id))
            self._append_message(
                conn,
                session_id=session_id,
                role="assistant",
                content=f"已移除 source：{row['title']}。",
                payload={"event": "source_deleted", "source_id": source_id},
                created_at=now,
            )
        return self.session_payload(session_id)

    def add_message(self, *, session_id: str, role: str, content: str) -> dict[str, Any]:
        self._session_row(session_id)
        if role not in {"user", "assistant"}:
            raise RuntimeError("message role must be user or assistant")
        clean_content = content.strip()
        if not clean_content:
            raise RuntimeError("消息内容不能为空")
        now = now_iso()
        with self._connect() as conn:
            self._append_message(
                conn,
                session_id=session_id,
                role=role,
                content=clean_content,
                payload={"event": "message"},
                created_at=now,
            )
            conn.execute("UPDATE workflow_design_sessions SET updated_at = ? WHERE id = ?", (now, session_id))
        return self.session_payload(session_id)

    def compliance_report(self, *, session_id: str, judge: Any | None = None) -> dict[str, Any]:
        """对当前草稿跑规则合规校验（模块3 · 接入设计侧）：结构化预筛适用规则 →
        确定性检查 + 定性规则 LLM-judge，返回违规发现（带规则出处引用）。judge 可
        注入；缺省时若有定性规则则惰性构造 ComplianceJudgeAgent。"""
        from app.rag.compliance import check_compliance

        session = self._session_row(session_id)
        process = ProcessDefinition.model_validate(_json_loads(session["draft_definition_json"]))
        # 用户点了"合规体检"这个明确动作，不是隐式门控——不需要省这次 LLM 调用，
        # 缺省时直接用（惰性构造的）真实 judge，不用像 generate_draft 里那样看 diff 触发。
        report = check_compliance(process, process_id=process.meta.process_id, judge=judge or self._get_compliance_judge())
        return report.model_dump(mode="json")

    def _resolve_closed_coverage_gaps(
        self, *, definition: dict[str, Any], process_id: str, diff: dict[str, Any] | None = None
    ) -> None:
        """反哺闭环复检（简化版）：草稿改过之后，看之前记的"覆盖漏洞"洞察对应的环节是不是
        已经被处理——用两种最简单的结构性判断，都不重放原始表单值复评具体条件区间（那需要
        在 evidence 里存下触发卡住的原始表单值，目前没存，留到后续要精确复检时再补）：

        1. 环节现在有一条无条件路径（兜底/覆盖住了）；
        2. 这次编辑确实动了该环节的提交路径——新增一档（submit_paths.added）或改了现有某档的
           条件/目标（submit_paths.changed）都算。修覆盖漏洞的两种常见手法：漏了一档就加一档
           （added）、某档条件写窄了就放宽阈值（changed，如把「≤3天」放宽为「≤7天」补上
           3~7天空白）——两种都得认，不然"改条件"这类修复完了提醒还赖着不走。这是弱信号
           （不验证改完是否真的补上了那个区间），但足够消掉"改完了提醒还在"这个更常见的
           体验问题；万一改错了，运行数据会重新产生新的覆盖漏洞证据，不是永久性误判。"""
        if self._insight_store is None:
            return
        from data.schema import InsightKind

        nodes = definition.get("flow_nodes", [])
        nodes_by_id = {n.get("node_id"): n for n in nodes}
        diff_by_node = {c.get("key"): c for c in ((diff or {}).get("flow_nodes") or {}).get("changed", [])}
        for insight in self._insight_store.open_for(process_id):
            if insight.kind != InsightKind.COVERAGE_GAP or not insight.node_id:
                continue
            # 反哺洞察可能是运维 mock 库的合成流程记的（node_id 命名跟这条已发布/在编的
            # 定义不完全一致，如 "gm" vs "dept_gm"）——跟前端 resolveLoose 同一套子串
            # 松匹配，不然这份复检对着真实草稿永远对不上号，形同虚设。
            node = nodes_by_id.get(insight.node_id) or next(
                (n for n in nodes if insight.node_id in n.get("node_id", "") or n.get("node_id", "") in insight.node_id),
                None,
            )
            if node is None:
                continue
            has_fallback = any(not p.get("condition") for p in node.get("submit_paths", []))
            node_diff = diff_by_node.get(node.get("node_id")) or next(
                (c for c in diff_by_node.values() if insight.node_id in c.get("key", "") or c.get("key", "") in insight.node_id),
                None,
            )
            paths_diff = (node_diff or {}).get("submit_paths", {})
            touched_path = bool(paths_diff.get("added") or paths_diff.get("changed"))
            if has_fallback or touched_path:
                self._insight_store.resolve(insight.insight_id)

    # 分析/组织类洞察（sla_breach/slow_node/high_return/org_gap）不能像覆盖漏洞那样靠重扫定义
    # 确定性消解——它们是过去 30 天真实运行数据的事实，改设计不会让历史数字变假，要证明真的
    # 改善得等新一轮数据。所以设计侧针对某洞察做了真实改动后，只把这些洞察从 open 标成
    # acknowledged（"已响应·待新数据验证"）：退出"待处理"、不再当急需项堆着，但也不假装已解决。
    #
    # 两路信号，任一命中就 ack：
    # 1) 结构事实——diff 里真的动过某个环节（flow_nodes.changed），代码直接判定，不经 LLM。
    # 2) LLM 声明——改动没有直接落在某个环节上（比如改的是表单字段），但 LLM 在结构化输出里
    #    声明了"这次方案回应了哪条洞察"；只信这轮真的给它看过的 id（防编造），LLM 只声明
    #    "关联了哪条"，不判断"解决了没有"——这个判断权本来就不该交给它猜。
    def _acknowledge_responded_insights(
        self,
        *,
        process_id: str,
        diff: dict[str, Any] | None,
        shown_insight_ids: set[str] | None = None,
        addressed_insight_ids: list[str] | None = None,
    ) -> None:
        if self._insight_store is None:
            return
        from data.schema import InsightKind

        ack_kinds = {
            InsightKind.SLOW_NODE, InsightKind.HIGH_RETURN, InsightKind.SLA_BREACH, InsightKind.ORG_GAP,
        }
        to_ack: set[str] = set()

        changed = (diff or {}).get("flow_nodes", {}).get("changed") or []
        touched_ids = {c.get("key") for c in changed if c.get("key")}
        if touched_ids:
            for insight in self._insight_store.open_for(process_id):
                if insight.kind not in ack_kinds or not insight.node_id:
                    continue
                # 松匹配（mock 用 gm、真实用 dept_gm）——跟 _resolve_closed_coverage_gaps 同一套
                if insight.node_id in touched_ids or any(
                    insight.node_id in t or t in insight.node_id for t in touched_ids
                ):
                    to_ack.add(insight.insight_id)

        if addressed_insight_ids:
            shown = shown_insight_ids or set()
            for insight_id in addressed_insight_ids:
                if insight_id not in shown:
                    continue  # LLM 编造了这轮根本没给它看过的 id，不予采信
                insight = self._insight_store.get(insight_id)
                if insight is not None and insight.kind in ack_kinds:
                    to_ack.add(insight_id)

        for insight_id in to_ack:
            self._insight_store.acknowledge(insight_id)

    @staticmethod
    def _is_structural_change(diff: dict[str, Any]) -> bool:
        """判断一次 diff 是否含"结构性改动"——改变流程执行骨架、影响实际运行行为的修改，
        需用户确认后才写入草稿；纯文案/说明/处理期限微调不算，直接落草稿。

        算结构性：
        - 环节增删（flow_nodes.added / removed）
        - 提交路径增删改（submit_paths.added / removed / changed）——改流转条件或目标
        - 处理人变更（flow_node 的 handler 属性变了）——改"谁来审"
        - 表单字段增删（form_fields.added / removed）——改"要收集什么"
        不算结构性（直接落草稿）：改字段说明/必填、改环节名/意见标签/处理期限、改流程描述等文案。
        """
        nodes = diff.get("flow_nodes") or {}
        if nodes.get("added") or nodes.get("removed"):
            return True
        for changed in nodes.get("changed") or []:
            paths = changed.get("submit_paths") or {}
            if paths.get("added") or paths.get("removed") or paths.get("changed"):
                return True
            if any(c.get("attribute") == "handler" for c in (changed.get("changes") or [])):
                return True
        fields = diff.get("form_fields") or {}
        if fields.get("added") or fields.get("removed"):
            return True
        return False

    def generate_draft(self, *, session_id: str) -> dict[str, Any]:
        """处理会话里最新一条未处理的用户消息：交给对话式修改 agent 解析成结构化
        编辑操作、确定性应用、校验、生成 diff，并把结果作为一条 assistant 消息追加。

        没有新的用户消息时（例如重复点击而没有新输入），直接返回当前会话，不
        重复调用 LLM、不重复应用上一条指令。有未解决的合规确认时同样直接返回，
        不接受新指令——必须先 confirm_pending_edit / discard_pending_edit。
        """
        session = self._session_row(session_id)
        if session.get("pending_definition_json"):
            return self.session_payload(session_id)
        latest_user_message = self._latest_user_message(session_id)
        if latest_user_message is None or latest_user_message["id"] == session.get("last_processed_message_id"):
            return self.session_payload(session_id)

        draft_definition = _json_loads(session["draft_definition_json"])
        process = ProcessDefinition.model_validate(draft_definition)
        conversation_context = self._message_context(session_id)
        open_clarifications = self._open_clarifications(session)
        source_context = self._source_context(session_id)
        # 反哺闭环：让设计副驾每轮都看得到运行侧洞察，不用等用户点"让副驾诊断"按钮把证据
        # 现拼进指令文本才知道——不然自由对话问"这条流程有什么问题/能优化什么"时，agent
        # 只能对着流程定义本身做静态审查，看不到运维/分析已经确定性发现的真实问题。
        open_insights = (
            [i.model_dump(mode="json") for i in self._insight_store.open_for(process.meta.process_id)]
            if self._insight_store is not None
            else []
        )

        proposal = self._get_edit_agent().run(
            process=process,
            instruction=latest_user_message["content"],
            conversation_context=conversation_context,
            open_clarifications=open_clarifications,
            source_context=source_context,
            open_insights=open_insights,
        )
        apply_result = apply_edit_operations(process, proposal.operations)
        diff = diff_process_definitions(process, apply_result.process)
        new_definition = apply_result.process.model_dump(mode="json")
        validation_report = self._validate_definition(new_definition)
        status = "NEEDS_REVIEW" if validation_report["issue_count"] else "GENERATED"

        # reply 只放 agent 的自然语言说明；错误/diff 走结构化 payload，由前端
        # 渲染成独立的错误提示条 / diff 卡片，避免同一份信息在文本和卡片里重复出现。
        #
        # 用 diff（真实数据是否变化）而不是 apply_result.applied（操作是否执行成功、
        # 不代表值真的变了——例如 LLM 重复提出"改成同一个值"的更新，_apply_one 不会
        # 报错，会被计入 applied，但字段值前后相同）来判断这一轮是否发生了真实变更。
        has_real_changes = diff["has_changes"]

        # 只拦"这次编辑新引入的违规"——用编辑前/编辑后各查一次确定性规则、取差集。
        # 草稿里本来就有的问题（比如 AI 初始化时就缺的审批环节）不算这次编辑的账，
        # 不然只要草稿本身没达标，随便改一个字段都会被拦，变成狼来了。
        new_violations: list[dict[str, Any]] = []
        if has_real_changes:
            from app.rag.compliance import deterministic_findings_for_draft, new_qualitative_findings_for_edit

            findings_before = {
                f.rule_id
                for f in deterministic_findings_for_draft(process, process_id=process.meta.process_id)
            }
            findings_after = deterministic_findings_for_draft(
                apply_result.process, process_id=apply_result.process.meta.process_id
            )
            new_violations = [f.model_dump(mode="json") for f in findings_after if f.rule_id not in findings_before]

            # 定性规则（命名规范、回避要求这类 LLM-judge）同一个"只拦新引入违规"语义，
            # 但智能门控：只有这次 diff 真碰到某条定性规则的地盘才为它打 LLM，跟这次编辑
            # 无关的定性规则（diff 里没触发）不查，不让每次编辑都平白多等一次 LLM。
            # judge 传可能是 None 的注入值，不用 _get_compliance_judge()——那个 getter 会立刻
            # 构造真实 Bedrock client；这里要让 new_qualitative_findings_for_edit 内部先判定
            # 触没触发智能门控，真触发了才需要一个 judge 实例，没触发时压根不该建它。
            qualitative_new = new_qualitative_findings_for_edit(
                before=process,
                after=apply_result.process,
                diff=diff,
                process_id=apply_result.process.meta.process_id,
                judge=self._compliance_judge,
            )
            new_violations.extend(f.model_dump(mode="json") for f in qualitative_new)

        # 主动提醒（跟"判违规"是两回事）：这次编辑改动过的东西文本上碰到了哪些制度规则，
        # 就把相关规则摆进待确认卡当提醒——不判违规、不打 LLM，纯确定性子串匹配。用户
        # 编辑金额阈值、委员会路由这类被制度管着的地方时，当场知道"你动的这块有制度约束"，
        # 哪怕这次改动本身没违规（如把阈值改得更严）。已经作为违规报出来的规则不重复提醒。
        related_rules: list[dict[str, Any]] = []
        if has_real_changes:
            from app.rag.compliance import _select_applicable_rules, rules_touched_by_edit
            from app.rag.rules import load_atomic_rules

            violated_ids = {v["rule_id"] for v in new_violations}
            applicable = _select_applicable_rules(
                load_atomic_rules(), apply_result.process, apply_result.process.meta.process_id, None
            )
            related_rules = [
                {
                    "rule_id": r.rule_id,
                    "statement": r.statement,
                    "source_doc": r.provenance.source_doc,
                    "clause": r.provenance.clause,
                    "severity": r.severity,
                }
                for r in rules_touched_by_edit(applicable, diff)
                if r.rule_id not in violated_ids
            ]

        # 确认门槛（用户定的标准）：只要这一轮真的改动了流程定义（diff 有变化），无论改的是
        # 结构、处理期限还是纯文案说明，都先进"待确认"、由用户确认后才写入草稿——不再分
        # 结构性/非结构性直接落地。让"改流程定义"这件事有统一、可预期的确认动作，避免
        # "有些改动弹确认卡、有些悄悄落库"的分裂观感（后者正是之前让用户困惑的根源）。
        # _is_structural_change 仍保留，只用来给待确认卡片分类文案（pending_reason），不再决定拦不拦。
        structural_change = has_real_changes and self._is_structural_change(diff)
        needs_confirmation = has_real_changes
        pending_reason = "compliance" if new_violations else ("structural" if structural_change else "edit")

        now = now_iso()
        if needs_confirmation:
            # 拦下：不写 draft_definition_json，先把拟议结果存进 pending_definition_json，
            # 等用户 confirm/discard；这条消息也标 requires_confirmation，前端据此渲染
            # 确认/放弃按钮并禁用输入框——不能一边等确认一边已经把拟议草稿落库了。
            with self._connect() as conn:
                conn.execute(
                    """
                    UPDATE workflow_design_sessions
                    SET pending_definition_json = ?, last_processed_message_id = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (_json_dumps(new_definition), latest_user_message["id"], now, session_id),
                )
                self._mark_sources_parsed(conn, session_id=session_id)
                pending_message_id = self._append_message(
                    conn,
                    session_id=session_id,
                    role="assistant",
                    content=proposal.reply,
                    payload={
                        "event": "edit_pending_confirmation",
                        # 区分待确认原因，只用于前端确认提示文案（都走同一套 confirm/discard）：
                        # compliance=新引入合规违规；structural=改流程骨架（增删环节/路径、改处理人/字段）；
                        # edit=其它一般定义改动（改处理期限、字段说明、环节名等）。
                        "pending_reason": pending_reason,
                        "applied": [item.model_dump(mode="json") for item in apply_result.applied],
                        "errors": apply_result.errors,
                        "diff": diff,
                        "compliance_findings": new_violations,
                        "related_rules": related_rules,
                        "resolved_clarification_ids": proposal.resolved_clarification_ids,
                        "addressed_insight_ids": proposal.addressed_insight_ids,
                        "open_insight_ids": [i.get("insight_id") for i in open_insights],
                        "out_of_scope": proposal.out_of_scope,
                        "requires_confirmation": True,
                    },
                    created_at=now,
                )
                conn.execute(
                    "UPDATE workflow_design_sessions SET pending_message_id = ? WHERE id = ?",
                    (pending_message_id, session_id),
                )
            return self.session_payload(session_id)

        # previous_draft_definition_json 只在真实变更时才更新为"变更前"的草稿；只是
        # 问答/重复确认而没有实际数据变化时，保留原来的"上一版"不动——否则一次追问
        # 就会把可撤销的目标悄悄换成"当前状态"，导致上一次真实编辑再也无法撤销。
        previous_draft_value = (
            session["draft_definition_json"] if has_real_changes else session.get("previous_draft_definition_json")
        )
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE workflow_design_sessions
                SET status = ?, draft_definition_json = ?, previous_draft_definition_json = ?,
                    validation_report_json = ?, last_processed_message_id = ?, updated_at = ?
                WHERE id = ?
                """,
                (
                    status,
                    _json_dumps(new_definition),
                    previous_draft_value,
                    _json_dumps(validation_report),
                    latest_user_message["id"],
                    now,
                    session_id,
                ),
            )
            self._mark_sources_parsed(conn, session_id=session_id)
            content = proposal.reply
            if proposal.operations and not has_real_changes and not apply_result.errors:
                # LLM 的 reply 文字有时会描述一个具体改动（"已把条件从 X 改成 Y"），但对应
                # 的结构化 operation 里 updates 字段实际是空的/值跟原来一样，确定性应用层
                # 判定没有真变化——这时不能让文字说法单方面当真（那是"说了但没做"的假成功），
                # 必须用 diff 这个确定性结果去纠正用户观感，不然用户会以为改完了、其实草稿
                # 原封不动。条件排除三种不该提示的场景：operations 为空的纯问答；有
                # apply_result.errors 的（那种已经有独立的错误提示条，见下面 payload.errors，
                # 重复提示是噪音）；以及真的产生了变化的正常情况。
                content += (
                    "\n\n（系统提示：本轮虽然尝试了编辑操作，但流程定义实际未发生变化——"
                    "可能是指令没有被准确理解，建议换个更具体的说法重试，比如明确指出要改"
                    "哪条路径的哪个属性、改成什么值。）"
                )
            self._append_message(
                conn,
                session_id=session_id,
                role="assistant",
                content=content,
                payload={
                    "event": "edit_applied" if has_real_changes else "edit_no_change",
                    "applied": [item.model_dump(mode="json") for item in apply_result.applied],
                    "errors": apply_result.errors,
                    "diff": diff,
                    "compliance_findings": [],
                    "resolved_clarification_ids": proposal.resolved_clarification_ids,
                    "addressed_insight_ids": proposal.addressed_insight_ids,
                    "out_of_scope": proposal.out_of_scope,
                },
                created_at=now,
            )
        if has_real_changes:
            pid = apply_result.process.meta.process_id
            self._resolve_closed_coverage_gaps(definition=new_definition, process_id=pid, diff=diff)
            self._acknowledge_responded_insights(
                process_id=pid,
                diff=diff,
                shown_insight_ids={i.get("insight_id") for i in open_insights},
                addressed_insight_ids=proposal.addressed_insight_ids,
            )
        return self.session_payload(session_id)

    def confirm_pending_edit(self, *, session_id: str) -> dict[str, Any]:
        """确认应用待确认的合规拦截修改：把 pending_definition_json 正式落进
        draft_definition_json，并把编辑前的草稿存进 previous_draft_definition_json
        供 undo（这确实是一次真实变更）。"""
        session = self._session_row(session_id)
        pending = session.get("pending_definition_json")
        if not pending:
            raise RuntimeError("当前没有待确认的修改。")
        validation_report = self._validate_definition(_json_loads(pending))
        status = "NEEDS_REVIEW" if validation_report["issue_count"] else "GENERATED"
        now = now_iso()
        with self._connect() as conn:
            # 拟议时算好的 diff 存在待确认消息的 payload 里——复检覆盖漏洞要用它判断
            # 这次确认新增了哪些路径，跟 generate_draft 直接落草稿分支用的是同一份 diff。
            pending_diff: dict[str, Any] | None = None
            pending_addressed_ids: list[str] = []
            pending_shown_ids: set[str] = set()
            pending_message_id = session.get("pending_message_id")
            if pending_message_id:
                row = conn.execute(
                    "SELECT payload_json FROM workflow_design_messages WHERE id = ?", (pending_message_id,)
                ).fetchone()
                if row is not None:
                    pending_payload = _json_loads(row["payload_json"], {})
                    pending_diff = pending_payload.get("diff")
                    pending_addressed_ids = pending_payload.get("addressed_insight_ids") or []
                    pending_shown_ids = set(pending_payload.get("open_insight_ids") or [])
            conn.execute(
                """
                UPDATE workflow_design_sessions
                SET status = ?, draft_definition_json = ?, previous_draft_definition_json = ?,
                    validation_report_json = ?, pending_definition_json = NULL, pending_message_id = NULL,
                    updated_at = ?
                WHERE id = ?
                """,
                (status, pending, session["draft_definition_json"], _json_dumps(validation_report), now, session_id),
            )
            self._resolve_pending_message(conn, session.get("pending_message_id"), resolution="confirmed")
            self._append_message(
                conn,
                session_id=session_id,
                role="assistant",
                content="已确认应用该修改。",
                payload={"event": "edit_pending_resolved", "resolution": "confirmed"},
                created_at=now,
            )
        pending_definition = _json_loads(pending)
        process_id = (pending_definition.get("meta") or {}).get("process_id") or session["workflow_definition_id"]
        self._resolve_closed_coverage_gaps(definition=pending_definition, process_id=process_id, diff=pending_diff)
        self._acknowledge_responded_insights(
            process_id=process_id,
            diff=pending_diff,
            shown_insight_ids=pending_shown_ids,
            addressed_insight_ids=pending_addressed_ids,
        )
        return self.session_payload(session_id)

    def discard_pending_edit(self, *, session_id: str) -> dict[str, Any]:
        """放弃待确认的修改：草稿保持编辑前的状态不变。"""
        session = self._session_row(session_id)
        if not session.get("pending_definition_json"):
            raise RuntimeError("当前没有待确认的修改。")
        now = now_iso()
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE workflow_design_sessions
                SET pending_definition_json = NULL, pending_message_id = NULL, updated_at = ?
                WHERE id = ?
                """,
                (now, session_id),
            )
            self._resolve_pending_message(conn, session.get("pending_message_id"), resolution="discarded")
            self._append_message(
                conn,
                session_id=session_id,
                role="assistant",
                content="已放弃该修改，草稿保持不变。",
                payload={"event": "edit_pending_resolved", "resolution": "discarded"},
                created_at=now,
            )
        return self.session_payload(session_id)

    def undo_last_change(self, *, session_id: str) -> dict[str, Any]:
        """撤销最近一次对话式编辑（单步 undo：只保留编辑前的上一版草稿）。"""
        session = self._session_row(session_id)
        previous = session.get("previous_draft_definition_json")
        if not previous:
            raise RuntimeError("当前没有可撤销的修改。")
        validation_report = self._validate_definition(_json_loads(previous))
        status = "NEEDS_REVIEW" if validation_report["issue_count"] else "GENERATED"
        now = now_iso()
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE workflow_design_sessions
                SET status = ?, draft_definition_json = ?, previous_draft_definition_json = NULL,
                    validation_report_json = ?, updated_at = ?
                WHERE id = ?
                """,
                (status, previous, _json_dumps(validation_report), now, session_id),
            )
            self._append_message(
                conn,
                session_id=session_id,
                role="assistant",
                content="已撤销上一次修改。",
                payload={"event": "edit_undone"},
                created_at=now,
            )
        return self.session_payload(session_id)

    def _get_edit_agent(self) -> Any:
        if self._edit_agent is None:
            from app.agents.design_edit_agent import DesignEditAgent

            self._edit_agent = DesignEditAgent()
        return self._edit_agent

    def _get_compliance_judge(self) -> Any:
        if self._compliance_judge is None:
            from app.agents.compliance_judge_agent import ComplianceJudgeAgent

            self._compliance_judge = ComplianceJudgeAgent()
        return self._compliance_judge

    def _latest_user_message(self, session_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT * FROM workflow_design_messages
                WHERE session_id = ? AND role = 'user'
                ORDER BY created_at DESC, id DESC LIMIT 1
                """,
                (session_id,),
            ).fetchone()
        return dict(row) if row else None

    def _open_clarifications(self, session: dict[str, Any]) -> list[dict[str, Any]]:
        """待确认项，供编辑 agent 判断本轮回复是否解决了某个 clarification。

        优先取 LangGraph 初始化产出的结构化待确认项（有稳定 id）；退化到早期
        fake 生成报告里的纯文本 clarifications 时，用序号拼出临时 id。
        """
        generation_report = _json_loads(session.get("generation_report_json"), {})
        structured = (generation_report.get("langgraph") or {}).get("user_clarification_requests") or []
        if structured:
            return [
                {"id": item.get("id"), "question": item.get("question"), "options": item.get("options")}
                for item in structured
                if isinstance(item, dict)
            ]
        fallback = generation_report.get("clarifications") or []
        return [{"id": f"clar_{index}", "question": text, "options": []} for index, text in enumerate(fallback, start=1)]

    def validate_session(self, *, session_id: str) -> dict[str, Any]:
        session = self._session_row(session_id)
        draft_definition = _json_loads(session["draft_definition_json"])
        validation_report = self._validate_definition(draft_definition)
        status = "VALIDATED" if validation_report["issue_count"] == 0 else "NEEDS_REVIEW"
        now = now_iso()
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE workflow_design_sessions
                SET status = ?, validation_report_json = ?, updated_at = ?
                WHERE id = ?
                """,
                (status, _json_dumps(validation_report), now, session_id),
            )
            self._append_message(
                conn,
                session_id=session_id,
                role="assistant",
                content=self._validation_message(validation_report),
                payload={"event": "draft_validated", "report": validation_report},
                created_at=now,
            )
        return self.session_payload(session_id)

    def save_session(self, *, session_id: str) -> dict[str, Any]:
        self._session_row(session_id)
        now = now_iso()
        with self._connect() as conn:
            conn.execute("UPDATE workflow_design_sessions SET updated_at = ? WHERE id = ?", (now, session_id))
        return self.session_payload(session_id)

    def delete_session(self, *, session_id: str) -> dict[str, Any]:
        session = self._session_row(session_id)
        workflow = self._workflow_row(session["workflow_definition_id"])
        is_new_workflow_draft = workflow["status"] == "DRAFT" and session["base_version"] == "NEW"

        with self._connect() as conn:
            if is_new_workflow_draft:
                runtime_row = conn.execute(
                    "SELECT COUNT(*) AS value FROM workflow_cases WHERE workflow_definition_id = ?",
                    (workflow["id"],),
                ).fetchone()
                runtime_count = int(runtime_row["value"] if runtime_row else 0)
                if runtime_count:
                    raise RuntimeError("该草稿流程已有运行实例，不能删除")

            if is_new_workflow_draft:
                session_ids = [
                    row["id"]
                    for row in conn.execute(
                        "SELECT id FROM workflow_design_sessions WHERE workflow_definition_id = ?",
                        (workflow["id"],),
                    ).fetchall()
                ]
            else:
                session_ids = [session_id]

            for current_session_id in session_ids:
                conn.execute("DELETE FROM workflow_design_messages WHERE session_id = ?", (current_session_id,))
                conn.execute("DELETE FROM workflow_design_sources WHERE session_id = ?", (current_session_id,))
                conn.execute("DELETE FROM workflow_design_sessions WHERE id = ?", (current_session_id,))

            deleted_workflow_definition = False
            if is_new_workflow_draft:
                conn.execute("DELETE FROM workflow_definitions WHERE id = ? AND status = 'DRAFT'", (workflow["id"],))
                deleted_workflow_definition = True

        return {
            "deleted": True,
            "session_id": session_id,
            "workflow_definition_id": workflow["id"],
            "deleted_workflow_definition": deleted_workflow_definition,
        }

    def delete_workflow_definition(self, *, workflow_definition_id: str) -> dict[str, Any]:
        """从流程管理删除一个流程（连带其全部设计会话/来源/消息）。用户明确要求全部可删，
        不保护种子流程；OA 流转已下掉，不再对运行实例做拦截。"""
        workflow = self._workflow_row(workflow_definition_id)
        with self._connect() as conn:
            session_ids = [
                row["id"]
                for row in conn.execute(
                    "SELECT id FROM workflow_design_sessions WHERE workflow_definition_id = ?",
                    (workflow_definition_id,),
                ).fetchall()
            ]
            for sid in session_ids:
                conn.execute("DELETE FROM workflow_design_messages WHERE session_id = ?", (sid,))
                conn.execute("DELETE FROM workflow_design_sources WHERE session_id = ?", (sid,))
                conn.execute("DELETE FROM workflow_design_sessions WHERE id = ?", (sid,))
            conn.execute("DELETE FROM workflow_definitions WHERE id = ?", (workflow_definition_id,))
        return {
            "deleted": True,
            "workflow_definition_id": workflow_definition_id,
            "name": workflow["name"],
            "deleted_sessions": len(session_ids),
        }

    def publish_workflow_definition(self, *, workflow_definition_id: str) -> dict[str, Any]:
        """上架：把流程标记为 PUBLISHED，使其出现在「发起申请」。OA 真实流转不接——
        上架只让它在发起页可见、可填表单、可预览确定性路径，不启动真实 case 流转。"""
        workflow = self._workflow_row(workflow_definition_id)
        now = now_iso()
        with self._connect() as conn:
            conn.execute(
                "UPDATE workflow_definitions SET status = 'PUBLISHED', published_at = ?, updated_at = ? WHERE id = ?",
                (now, now, workflow_definition_id),
            )
        return {
            "published": True,
            "workflow_definition_id": workflow_definition_id,
            "name": workflow["name"],
            "status": "PUBLISHED",
            "published_at": now,
        }

    def unpublish_workflow_definition(self, *, workflow_definition_id: str) -> dict[str, Any]:
        """下架：把流程状态改回 DRAFT，从「发起申请」隐藏。设计草稿/历史都不受影响，
        随时可以再次上架——跟删除不同，这是可逆操作。"""
        workflow = self._workflow_row(workflow_definition_id)
        now = now_iso()
        with self._connect() as conn:
            conn.execute(
                "UPDATE workflow_definitions SET status = 'DRAFT', published_at = NULL, updated_at = ? WHERE id = ?",
                (now, workflow_definition_id),
            )
        return {
            "published": False,
            "workflow_definition_id": workflow_definition_id,
            "name": workflow["name"],
            "status": "DRAFT",
        }

    def design_activity_counts(self) -> dict[str, int]:
        """设计侧自助活动计数（看板价值层「负责人自助上线」用）。

        都是从会话消息里已有的事件直接数出来的**真实记录**，不是估算：
        - 初始化条数：assistant 消息事件 draft_initialized（负责人用副驾从零建了一条流程）
        - 调整次数：assistant 消息事件 edit_applied（会话式修改真正确认落地的那些，
          不含 edit_no_change 那种说了但没改成的）
        """
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT payload_json FROM workflow_design_messages WHERE role = 'assistant'"
            ).fetchall()
        initialized = adjusted = 0
        for row in rows:
            event = (_json_loads(row["payload_json"], {}) or {}).get("event")
            if event == "draft_initialized":
                initialized += 1
            elif event == "edit_applied":
                adjusted += 1
        return {"initialized": initialized, "adjusted": adjusted}

    def design_summary_for_workflow(self, workflow_definition_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT * FROM workflow_design_sessions
                WHERE workflow_definition_id = ?
                ORDER BY updated_at DESC
                LIMIT 1
                """,
                (workflow_definition_id,),
            ).fetchone()
        if row is None:
            return None
        draft = _json_loads(row["draft_definition_json"])
        report = _json_loads(row["validation_report_json"], {})
        issue_count = int(report.get("issue_count") or 0)
        progress = "70%" if row["status"] in {"GENERATED", "VALIDATED"} else "35%"
        # 按草稿（不是已发布的 definition_json）算未配置处理期限的环节——跟卡片上
        # issue_count 同源，避免 AI 已在草稿里修好、但卡片这一项仍读已发布版本
        # 显示"未修"的撕裂（覆盖 issue_count 用的是这套 overlay，这个字段之前漏了）。
        missing_time_limit_nodes = [
            node.get("node_id")
            for node in (draft.get("flow_nodes") or [])
            if node.get("time_limit_days") is None
        ]
        # _validate_definition 不再把"未配处理期限"计入 issues（可选建议项，非规定项），
        # 所以这里就是 issue_count 本身——保留这个字段名是为了不动前端/其它接口的调用面。
        non_time_limit_issue_count = issue_count
        return {
            "session_id": row["id"],
            "mode": row["mode"],
            "draft_state": "校验通过" if row["status"] == "VALIDATED" else "AI 草稿",
            "draft_version": draft.get("meta", {}).get("version", "V1.1.0-draft"),
            "draft_progress": progress,
            "issue_count": issue_count,
            "health": "待确认" if issue_count else "草稿正常",
            "health_tone": "amber" if issue_count else "green",
            "next_action": (
                f"AI 草稿有 {issue_count} 个校验提示，建议进入 AI 流程设计确认。"
                if issue_count
                else "AI 草稿已生成，可进入设计页查看字段、环节和路径。"
            ),
            "updated_at": row["updated_at"],
            "base_version": row["base_version"],
            "can_delete_draft": True,
            "is_new_draft": row["base_version"] == "NEW",
            "missing_time_limit_nodes": missing_time_limit_nodes,
            "non_time_limit_issue_count": non_time_limit_issue_count,
        }

    def _run_initialization_job(
        self,
        *,
        job_id: str,
        created_by: str,
        workflow_type: str,
        workflow_name: str,
        category: str,
        instruction: str,
        sources: list[dict[str, Any]],
        uploaded_files: list[UploadedSourceFile] | None = None,
        eval_context: dict[str, Any] | None = None,
    ) -> None:
        self._update_initialization_job(job_id, status="running")
        self._record_initialization_event(
            job_id,
            {
                "event_type": "job_started",
                "kind": "job_status",
                "status": "running",
                "message": "LangGraph 初始化开始",
            },
        )
        try:
            payload = self.initialize_new_session(
                created_by=created_by,
                workflow_type=workflow_type,
                workflow_name=workflow_name,
                category=category,
                instruction=instruction,
                sources=sources,
                uploaded_files=uploaded_files or [],
                progress_callback=lambda event: self._record_initialization_event(
                    job_id,
                    self._normalize_initialization_event(event),
                ),
                is_eval_run=bool(eval_context),
            )
        except Exception as exc:  # noqa: BLE001 - keep async job errors visible to the UI.
            self._update_initialization_job(job_id, status="failed", error=str(exc))
            self._record_initialization_event(
                job_id,
                {
                    "event_type": "job_failed",
                    "kind": "job_status",
                    "status": "failed",
                    "message": f"LangGraph 初始化失败：{exc}",
                    "error": str(exc),
                },
            )
            return

        session_id = payload["session"]["session_id"]
        workflow_definition_id = payload["workflow"]["workflow_definition_id"]
        workflow_name_out = payload["workflow"].get("name") or workflow_name

        # 评测模式叠加层：对刚产出的 run_dir 打分，run_id=流程定义 id（卡片↔评测共享一个 id），入历史
        eval_result: dict[str, Any] | None = None
        if eval_context:
            try:
                self._record_initialization_event(
                    job_id,
                    {"event_type": "node_progress", "kind": "node_step", "status": "running",
                     "message": "设计草稿已生成，正在对 gold 打分…"},
                )
                from app.eval import eval_run_service as _eval

                run_dir = DESIGN_INITIALIZATION_RUN_DIR / session_id / "run"
                entry = _eval.score_and_append_run(
                    case_id=eval_context["case_id"],
                    run_dir=run_dir,
                    workflow_definition_id=workflow_definition_id,
                    workflow_name=workflow_name_out,
                    session_id=session_id,
                    included=eval_context.get("included", []),
                    excluded=eval_context.get("excluded", []),
                    extra_count=eval_context.get("extra_count", 0),
                )
                eval_result = {"case_id": eval_context["case_id"], "run_id": entry["run_id"], "total": entry["total"]}
                self._update_initialization_job(job_id, eval_run_id=entry["run_id"])
            except Exception as exc:  # noqa: BLE001 - 打分失败不该拖垮设计产出（卡片已建好）
                self._record_initialization_event(
                    job_id,
                    {"event_type": "eval_failed", "kind": "job_status", "status": "running",
                     "message": f"设计已完成，但评测打分失败：{exc}", "error": str(exc)},
                )

        self._update_initialization_job(job_id, status="completed", session_id=session_id)
        completed_event = {
            "event_type": "job_completed",
            "kind": "job_status",
            "status": "completed",
            "message": "AI 初始化完成，已写入设计草稿",
            "session_id": session_id,
            "workflow_definition_id": workflow_definition_id,
        }
        if eval_result:
            completed_event["message"] = f"评测完成 · 总分 {eval_result['total']}，草稿已入流程管理"
            completed_event["eval"] = eval_result
        self._record_initialization_event(job_id, completed_event)

    def _update_initialization_job(self, job_id: str, **updates: Any) -> None:
        with self._initialization_job_condition:
            job = self._initialization_jobs.get(job_id)
            if job is None:
                raise KeyError(f"workflow design initialization job {job_id} not found")
            job.update(updates)
            job["updated_at"] = now_iso()
            self._initialization_job_condition.notify_all()

    def _record_initialization_event(self, job_id: str, event: dict[str, Any]) -> None:
        with self._initialization_job_condition:
            if job_id not in self._initialization_jobs:
                return
            events = self._initialization_job_events.setdefault(job_id, [])
            events.append(
                {
                    "sequence": len(events) + 1,
                    "created_at": now_iso(),
                    **event,
                }
            )
            self._initialization_job_condition.notify_all()

    def _normalize_initialization_event(self, event: dict[str, Any]) -> dict[str, Any]:
        payload = dict(event)
        event_type = payload.pop("event_type", "")
        if event_type == "custom":
            payload["event_type"] = "node_progress"
        elif event_type == "node_done":
            payload["event_type"] = "node_done"
            payload.setdefault("kind", "node_status")
        else:
            payload["event_type"] = event_type or "node_progress"
        payload.setdefault("kind", "node_step")
        payload.setdefault("status", "running")
        payload.setdefault("message", "")
        return payload

    def _normalize_uploaded_file(self, file: UploadedSourceFile, *, index: int) -> UploadedSourceFile:
        title = (file.get("filename") or file.get("title") or f"uploaded_source_{index}").strip()
        content = file.get("content") or b""
        if isinstance(content, str):
            content = content.encode("utf-8")
        source_type = (Path(title).suffix.removeprefix(".") or file.get("source_type") or "file").lower()
        return {
            "title": title,
            "original_filename": title,
            "source_type": source_type,
            "mime_type": file.get("mime_type") or file.get("content_type"),
            "size_bytes": int(file.get("size_bytes") or len(content)),
            "content": content,
            "kind": "file",
        }

    def _source_asset_summary(self, source: dict[str, Any]) -> str:
        if source.get("kind") == "file":
            size_bytes = int(source.get("size_bytes") or 0)
            if size_bytes >= 1024 * 1024:
                size_text = f"{size_bytes / 1024 / 1024:.1f} MB"
            elif size_bytes >= 1024:
                size_text = f"{max(1, round(size_bytes / 1024))} KB"
            else:
                size_text = f"{size_bytes} B"
            return f"{size_text} · 原始文件已上传，等待解析"
        return self._summarize_source(source.get("content", ""))

    def _initial_draft(self, base_definition: dict[str, Any]) -> dict[str, Any]:
        draft = json.loads(json.dumps(base_definition, ensure_ascii=False))
        draft.setdefault("meta", {})["version"] = "V1.1.0-draft"
        return draft

    def _initialize_definition_with_langgraph(
        self,
        *,
        session_id: str,
        process_id: str,
        process_name: str,
        category: str,
        sources: list[dict[str, Any]],
        progress_callback: Callable[[dict[str, Any]], None] | None = None,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        run_root = DESIGN_INITIALIZATION_RUN_DIR / session_id
        case_dir = run_root / "case"
        out_dir = run_root / "run"
        case_dir.mkdir(parents=True, exist_ok=True)
        out_dir.mkdir(parents=True, exist_ok=True)
        uploaded_source_manifest = self._write_initialization_source_package(sources=sources, case_dir=case_dir)
        (run_root / "input_manifest.json").write_text(
            _json_dumps(
                {
                    "session_id": session_id,
                    "process_id": process_id,
                    "process_name": process_name,
                    "category": category,
                    "source_count": len(sources),
                    "case_dir": str(case_dir),
                    "out_dir": str(out_dir),
                    "uploaded_source_manifest": uploaded_source_manifest,
                }
            )
            + "\n",
            encoding="utf-8",
        )

        try:
            if progress_callback is not None and self._uses_default_graph_initializer:
                graph_result = self._run_langgraph_initialization_stream(
                    case_dir=case_dir,
                    out_dir=out_dir,
                    session_id=session_id,
                    progress_callback=progress_callback,
                )
            else:
                graph_result = self.graph_initializer(case_dir=case_dir, out_dir=out_dir)
        except Exception as exc:  # noqa: BLE001 - surface the initialization failure to the product UI.
            raise RuntimeError(f"LangGraph 初始化失败：{exc}") from exc

        draft = self._process_definition_from_graph_result(graph_result, out_dir=out_dir)
        draft = json.loads(json.dumps(draft, ensure_ascii=False))
        meta = draft.setdefault("meta", {})
        meta["process_id"] = process_id
        if process_name and process_name != "未命名审批流程":
            meta["process_name"] = process_name
        else:
            meta.setdefault("process_name", process_name)
        meta["version"] = "V0.1.0-draft"
        if not meta.get("description"):
            combined = "\n".join(source.get("content", "") for source in sources if source.get("content"))
            meta["description"] = self._description_from_sources(
                combined,
                fallback=f"根据用户输入初始化的{category}草稿。",
            )

        process = ProcessDefinition.model_validate(draft)
        output_paths = graph_result.get("output_paths", {}) if isinstance(graph_result, dict) else {}
        workflow_design_output = graph_result.get("workflow_design_output", {}) if isinstance(graph_result, dict) else {}
        workflow_design_output = json.loads(json.dumps(workflow_design_output, ensure_ascii=False))
        if workflow_design_output:
            workflow_design_output["process_definition"] = process.model_dump(mode="json")
            workflow_design_output["draft_version"] = process.meta.version
            workflow_design_output.setdefault("design_summary", {}).update(
                {
                    "field_count": len(process.form_fields),
                    "node_count": len(process.flow_nodes),
                    "path_count": sum(len(node.submit_paths) for node in process.flow_nodes),
                    "role_count": len(process.roles or []),
                    "attachment_count": len(process.attachments or []),
                }
            )
        designer_assistant_message = (
            graph_result.get("designer_assistant_message", {}) if isinstance(graph_result, dict) else {}
        )
        user_clarification_requests = (
            graph_result.get("user_clarification_requests", []) if isinstance(graph_result, dict) else []
        )
        return process.model_dump(mode="json"), {
            "run_dir": str(run_root),
            "case_dir": str(case_dir),
            "out_dir": str(out_dir),
            "uploaded_source_manifest": uploaded_source_manifest,
            "output_paths": output_paths,
            "workflow_design_output": workflow_design_output,
            "designer_assistant_message": designer_assistant_message,
            "user_clarification_requests": user_clarification_requests,
            "design_persistence_report": graph_result.get("design_persistence_report", {})
            if isinstance(graph_result, dict)
            else {},
            "schema_validation_report": graph_result.get("schema_validation_report", {})
            if isinstance(graph_result, dict)
            else {},
            "business_validation_result": graph_result.get("business_validation_result", {})
            if isinstance(graph_result, dict)
            else {},
        }

    def _run_langgraph_initialization(self, *, case_dir: Path, out_dir: Path) -> dict[str, Any]:
        from app.tools.vision_ocr import build_bedrock_image_transcriber
        from app.workflows.process_v1 import run_process_case

        return run_process_case(
            case_dir=case_dir,
            out_dir=out_dir,
            image_transcriber=build_bedrock_image_transcriber(),
            max_validation_retries=1,
            verbose=False,
        )

    def _run_langgraph_initialization_stream(
        self,
        *,
        case_dir: Path,
        out_dir: Path,
        session_id: str,
        progress_callback: Callable[[dict[str, Any]], None],
    ) -> dict[str, Any]:
        from app.tools.vision_ocr import build_bedrock_image_transcriber
        from app.workflows.process_v1 import run_process_case_stream

        return run_process_case_stream(
            case_dir=case_dir,
            out_dir=out_dir,
            image_transcriber=build_bedrock_image_transcriber(),
            max_validation_retries=1,
            verbose=False,
            session_id=session_id,
            event_callback=progress_callback,
        )

    def _write_initialization_source_package(self, *, sources: list[dict[str, Any]], case_dir: Path) -> list[dict[str, Any]]:
        upload_dir = case_dir / "uploaded_sources"
        upload_dir.mkdir(parents=True, exist_ok=True)
        manifest: list[dict[str, Any]] = []
        for index, source in enumerate(sources, start=1):
            title = source.get("title") or source.get("original_filename") or f"source_{index}.txt"
            source_type = (source.get("source_type") or Path(title).suffix.removeprefix(".") or "txt").lower()
            filename = _safe_source_filename(title, fallback=f"source_{index}.{source_type or 'txt'}")
            if "." not in Path(filename).name:
                filename = f"{filename}.{source_type or 'txt'}"
            path = upload_dir / f"{index:02d}_{filename}"
            if source.get("kind") == "file":
                path.write_bytes(source.get("content") or b"")
                size_bytes = path.stat().st_size
            else:
                content = source.get("content", "")
                path.write_text(content, encoding="utf-8")
                size_bytes = path.stat().st_size
            manifest.append(
                {
                    "source_id": f"upload_{index:03d}",
                    "title": title,
                    "original_filename": source.get("original_filename") or title,
                    "source_type": source_type,
                    "mime_type": source.get("mime_type"),
                    "size_bytes": source.get("size_bytes") or size_bytes,
                    "storage_path": str(path),
                    "kind": source.get("kind") or "text",
                    "parser_warnings": [],
                }
            )
        (case_dir / "uploaded_source_manifest.json").write_text(
            _json_dumps({"sources": manifest}) + "\n",
            encoding="utf-8",
        )
        return manifest

    def _process_definition_from_graph_result(self, graph_result: dict[str, Any], *, out_dir: Path) -> dict[str, Any]:
        workflow_design_output = graph_result.get("workflow_design_output", {}) if isinstance(graph_result, dict) else {}
        if isinstance(workflow_design_output, dict) and workflow_design_output.get("process_definition"):
            return ProcessDefinition.model_validate(workflow_design_output["process_definition"]).model_dump(mode="json")

        candidate = graph_result.get("candidate_process")
        if isinstance(candidate, ProcessDefinition):
            return candidate.model_dump(mode="json")
        if isinstance(candidate, dict):
            return ProcessDefinition.model_validate(candidate).model_dump(mode="json")

        output_paths = graph_result.get("output_paths", {}) if isinstance(graph_result, dict) else {}
        candidate_paths = [
            output_paths.get("product_process_definition"),
            output_paths.get("process_def"),
            out_dir / "product" / "process_definition.json",
            out_dir / "product" / "workflow_design_output.json",
            out_dir / "logs" / "process_extraction_agent" / "process_def.json",
            out_dir / "process_extraction_agent" / "process_def.json",
            out_dir / "process_def.json",
        ]
        for candidate_path in candidate_paths:
            if not candidate_path:
                continue
            path = Path(candidate_path)
            if path.exists():
                payload = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(payload, dict) and payload.get("process_definition"):
                    payload = payload["process_definition"]
                return ProcessDefinition.model_validate(payload).model_dump(mode="json")

        schema_report = graph_result.get("schema_validation_report", {}) if isinstance(graph_result, dict) else {}
        business_report = graph_result.get("business_validation_result", {}) if isinstance(graph_result, dict) else {}
        detail = schema_report.get("summary") or business_report.get("summary") or "未找到 candidate_process/process_def.json"
        raise RuntimeError(f"LangGraph 未生成合法 ProcessDefinition：{detail}")

    def _initial_definition_from_sources(
        self,
        *,
        process_id: str,
        process_name: str,
        category: str,
        sources: list[dict[str, Any]],
    ) -> dict[str, Any]:
        combined = "\n".join(source["content"] for source in sources)
        if "请假" in combined or "假期" in combined or "年假" in combined or "病假" in combined:
            draft = self._initial_draft(load_process_definition(find_standard_json(LEAVE_CASE_DIR)).model_dump(mode="json"))
            draft["meta"].update(
                {
                    "process_id": process_id,
                    "process_name": process_name,
                    "version": "V0.1.0-draft",
                    "description": self._description_from_sources(
                        combined,
                        fallback="根据用户 source 初始化的员工请假审批流程草稿。",
                    ),
                }
            )
            return draft

        return {
            "meta": {
                "process_id": process_id,
                "process_name": process_name,
                "version": "V0.1.0-draft",
                "responsible_dept": "待确认",
                "description": self._description_from_sources(
                    combined,
                    fallback=f"根据用户输入初始化的{category}草稿。",
                ),
                "applicant_scope": "待确认",
                "entry_point": "OA系统",
            },
            "form_fields": [
                {
                    "seq": 1,
                    "field_name": "标题",
                    "required_stages": ["draft"],
                    "visible_stages": ["all"],
                    "editable_stages": ["draft"],
                    "component_type": "单行文本",
                    "logic_description": "根据流程名称或申请事项生成标题。",
                    "default_value": process_name,
                },
                {
                    "seq": 2,
                    "field_name": "申请人",
                    "required_stages": ["draft"],
                    "visible_stages": ["all"],
                    "editable_stages": [],
                    "component_type": "只读文本",
                    "logic_description": "系统自动带出当前登录人。",
                    "default_value": "当前登录人",
                },
                {
                    "seq": 3,
                    "field_name": "所属部门",
                    "required_stages": ["draft"],
                    "visible_stages": ["all"],
                    "editable_stages": [],
                    "component_type": "只读文本",
                    "logic_description": "系统自动带出申请人所属部门。",
                    "default_value": "当前登录人所属部门",
                },
                {
                    "seq": 4,
                    "field_name": "申请事项",
                    "required_stages": ["draft"],
                    "visible_stages": ["all"],
                    "editable_stages": ["draft"],
                    "component_type": "单行文本",
                    "logic_description": "填写本次申请的事项名称。",
                    "placeholder": "请输入申请事项",
                    "max_length": 80,
                },
                {
                    "seq": 5,
                    "field_name": "申请说明",
                    "required_stages": ["draft"],
                    "visible_stages": ["all"],
                    "editable_stages": ["draft"],
                    "component_type": "多行文本",
                    "logic_description": "说明申请背景、原因和必要性。",
                    "placeholder": "请说明申请原因和业务背景",
                    "max_length": 1000,
                },
                {
                    "seq": 6,
                    "field_name": "期望完成日期",
                    "required_stages": [],
                    "visible_stages": ["all"],
                    "editable_stages": ["draft"],
                    "component_type": "日期组件",
                    "logic_description": "如有时效要求，可填写期望完成日期。",
                },
            ],
            "flow_nodes": [
                {
                    "node_id": "draft",
                    "node_name": "起草",
                    "is_draft": True,
                    "handler": None,
                    "opinion": None,
                    "opinion_label": None,
                    "time_limit_days": None,
                    "submit_paths": [
                        {
                            "path_name": "送部门主管审批",
                            "condition": None,
                            "target_node_id": "dept_supervisor",
                        }
                    ],
                },
                {
                    "node_id": "dept_supervisor",
                    "node_name": "部门主管审批",
                    "is_draft": False,
                    "handler": {
                        "mode": "单选-单人处理",
                        "source": "本部门",
                        "role": "部门主管",
                        "source_field": None,
                    },
                    "opinion": {
                        "conclusive_required": True,
                        "detail_required": False,
                        "conclusive_options": ["同意", "不同意"],
                    },
                    "opinion_label": "部门主管意见",
                    "time_limit_days": None,
                    "submit_paths": [
                        {
                            "path_name": "流程结束",
                            "condition": "结论性意见=同意",
                            "target_node_id": "END",
                        },
                        {
                            "path_name": "退回起草",
                            "condition": "结论性意见=不同意",
                            "target_node_id": "DRAFT",
                        },
                    ],
                },
            ],
            "attachments": None,
            "roles": None,
        }

    def _validate_definition(self, definition: dict[str, Any]) -> dict[str, Any]:
        issues: list[dict[str, Any]] = []
        try:
            process = ProcessDefinition.model_validate(definition)
        except Exception as exc:  # noqa: BLE001 - return validation details to the product UI.
            return {
                "status": "BLOCKED",
                "issue_count": 1,
                "issues": [
                    {
                        "severity": "blocking",
                        "category": "schema",
                        "message": str(exc),
                    }
                ],
                "summary": "草稿结构不符合流程定义 Schema。",
            }

        node_ids = {node.node_id for node in process.flow_nodes}
        if not process.form_fields:
            issues.append({"severity": "blocking", "category": "fields", "message": "表单字段为空。"})
        if not process.flow_nodes:
            issues.append({"severity": "blocking", "category": "nodes", "message": "流程环节为空。"})

        for field in process.form_fields:
            if not field.field_name.strip():
                issues.append({"severity": "blocking", "category": "fields", "message": f"第 {field.seq} 个字段名称为空。"})

        # 处理期限是可选建议项，不是规定项——环节没配处理期限本身不算校验问题，不进
        # issue_count/待处理。真正该不该配、该配多长，交给运行数据判断（分析侧检出某环节
        # 处理时长异常时，走反哺闭环把"建议配置处理期限"作为有真实依据的效能建议提出，
        # 而不是不分青红皂白地对每个环节都要求配置）。missing_time_limit_nodes 仍在下面
        # _session_management_summary 里作为纯信息展示，只是不再算"待处理"。
        for node in process.flow_nodes:
            if not node.is_draft and node.handler is None:
                issues.append(
                    {
                        "severity": "blocking",
                        "category": "role",
                        "node_id": node.node_id,
                        "node_name": node.node_name,
                        "message": f"{node.node_name} 缺少处理角色。",
                    }
                )
            if not node.submit_paths:
                issues.append(
                    {
                        "severity": "blocking",
                        "category": "paths",
                        "node_id": node.node_id,
                        "node_name": node.node_name,
                        "message": f"{node.node_name} 没有提交路径。",
                    }
                )
            for path in node.submit_paths:
                target = path.target_node_id
                if target not in node_ids and target not in {"END", "DRAFT"}:
                    issues.append(
                        {
                            "severity": "blocking",
                            "category": "paths",
                            "node_id": node.node_id,
                            "node_name": node.node_name,
                            "message": f"{node.node_name} 的路径「{path.path_name}」指向不存在的环节 {target}。",
                        }
                    )

        has_return = any(
            path.target_node_id == "DRAFT"
            for node in process.flow_nodes
            if not node.is_draft
            for path in node.submit_paths
        )
        if not has_return:
            issues.append({"severity": "warning", "category": "paths", "message": "审批环节缺少退回起草路径。"})

        blocking = any(issue["severity"] == "blocking" for issue in issues)
        return {
            "status": "BLOCKED" if blocking else "WARN" if issues else "PASS",
            "issue_count": len(issues),
            "blocking_count": sum(1 for issue in issues if issue["severity"] == "blocking"),
            "warning_count": sum(1 for issue in issues if issue["severity"] == "warning"),
            "issues": issues,
            "summary": self._validation_summary(issues),
        }

    def _generation_report(self, definition: dict[str, Any], validation_report: dict[str, Any]) -> dict[str, Any]:
        process = ProcessDefinition.model_validate(definition)
        if "请假" in process.meta.process_name:
            clarifications = [
                "处理期限目前仍有缺失，建议为审批环节配置处理时限。",
                "请确认 3 天以上请假是否仍进入部门总经理审批。",
            ]
        else:
            clarifications = [
                "处理期限目前仍有缺失，建议为审批环节配置处理时限。",
                "请确认是否需要按金额、风险等级或事项类型增加条件分支。",
                "请确认是否需要附件材料或职能部门复核。",
            ]
        if validation_report.get("warning_count") == 0:
            clarifications = ["草稿已补齐当前校验项，可继续进入模拟测试和发布申请。"]
        return {
            "summary": (
                f"已生成{process.meta.process_name} {process.meta.version} 草稿："
                f"{len(process.form_fields)} 个字段、{len(process.flow_nodes)} 个环节、"
                f"{sum(len(node.submit_paths) for node in process.flow_nodes)} 条路径。"
            ),
            "clarifications": clarifications,
            "provider": "fake",
        }

    def _initialization_message(
        self,
        definition: dict[str, Any],
        validation_report: dict[str, Any],
        source_count: int,
    ) -> str:
        process = ProcessDefinition.model_validate(definition)
        return (
            f"已根据 {source_count} 份输入材料初始化「{process.meta.process_name}」草稿："
            f"{len(process.form_fields)} 个字段、{len(process.flow_nodes)} 个环节、"
            f"{sum(len(node.submit_paths) for node in process.flow_nodes)} 条路径。"
            f" 当前发现 {validation_report.get('issue_count', 0)} 个待确认项，建议先检查处理期限、附件材料、审批角色和退回路径。"
        )

    def _description_from_sources(self, text: str, *, fallback: str) -> str:
        compact = " ".join(text.split())
        if not compact:
            return fallback
        return compact[:120] + ("..." if len(compact) > 120 else "")

    def _validation_summary(self, issues: list[dict[str, Any]]) -> str:
        if not issues:
            return "草稿校验通过。"
        blocking = sum(1 for issue in issues if issue["severity"] == "blocking")
        warning = sum(1 for issue in issues if issue["severity"] == "warning")
        return f"发现 {blocking} 个阻断项、{warning} 个提示项。"

    def _validation_message(self, report: dict[str, Any]) -> str:
        if report.get("issue_count", 0) == 0:
            return "草稿校验通过。"
        return f"草稿校验完成：{report.get('summary', '存在待确认问题')} 请在右侧报告和配置页逐项确认。"

    def _source_context(self, session_id: str) -> str:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT title, content FROM workflow_design_sources WHERE session_id = ? ORDER BY created_at ASC, title ASC",
                (session_id,),
            ).fetchall()
        return "\n\n".join(f"# {row['title']}\n{row['content']}" for row in rows)

    def _message_context(self, session_id: str) -> str:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT role, content FROM workflow_design_messages WHERE session_id = ? ORDER BY created_at ASC, id ASC",
                (session_id,),
            ).fetchall()
        return "\n".join(f"{row['role']}: {row['content']}" for row in rows)

    def _seed_raw_sources(self, conn: sqlite3.Connection, *, session_id: str, created_by: str, created_at: str) -> None:
        if not RAW_SOURCE_DIR.exists():
            return
        for path in sorted(RAW_SOURCE_DIR.glob("*.txt")):
            content = path.read_text(encoding="utf-8")
            conn.execute(
                """
                INSERT INTO workflow_design_sources (
                    id, session_id, title, source_type, content, status, summary,
                    created_by, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    f"wsrc_{uuid4().hex[:12]}",
                    session_id,
                    path.name,
                    "txt",
                    content,
                    "已解析",
                    self._summarize_source(content),
                    created_by,
                    created_at,
                ),
            )

    def _mark_sources_parsed(self, conn: sqlite3.Connection, *, session_id: str) -> None:
        conn.execute(
            "UPDATE workflow_design_sources SET status = '已解析' WHERE session_id = ? AND status != '已解析'",
            (session_id,),
        )

    def _append_message(
        self,
        conn: sqlite3.Connection,
        *,
        session_id: str,
        role: str,
        content: str,
        payload: dict[str, Any],
        created_at: str,
    ) -> str:
        message_id = f"wmsg_{uuid4().hex[:12]}"
        conn.execute(
            """
            INSERT INTO workflow_design_messages (id, session_id, role, content, payload_json, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (message_id, session_id, role, content, _json_dumps(payload), created_at),
        )
        return message_id

    def _resolve_pending_message(self, conn: sqlite3.Connection, message_id: str | None, *, resolution: str) -> None:
        """把待确认消息标记为已解决（confirmed/discarded），供前端隐藏确认/放弃按钮。"""
        if not message_id:
            return
        row = conn.execute(
            "SELECT payload_json FROM workflow_design_messages WHERE id = ?", (message_id,)
        ).fetchone()
        if row is None:
            return
        payload = _json_loads(row["payload_json"], {})
        payload["requires_confirmation"] = False
        payload["resolution"] = resolution
        conn.execute(
            "UPDATE workflow_design_messages SET payload_json = ? WHERE id = ?",
            (_json_dumps(payload), message_id),
        )

    def _source_payload(self, row: dict[str, Any]) -> dict[str, Any]:
        content = row["content"]
        if isinstance(content, (bytes, bytearray, memoryview)):
            content = ""
        return {
            "source_id": row["id"],
            "title": row["title"],
            "source_type": row["source_type"],
            "content": content,
            "content_preview": "",
            "status": row["status"],
            "summary": row["summary"],
            "original_filename": row.get("original_filename"),
            "mime_type": row.get("mime_type"),
            "size_bytes": row.get("size_bytes"),
            "storage_path": row.get("storage_path"),
            "parser_warnings": _json_loads(row.get("parser_warnings_json"), []),
            "created_by": row["created_by"],
            "created_at": row["created_at"],
        }

    def _persisted_source_content(self, source: dict[str, Any]) -> str:
        if source.get("kind") == "file":
            return ""
        content = source.get("content") or ""
        if isinstance(content, (bytes, bytearray, memoryview)):
            return ""
        return str(content)

    def _message_payload(self, row: dict[str, Any]) -> dict[str, Any]:
        return {
            "message_id": row["id"],
            "role": row["role"],
            "content": row["content"],
            "payload": _json_loads(row["payload_json"], {}),
            "created_at": row["created_at"],
        }

    def _summarize_source(self, content: str) -> str:
        compact = " ".join(content.split())
        return compact[:90] + ("..." if len(compact) > 90 else "")

    def _session_row(self, session_id: str) -> dict[str, Any]:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM workflow_design_sessions WHERE id = ?", (session_id,)).fetchone()
        if row is None:
            raise KeyError(f"workflow design session {session_id} not found")
        return dict(row)

    def _workflow_row(self, workflow_definition_id: str) -> dict[str, Any]:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM workflow_definitions WHERE id = ?", (workflow_definition_id,)).fetchone()
        if row is None:
            raise KeyError(f"workflow definition {workflow_definition_id} not found")
        return dict(row)

    def _require_user(self, user_id: str) -> None:
        with self._connect() as conn:
            row = conn.execute("SELECT id FROM users WHERE id = ?", (user_id,)).fetchone()
        if row is None:
            raise KeyError(f"user {user_id} not found")

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn


def _safe_source_filename(value: str, *, fallback: str) -> str:
    cleaned = re.sub(r"[^\w\u4e00-\u9fff.-]+", "_", value.strip())
    cleaned = cleaned.strip("._")
    return cleaned[:80] or fallback


def _json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False)


def _json_loads(value: str | None, default: Any | None = None) -> Any:
    if value is None or value == "":
        return default
    return json.loads(value)
