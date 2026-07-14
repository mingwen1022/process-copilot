from __future__ import annotations

import json
import sqlite3
from datetime import date
from pathlib import Path
from typing import Any
from uuid import uuid4

from app.io_utils import find_standard_json, load_process_definition
from app.org_knowledge import get_user_assignments, load_org_seed, resolve_role
from app.runtime.condition_eval import evaluate_condition
from app.runtime.form_renderer import render_form_schema
from app.runtime.models import now_iso
from data.schema import FlowNode, ProcessDefinition, SubmitPath


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_SLICE1_DB_PATH = PROJECT_ROOT / "data/runtime/runtime.db"
LEAVE_CASE_DIR = PROJECT_ROOT / "data/cases/leave_request"
LEAVE_WORKFLOW_DEFINITION_ID = "wfd_leave_request_v1"
EOA140_CASE_DIR = PROJECT_ROOT / "data/cases/EOA140_subsidiary_major_matter"
EOA140_WORKFLOW_DEFINITION_ID = "wfd_eoa140_v1"
EXPENSE_CASE_DIR = PROJECT_ROOT / "data/cases/expense_reimbursement"
EXPENSE_WORKFLOW_DEFINITION_ID = "wfd_expense_v1"
PROCUREMENT_CASE_DIR = PROJECT_ROOT / "data/cases/procurement_request"
PROCUREMENT_WORKFLOW_DEFINITION_ID = "wfd_procurement_v1"
SEAL_CASE_DIR = PROJECT_ROOT / "data/cases/seal_request"
SEAL_WORKFLOW_DEFINITION_ID = "wfd_seal_v1"
SLICE1_USERS = ("u_it_app_staff", "u_it_app_supervisor", "u_it_line_leader")
SLICE1_PASSWORD = "123456"

# 请假发布版 V1.0 相对金标准故意留的那个缺口：dept_gm「流程结束」的天数上限
_LEAVE_PUBLISHED_GAP_NODE = "dept_gm"
_LEAVE_PUBLISHED_GAP_PATH = "流程结束"
_LEAVE_GOLD_DAY_LIMIT = "≤7天"   # 金标准（修好后）：4~7 天走直接结束
_LEAVE_PUBLISHED_DAY_LIMIT = "≤3天"  # 发布 V1.0（有缺口）：只到 3 天，4~7 天无出口


def _inject_leave_published_gap(process: ProcessDefinition) -> ProcessDefinition:
    """把金标准派生的请假定义降级成"发布的 V1.0"——只动一处：dept_gm「流程结束」路径
    的天数上限 ≤7天 → ≤3天，制造 4~7 天普通假两头不沾的覆盖漏洞（与「送条线分管领导」的
    >7天 之间空出 3<天数≤7 这一档）。跟运维排障 mock 的缺口语义一致，也正是反哺闭环里
    设计副驾要修复的那个。找不到目标路径/阈值时原样返回，不硬塞（金标准哪天改了措辞也不崩）。"""
    process = process.model_copy(deep=True)
    node = next((n for n in process.flow_nodes if n.node_id == _LEAVE_PUBLISHED_GAP_NODE), None)
    if node is None:
        return process
    path = next((p for p in node.submit_paths if p.path_name == _LEAVE_PUBLISHED_GAP_PATH), None)
    if path is None or not path.condition or _LEAVE_GOLD_DAY_LIMIT not in path.condition:
        return process
    path.condition = path.condition.replace(_LEAVE_GOLD_DAY_LIMIT, _LEAVE_PUBLISHED_DAY_LIMIT)
    return process


class Slice1Service:
    """Slice 1 runtime for the employee leave approval closed loop."""

    def __init__(self, db_path: str | Path = DEFAULT_SLICE1_DB_PATH):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.org_data = load_org_seed()
        self.init_schema()
        self.ensure_seed_data()

    def init_schema(self) -> None:
        with self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS users (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    employee_no TEXT,
                    status TEXT NOT NULL,
                    email TEXT,
                    mobile TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS org_units (
                    id TEXT PRIMARY KEY,
                    parent_id TEXT,
                    name TEXT NOT NULL,
                    type TEXT NOT NULL,
                    sort_order INTEGER NOT NULL DEFAULT 0,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS user_positions (
                    id TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL,
                    org_unit_id TEXT NOT NULL,
                    position_code TEXT NOT NULL,
                    position_name TEXT NOT NULL,
                    is_primary INTEGER NOT NULL,
                    is_manager INTEGER NOT NULL,
                    role_tags_json TEXT NOT NULL,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY(user_id) REFERENCES users(id),
                    FOREIGN KEY(org_unit_id) REFERENCES org_units(id)
                );

                CREATE TABLE IF NOT EXISTS workflow_definitions (
                    id TEXT PRIMARY KEY,
                    workflow_id TEXT NOT NULL,
                    code TEXT NOT NULL,
                    name TEXT NOT NULL,
                    category TEXT NOT NULL,
                    owner_dept_id TEXT,
                    owner_dept_name TEXT,
                    version TEXT NOT NULL,
                    status TEXT NOT NULL,
                    description TEXT,
                    launch_scope_json TEXT NOT NULL,
                    definition_json TEXT NOT NULL,
                    created_by TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    published_at TEXT
                );

                CREATE TABLE IF NOT EXISTS workflow_cases (
                    id TEXT PRIMARY KEY,
                    case_no TEXT NOT NULL UNIQUE,
                    workflow_definition_id TEXT NOT NULL,
                    workflow_id TEXT NOT NULL,
                    workflow_version TEXT NOT NULL,
                    title TEXT NOT NULL,
                    status TEXT NOT NULL,
                    initiator_id TEXT NOT NULL,
                    initiator_name TEXT NOT NULL,
                    current_node_id TEXT NOT NULL,
                    current_node_name TEXT NOT NULL,
                    current_assignee_ids_json TEXT NOT NULL,
                    form_values_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    completed_at TEXT,
                    FOREIGN KEY(workflow_definition_id) REFERENCES workflow_definitions(id)
                );

                CREATE TABLE IF NOT EXISTS work_items (
                    id TEXT PRIMARY KEY,
                    case_id TEXT NOT NULL,
                    workflow_definition_id TEXT NOT NULL,
                    node_id TEXT NOT NULL,
                    node_name TEXT NOT NULL,
                    type TEXT NOT NULL,
                    status TEXT NOT NULL,
                    case_initiator_id TEXT NOT NULL,
                    case_initiator_name TEXT NOT NULL,
                    current_handler_id TEXT NOT NULL,
                    current_handler_name TEXT NOT NULL,
                    arrived_at TEXT NOT NULL,
                    due_at TEXT,
                    completed_at TEXT,
                    priority TEXT,
                    risk_level TEXT,
                    summary TEXT,
                    submitted_by_id TEXT,
                    submitted_by_name TEXT,
                    decision TEXT,
                    selected_edge_id TEXT,
                    selected_edge_label TEXT,
                    target_node_id TEXT,
                    target_node_name TEXT,
                    next_handler_ids_json TEXT NOT NULL,
                    next_handler_names_json TEXT NOT NULL,
                    comment TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY(case_id) REFERENCES workflow_cases(id),
                    FOREIGN KEY(workflow_definition_id) REFERENCES workflow_definitions(id)
                );

                CREATE TABLE IF NOT EXISTS timeline_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    case_id TEXT NOT NULL,
                    work_item_id TEXT,
                    type TEXT NOT NULL,
                    node_id TEXT,
                    node_name TEXT,
                    actor_id TEXT,
                    actor_name TEXT,
                    decision TEXT,
                    comment TEXT,
                    from_node_id TEXT,
                    to_node_id TEXT,
                    message TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(case_id) REFERENCES workflow_cases(id)
                );

                CREATE TABLE IF NOT EXISTS attachment_metas (
                    id TEXT PRIMARY KEY,
                    case_id TEXT NOT NULL,
                    field_id TEXT,
                    filename TEXT NOT NULL,
                    size INTEGER,
                    mime_type TEXT,
                    storage_path TEXT,
                    status TEXT NOT NULL,
                    uploaded_by TEXT,
                    uploaded_at TEXT NOT NULL,
                    FOREIGN KEY(case_id) REFERENCES workflow_cases(id)
                );
                """
            )

    def ensure_seed_data(self) -> None:
        now = now_iso()
        positions = {item["position_code"]: item["position_name"] for item in self.org_data.get("positions", [])}
        departments = {item["dept_id"]: item for item in self.org_data.get("departments", [])}

        with self._connect() as conn:
            for line_index, line in enumerate(self.org_data.get("lines", []), start=1):
                conn.execute(
                    """
                    INSERT INTO org_units (id, parent_id, name, type, sort_order, status, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(id) DO UPDATE SET
                        parent_id=excluded.parent_id,
                        name=excluded.name,
                        type=excluded.type,
                        sort_order=excluded.sort_order,
                        status=excluded.status,
                        updated_at=excluded.updated_at
                    """,
                    (line["line_id"], None, line["line_name"], "line", line_index, "active", now, now),
                )

            for dept_index, dept in enumerate(self.org_data.get("departments", []), start=1):
                parent_id = dept.get("parent_dept_id") or dept.get("line_id")
                conn.execute(
                    """
                    INSERT INTO org_units (id, parent_id, name, type, sort_order, status, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(id) DO UPDATE SET
                        parent_id=excluded.parent_id,
                        name=excluded.name,
                        type=excluded.type,
                        sort_order=excluded.sort_order,
                        status=excluded.status,
                        updated_at=excluded.updated_at
                    """,
                    (
                        dept["dept_id"],
                        parent_id,
                        dept["dept_name"],
                        dept.get("dept_type") or "department",
                        dept_index,
                        "active",
                        now,
                        now,
                    ),
                )

            for user in self.org_data.get("users", []):
                conn.execute(
                    """
                    INSERT INTO users (id, name, employee_no, status, email, mobile, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(id) DO UPDATE SET
                        name=excluded.name,
                        employee_no=excluded.employee_no,
                        status=excluded.status,
                        email=excluded.email,
                        mobile=excluded.mobile,
                        updated_at=excluded.updated_at
                    """,
                    (
                        user["user_id"],
                        user["name"],
                        user.get("employee_no"),
                        user.get("status", "active"),
                        user.get("email"),
                        user.get("mobile"),
                        now,
                        now,
                    ),
                )

            for assignment in self.org_data.get("position_assignments", []):
                assignment_id = f"{assignment['user_id']}::{assignment['dept_id']}::{assignment['position_code']}"
                role_tags = []
                if assignment.get("is_primary"):
                    role_tags.append("主岗")
                if assignment.get("assignment_type") == "concurrent":
                    role_tags.append("兼岗")
                if departments.get(assignment["dept_id"], {}).get("manager_user_id") == assignment["user_id"]:
                    role_tags.append("负责人")
                conn.execute(
                    """
                    INSERT INTO user_positions (
                        id, user_id, org_unit_id, position_code, position_name, is_primary,
                        is_manager, role_tags_json, status, created_at, updated_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(id) DO UPDATE SET
                        org_unit_id=excluded.org_unit_id,
                        position_code=excluded.position_code,
                        position_name=excluded.position_name,
                        is_primary=excluded.is_primary,
                        is_manager=excluded.is_manager,
                        role_tags_json=excluded.role_tags_json,
                        status=excluded.status,
                        updated_at=excluded.updated_at
                    """,
                    (
                        assignment_id,
                        assignment["user_id"],
                        assignment["dept_id"],
                        assignment["position_code"],
                        assignment.get("title") or positions.get(assignment["position_code"], assignment["position_code"]),
                        1 if assignment.get("is_primary") else 0,
                        1 if "负责人" in role_tags else 0,
                        _json_dumps(role_tags),
                        "active",
                        now,
                        now,
                    ),
                )

            process = self._read_leave_process()
            conn.execute(
                """
                INSERT INTO workflow_definitions (
                    id, workflow_id, code, name, category, owner_dept_id, owner_dept_name,
                    version, status, description, launch_scope_json, definition_json,
                    created_by, created_at, updated_at, published_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    workflow_id=excluded.workflow_id,
                    code=excluded.code,
                    name=excluded.name,
                    category=excluded.category,
                    owner_dept_id=excluded.owner_dept_id,
                    owner_dept_name=excluded.owner_dept_name,
                    version=excluded.version,
                    status=excluded.status,
                    description=excluded.description,
                    launch_scope_json=excluded.launch_scope_json,
                    definition_json=excluded.definition_json,
                    updated_at=excluded.updated_at,
                    published_at=excluded.published_at
                """,
                (
                    LEAVE_WORKFLOW_DEFINITION_ID,
                    process.meta.process_id,
                    "leave_request",
                    process.meta.process_name,
                    "人事行政",
                    "hr_dept",
                    process.meta.responsible_dept,
                    process.meta.version,
                    "PUBLISHED",
                    process.meta.description,
                    _json_dumps({"type": "all_active_users", "slice": "leave_demo"}),
                    _json_dumps(process.model_dump(mode="json")),
                    "system_seed",
                    now,
                    now,
                    now,
                ),
            )

            eoa140 = self._read_eoa140_process()
            conn.execute(
                """
                INSERT INTO workflow_definitions (
                    id, workflow_id, code, name, category, owner_dept_id, owner_dept_name,
                    version, status, description, launch_scope_json, definition_json,
                    created_by, created_at, updated_at, published_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    workflow_id=excluded.workflow_id,
                    code=excluded.code,
                    name=excluded.name,
                    category=excluded.category,
                    owner_dept_id=excluded.owner_dept_id,
                    owner_dept_name=excluded.owner_dept_name,
                    version=excluded.version,
                    status=excluded.status,
                    description=excluded.description,
                    launch_scope_json=excluded.launch_scope_json,
                    definition_json=excluded.definition_json,
                    updated_at=excluded.updated_at,
                    published_at=excluded.published_at
                """,
                (
                    EOA140_WORKFLOW_DEFINITION_ID,
                    eoa140.meta.process_id,
                    "subsidiary_major_matter",
                    eoa140.meta.process_name,
                    "公司治理",
                    None,  # 战略发展部在当前 org_seed（单一证券公司架构）中未建模，无对应 dept_id
                    eoa140.meta.responsible_dept,
                    eoa140.meta.version,
                    "PUBLISHED",
                    eoa140.meta.description,
                    _json_dumps({"type": "all_active_users", "slice": "eoa140_demo"}),
                    _json_dumps(eoa140.model_dump(mode="json")),
                    "system_seed",
                    now,
                    now,
                    now,
                ),
            )

            expense = self._read_expense_process()
            conn.execute(
                """
                INSERT INTO workflow_definitions (
                    id, workflow_id, code, name, category, owner_dept_id, owner_dept_name,
                    version, status, description, launch_scope_json, definition_json,
                    created_by, created_at, updated_at, published_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    workflow_id=excluded.workflow_id,
                    code=excluded.code,
                    name=excluded.name,
                    category=excluded.category,
                    owner_dept_id=excluded.owner_dept_id,
                    owner_dept_name=excluded.owner_dept_name,
                    version=excluded.version,
                    status=excluded.status,
                    description=excluded.description,
                    launch_scope_json=excluded.launch_scope_json,
                    definition_json=excluded.definition_json,
                    updated_at=excluded.updated_at,
                    published_at=excluded.published_at
                """,
                (
                    EXPENSE_WORKFLOW_DEFINITION_ID,
                    expense.meta.process_id,
                    "expense_reimbursement",
                    expense.meta.process_name,
                    "财务费用",
                    "finance_dept",
                    expense.meta.responsible_dept,
                    expense.meta.version,
                    "PUBLISHED",
                    expense.meta.description,
                    _json_dumps({"type": "all_active_users", "slice": "expense_demo"}),
                    _json_dumps(expense.model_dump(mode="json")),
                    "system_seed",
                    now,
                    now,
                    now,
                ),
            )

            # P4 轻量目录级流程：草稿态，仅用于流程管理/发起页广度展示，不做运行实例/分析/eval
            procurement = self._read_procurement_process()
            conn.execute(
                """
                INSERT INTO workflow_definitions (
                    id, workflow_id, code, name, category, owner_dept_id, owner_dept_name,
                    version, status, description, launch_scope_json, definition_json,
                    created_by, created_at, updated_at, published_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    workflow_id=excluded.workflow_id,
                    code=excluded.code,
                    name=excluded.name,
                    category=excluded.category,
                    owner_dept_id=excluded.owner_dept_id,
                    owner_dept_name=excluded.owner_dept_name,
                    version=excluded.version,
                    status=excluded.status,
                    description=excluded.description,
                    launch_scope_json=excluded.launch_scope_json,
                    definition_json=excluded.definition_json,
                    updated_at=excluded.updated_at
                """,
                (
                    PROCUREMENT_WORKFLOW_DEFINITION_ID,
                    procurement.meta.process_id,
                    "procurement_request",
                    procurement.meta.process_name,
                    "采购合同",
                    "procurement_center",
                    procurement.meta.responsible_dept,
                    procurement.meta.version,
                    "DRAFT",
                    procurement.meta.description,
                    _json_dumps({"type": "all_active_users", "slice": "procurement_demo"}),
                    _json_dumps(procurement.model_dump(mode="json")),
                    "system_seed",
                    now,
                    now,
                    None,
                ),
            )

            seal = self._read_seal_process()
            conn.execute(
                """
                INSERT INTO workflow_definitions (
                    id, workflow_id, code, name, category, owner_dept_id, owner_dept_name,
                    version, status, description, launch_scope_json, definition_json,
                    created_by, created_at, updated_at, published_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    workflow_id=excluded.workflow_id,
                    code=excluded.code,
                    name=excluded.name,
                    category=excluded.category,
                    owner_dept_id=excluded.owner_dept_id,
                    owner_dept_name=excluded.owner_dept_name,
                    version=excluded.version,
                    status=excluded.status,
                    description=excluded.description,
                    launch_scope_json=excluded.launch_scope_json,
                    definition_json=excluded.definition_json,
                    updated_at=excluded.updated_at
                """,
                (
                    SEAL_WORKFLOW_DEFINITION_ID,
                    seal.meta.process_id,
                    "seal_request",
                    seal.meta.process_name,
                    "行政",
                    "admin_office",
                    seal.meta.responsible_dept,
                    seal.meta.version,
                    "DRAFT",
                    seal.meta.description,
                    _json_dumps({"type": "all_active_users", "slice": "seal_demo"}),
                    _json_dumps(seal.model_dump(mode="json")),
                    "system_seed",
                    now,
                    now,
                    None,
                ),
            )

    def list_users(self) -> list[dict[str, Any]]:
        return [self.user_payload(user_id) for user_id in SLICE1_USERS]

    def create_mock_session(self, user_id: str) -> dict[str, Any]:
        user = self.user_payload(user_id)
        return {"session_id": f"mock_{user_id}", "user": user}

    def login_session(self, *, account: str, password: str) -> dict[str, Any]:
        normalized = account.strip()
        if password != SLICE1_PASSWORD:
            raise RuntimeError("账号或密码错误")
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT id FROM users
                WHERE id IN (?, ?, ?) AND (employee_no = ? OR id = ?)
                """,
                (*SLICE1_USERS, normalized, normalized),
            ).fetchone()
        if row is None:
            raise RuntimeError("账号或密码错误")
        user = self.user_payload(row["id"])
        return {"session_id": f"mock_{user['user_id']}", "user": user}

    def workbench_summary(self, user_id: str) -> dict[str, Any]:
        self._require_user(user_id)
        with self._connect() as conn:
            todo = conn.execute(
                """
                SELECT COUNT(*) AS value FROM work_items
                WHERE current_handler_id = ? AND status = 'OPEN' AND type = 'APPROVAL'
                """,
                (user_id,),
            ).fetchone()["value"]
            drafts = conn.execute(
                """
                SELECT COUNT(*) AS value FROM work_items
                WHERE current_handler_id = ? AND status = 'OPEN' AND type = 'DRAFT'
                """,
                (user_id,),
            ).fetchone()["value"]
            done = conn.execute(
                """
                SELECT COUNT(*) AS value
                FROM (
                    SELECT wi.case_id
                    FROM work_items wi
                    WHERE wi.current_handler_id = ?
                      AND wi.status IN ('COMPLETED', 'SKIPPED')
                      AND NOT EXISTS (
                          SELECT 1
                          FROM work_items open_wi
                          WHERE open_wi.case_id = wi.case_id
                            AND open_wi.current_handler_id = ?
                            AND open_wi.status = 'OPEN'
                      )
                    GROUP BY wi.case_id
                )
                """,
                (user_id, user_id),
            ).fetchone()["value"]
            initiated = conn.execute(
                "SELECT COUNT(*) AS value FROM workflow_cases WHERE initiator_id = ?",
                (user_id,),
            ).fetchone()["value"]
        return {
            "user": self.user_payload(user_id),
            "counts": {"todo": todo, "drafts": drafts, "done": done, "initiated": initiated},
        }

    def workbench_items(self, *, user_id: str, bucket: str) -> list[dict[str, Any]]:
        self._require_user(user_id)
        if bucket not in {"todo", "drafts", "done"}:
            raise RuntimeError(f"unsupported bucket: {bucket}")
        if bucket == "done":
            return self._done_workbench_items(user_id)
        where = {
            "todo": "wi.current_handler_id = ? AND wi.status = 'OPEN' AND wi.type = 'APPROVAL'",
            "drafts": "wi.current_handler_id = ? AND wi.status = 'OPEN' AND wi.type = 'DRAFT'",
        }[bucket]
        with self._connect() as conn:
            rows = conn.execute(
                f"""
                SELECT wi.*, wc.case_no, wc.title AS case_title, wc.status AS case_status,
                       wc.initiator_id, wc.initiator_name, wc.updated_at AS case_updated_at,
                       wd.name AS workflow_name
                FROM work_items wi
                JOIN workflow_cases wc ON wc.id = wi.case_id
                JOIN workflow_definitions wd ON wd.id = wi.workflow_definition_id
                WHERE {where}
                ORDER BY wi.updated_at DESC
                """,
                (user_id,),
            ).fetchall()
        return [self._work_item_card(dict(row), bucket=bucket) for row in rows]

    def _done_workbench_items(self, user_id: str) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                WITH user_done AS (
                    SELECT
                        wi.*,
                        COUNT(*) OVER (PARTITION BY wi.case_id) AS handled_count,
                        ROW_NUMBER() OVER (
                            PARTITION BY wi.case_id
                            ORDER BY COALESCE(wi.completed_at, wi.updated_at) DESC, wi.id DESC
                        ) AS row_no
                    FROM work_items wi
                    WHERE wi.current_handler_id = ?
                      AND wi.status IN ('COMPLETED', 'SKIPPED')
                      AND NOT EXISTS (
                          SELECT 1
                          FROM work_items open_wi
                          WHERE open_wi.case_id = wi.case_id
                            AND open_wi.current_handler_id = ?
                            AND open_wi.status = 'OPEN'
                      )
                )
                SELECT user_done.*, wc.case_no, wc.title AS case_title, wc.status AS case_status,
                       wc.initiator_id, wc.initiator_name, wc.current_node_id AS case_current_node_id,
                       wc.current_node_name AS case_current_node_name, wc.updated_at AS case_updated_at,
                       wd.name AS workflow_name
                FROM user_done
                JOIN workflow_cases wc ON wc.id = user_done.case_id
                JOIN workflow_definitions wd ON wd.id = user_done.workflow_definition_id
                WHERE user_done.row_no = 1
                ORDER BY COALESCE(user_done.completed_at, user_done.updated_at) DESC
                """,
                (user_id, user_id),
            ).fetchall()
        return [self._work_item_card(dict(row), bucket="done") for row in rows]

    def initiated_cases(self, user_id: str) -> list[dict[str, Any]]:
        self._require_user(user_id)
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT wc.*, wd.name AS workflow_name
                FROM workflow_cases wc
                JOIN workflow_definitions wd ON wd.id = wc.workflow_definition_id
                WHERE wc.initiator_id = ?
                ORDER BY wc.updated_at DESC
                """,
                (user_id,),
            ).fetchall()
        return [self._case_card(dict(row)) for row in rows]

    def workflow_catalog(self, user_id: str | None = None) -> list[dict[str, Any]]:
        if user_id:
            self._require_user(user_id)
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM workflow_definitions
                WHERE status = 'PUBLISHED'
                ORDER BY updated_at DESC
                """,
            ).fetchall()
        return [self._workflow_payload(dict(row), include_definition=False) for row in rows]

    def workflow_definitions(self, user_id: str | None = None) -> list[dict[str, Any]]:
        if user_id:
            self._require_user(user_id)
        with self._connect() as conn:
            params: tuple[str, ...]
            if user_id:
                where_clause = "status = 'PUBLISHED' OR (status = 'DRAFT' AND created_by = ?)"
                params = (user_id,)
            else:
                where_clause = "status = 'PUBLISHED' OR status = 'DRAFT'"
                params = ()
            rows = conn.execute(
                f"""
                SELECT * FROM workflow_definitions
                WHERE {where_clause}
                ORDER BY updated_at DESC
                """,
                params,
            ).fetchall()
        return [self._workflow_management_payload(dict(row), include_definition=False) for row in rows]

    def workflow_definition_detail(self, workflow_definition_id: str, user_id: str | None = None) -> dict[str, Any]:
        if user_id:
            self._require_user(user_id)
        workflow = self.workflow_definition(workflow_definition_id)
        is_owned_draft = workflow["status"] == "DRAFT" and (not user_id or workflow["created_by"] == user_id)
        if workflow["status"] != "PUBLISHED" and not is_owned_draft:
            raise KeyError(f"workflow definition {workflow_definition_id} not found")
        return self._workflow_management_payload(workflow, include_definition=True)

    def launch_form(self, workflow_definition_id: str, user_id: str | None = None) -> dict[str, Any]:
        workflow = self.workflow_definition(workflow_definition_id)
        process = ProcessDefinition.model_validate(_json_loads(workflow["definition_json"]))
        return {
            "workflow": self._workflow_payload(workflow, include_definition=False),
            "form_schema": render_form_schema(process, node_id="draft"),
            "initial_values": self.initial_form_values(process, user_id=user_id),
        }

    def create_case(
        self,
        *,
        workflow_definition_id: str,
        initiator_user_id: str,
        form_values: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        initiator = self.user_payload(initiator_user_id)
        workflow = self.workflow_definition(workflow_definition_id)
        process = ProcessDefinition.model_validate(_json_loads(workflow["definition_json"]))
        draft_node = self._node(process, "draft")
        values = self.initial_form_values(process, user_id=initiator_user_id)
        values.update(form_values or {})
        values = self._normalize_form_values(values, initiator_user_id=initiator_user_id)
        case_id = f"case_{uuid4().hex[:12]}"
        case_no = self._next_case_no()
        now = now_iso()
        title = self._case_title(values, initiator["name"])

        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO workflow_cases (
                    id, case_no, workflow_definition_id, workflow_id, workflow_version,
                    title, status, initiator_id, initiator_name, current_node_id,
                    current_node_name, current_assignee_ids_json, form_values_json,
                    created_at, updated_at, completed_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    case_id,
                    case_no,
                    workflow_definition_id,
                    workflow["workflow_id"],
                    workflow["version"],
                    title,
                    "DRAFT",
                    initiator_user_id,
                    initiator["name"],
                    "draft",
                    draft_node.node_name,
                    _json_dumps([initiator_user_id]),
                    _json_dumps(values),
                    now,
                    now,
                    None,
                ),
            )
            work_item_id = self._create_work_item(
                conn,
                case_id=case_id,
                workflow_definition_id=workflow_definition_id,
                node=draft_node,
                item_type="DRAFT",
                case_initiator_id=initiator_user_id,
                case_initiator_name=initiator["name"],
                handler_id=initiator_user_id,
                handler_name=initiator["name"],
                summary=title,
                now=now,
            )
            self._append_event(
                conn,
                case_id=case_id,
                work_item_id=work_item_id,
                event_type="CASE_CREATED",
                node_id="draft",
                node_name=draft_node.node_name,
                actor_id=initiator_user_id,
                actor_name=initiator["name"],
                message=f"{initiator['name']} 创建请假申请草稿",
                payload={"case_no": case_no},
                created_at=now,
            )
            self._append_event(
                conn,
                case_id=case_id,
                work_item_id=work_item_id,
                event_type="WORK_ITEM_CREATED",
                node_id="draft",
                node_name=draft_node.node_name,
                actor_id=None,
                actor_name=None,
                message=f"起草任务分配给 {initiator['name']}",
                payload={"assignee_id": initiator_user_id},
                created_at=now,
            )
        return self.case_detail(case_id)

    def case_detail(self, case_id: str, user_id: str | None = None) -> dict[str, Any]:
        case = self._case_row(case_id)
        workflow = self.workflow_definition(case["workflow_definition_id"])
        process = ProcessDefinition.model_validate(_json_loads(workflow["definition_json"]))
        form_values = _json_loads(case["form_values_json"], {})
        current_node_id = "draft" if case["current_node_id"] in {"DRAFT", "RETURNED"} else case["current_node_id"]
        form_schema = render_form_schema(process, node_id=current_node_id if current_node_id != "END" else "draft")
        with self._connect() as conn:
            work_items = [self._work_item_payload(dict(row)) for row in conn.execute(
                "SELECT * FROM work_items WHERE case_id = ? ORDER BY created_at ASC, id ASC",
                (case_id,),
            ).fetchall()]
            timeline = [self._event_payload(dict(row)) for row in conn.execute(
                "SELECT * FROM timeline_events WHERE case_id = ? ORDER BY id ASC",
                (case_id,),
            ).fetchall()]
        active_work_item = None
        if user_id:
            active_work_item = next(
                (item for item in work_items if item["status"] == "OPEN" and item["current_handler_id"] == user_id),
                None,
            )
        return {
            "case": self._case_payload(case),
            "workflow": self._workflow_payload(workflow, include_definition=False),
            "definition": process.model_dump(mode="json"),
            "form_schema": form_schema,
            "form_values": form_values,
            "work_items": work_items,
            "open_work_items": [item for item in work_items if item["status"] == "OPEN"],
            "active_work_item": active_work_item,
            "timeline": timeline,
        }

    def update_case_form(self, *, case_id: str, actor_user_id: str, form_values: dict[str, Any]) -> dict[str, Any]:
        actor = self.user_payload(actor_user_id)
        case = self._case_row(case_id)
        work_item = self._open_work_item_for_case(case_id, actor_user_id, item_type="DRAFT")
        values = _json_loads(case["form_values_json"], {})
        values.update(form_values)
        values = self._normalize_form_values(values, initiator_user_id=case["initiator_id"])
        now = now_iso()
        title = self._case_title(values, case["initiator_name"])
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE workflow_cases
                SET title = ?, form_values_json = ?, updated_at = ?
                WHERE id = ?
                """,
                (title, _json_dumps(values), now, case_id),
            )
            self._append_event(
                conn,
                case_id=case_id,
                work_item_id=work_item["id"],
                event_type="FORM_SAVED",
                node_id=work_item["node_id"],
                node_name=work_item["node_name"],
                actor_id=actor_user_id,
                actor_name=actor["name"],
                message=f"{actor['name']} 保存草稿",
                payload={"changed_fields": sorted(form_values)},
                created_at=now,
            )
        return self.case_detail(case_id, user_id=actor_user_id)

    def submit_case(
        self,
        *,
        case_id: str,
        actor_user_id: str,
        form_values: dict[str, Any] | None = None,
        selected_edge_id: str | None = None,
        comment: str | None = None,
    ) -> dict[str, Any]:
        actor = self.user_payload(actor_user_id)
        case = self._case_row(case_id)
        work_item = self._open_work_item_for_case(case_id, actor_user_id, item_type="DRAFT")
        workflow = self.workflow_definition(case["workflow_definition_id"])
        process = ProcessDefinition.model_validate(_json_loads(workflow["definition_json"]))
        draft_node = self._node(process, "draft")
        path = self._path_from_edge_or_name(draft_node, selected_edge_id=selected_edge_id, path_name=None)
        values = _json_loads(case["form_values_json"], {})
        values.update(form_values or {})
        values = self._normalize_form_values(values, initiator_user_id=case["initiator_id"])
        missing = self._missing_required_draft_fields(process, values)
        if missing:
            raise RuntimeError(f"请先补齐必填字段: {', '.join(missing)}")
        target_node = self._node(process, path.target_node_id)
        handlers = self._resolve_node_handlers(target_node, case["initiator_id"])
        now = now_iso()
        next_ids = [item["user_id"] for item in handlers]
        next_names = [item["name"] for item in handlers]
        title = self._case_title(values, case["initiator_name"])
        with self._connect() as conn:
            self._complete_work_item_row(
                conn,
                work_item_id=work_item["id"],
                submitted_by=actor,
                decision=None,
                edge_id=self._edge_id(draft_node, path),
                path=path,
                target_node_name=target_node.node_name,
                next_ids=next_ids,
                next_names=next_names,
                comment=comment,
                now=now,
            )
            conn.execute(
                """
                UPDATE workflow_cases
                SET title = ?, status = 'IN_PROGRESS', current_node_id = ?, current_node_name = ?,
                    current_assignee_ids_json = ?, form_values_json = ?, updated_at = ?
                WHERE id = ?
                """,
                (
                    title,
                    target_node.node_id,
                    target_node.node_name,
                    _json_dumps(next_ids),
                    _json_dumps(values),
                    now,
                    case_id,
                ),
            )
            self._append_event(
                conn,
                case_id=case_id,
                work_item_id=work_item["id"],
                event_type="WORK_ITEM_COMPLETED",
                node_id=draft_node.node_id,
                node_name=draft_node.node_name,
                actor_id=actor_user_id,
                actor_name=actor["name"],
                message=f"{actor['name']} 提交申请，流转至{target_node.node_name}",
                payload={"path_name": path.path_name, "next_handler_ids": next_ids},
                created_at=now,
                to_node_id=target_node.node_id,
            )
            for handler in handlers:
                next_work_item_id = self._create_work_item(
                    conn,
                    case_id=case_id,
                    workflow_definition_id=workflow["id"],
                    node=target_node,
                    item_type="APPROVAL",
                    case_initiator_id=case["initiator_id"],
                    case_initiator_name=case["initiator_name"],
                    handler_id=handler["user_id"],
                    handler_name=handler["name"],
                    summary=title,
                    now=now,
                )
                self._append_event(
                    conn,
                    case_id=case_id,
                    work_item_id=next_work_item_id,
                    event_type="WORK_ITEM_CREATED",
                    node_id=target_node.node_id,
                    node_name=target_node.node_name,
                    actor_id=None,
                    actor_name=None,
                    message=f"{target_node.node_name} 分配给 {handler['name']}",
                    payload={"assignee_id": handler["user_id"]},
                    created_at=now,
                )
        return self.case_detail(case_id, user_id=actor_user_id)

    def route_options(self, *, work_item_id: str, actor_user_id: str, decision: str | None = None) -> list[dict[str, Any]]:
        work_item = self._work_item_row(work_item_id)
        if work_item["status"] != "OPEN":
            raise RuntimeError("work item is not open")
        if work_item["current_handler_id"] != actor_user_id:
            raise RuntimeError("only current handler can route this work item")
        case = self._case_row(work_item["case_id"])
        workflow = self.workflow_definition(work_item["workflow_definition_id"])
        process = ProcessDefinition.model_validate(_json_loads(workflow["definition_json"]))
        node = self._node(process, work_item["node_id"])
        form_values = _json_loads(case["form_values_json"], {})
        context = {**form_values, "结论性意见": decision, "conclusive_opinion": decision}
        options = []
        for path in node.submit_paths:
            result = evaluate_condition(path.condition, context)
            options.append(
                {
                    "edge_id": self._edge_id(node, path),
                    "path_name": path.path_name,
                    "condition": path.condition,
                    "target_node_id": path.target_node_id,
                    "target_node_name": process.get_node_name(path.target_node_id),
                    "enabled": result.matched,
                    "reason": result.reason,
                }
            )
        return options

    def complete_work_item(
        self,
        *,
        work_item_id: str,
        actor_user_id: str,
        decision: str,
        selected_edge_id: str | None = None,
        path_name: str | None = None,
        comment: str | None = None,
    ) -> dict[str, Any]:
        actor = self.user_payload(actor_user_id)
        work_item = self._work_item_row(work_item_id)
        if work_item["status"] != "OPEN":
            raise RuntimeError("work item is not open")
        if work_item["type"] != "APPROVAL":
            raise RuntimeError("draft work items must be submitted through /cases/{case_id}/submit")
        if work_item["current_handler_id"] != actor_user_id:
            raise RuntimeError("only current handler can complete this work item")
        case = self._case_row(work_item["case_id"])
        workflow = self.workflow_definition(work_item["workflow_definition_id"])
        process = ProcessDefinition.model_validate(_json_loads(workflow["definition_json"]))
        node = self._node(process, work_item["node_id"])
        if node.opinion and node.opinion.conclusive_required and decision not in node.opinion.conclusive_options:
            raise RuntimeError(f"decision must be one of: {', '.join(node.opinion.conclusive_options)}")
        path = self._path_from_edge_or_name(node, selected_edge_id=selected_edge_id, path_name=path_name)
        enabled = next(
            (item for item in self.route_options(work_item_id=work_item_id, actor_user_id=actor_user_id, decision=decision)
             if item["edge_id"] == self._edge_id(node, path)),
            None,
        )
        if not enabled or not enabled["enabled"]:
            reason = enabled["reason"] if enabled else "selected path not found"
            raise RuntimeError(f"selected path is not available: {reason}")

        now = now_iso()
        target_node_id = path.target_node_id
        next_ids: list[str] = []
        next_names: list[str] = []
        target_name = process.get_node_name(target_node_id)
        if target_node_id not in {"END", "DRAFT"}:
            target_node = self._node(process, target_node_id)
            handlers = self._resolve_node_handlers(target_node, case["initiator_id"])
            next_ids = [item["user_id"] for item in handlers]
            next_names = [item["name"] for item in handlers]

        with self._connect() as conn:
            self._complete_work_item_row(
                conn,
                work_item_id=work_item_id,
                submitted_by=actor,
                decision=decision,
                edge_id=self._edge_id(node, path),
                path=path,
                target_node_name=target_name,
                next_ids=next_ids,
                next_names=next_names,
                comment=comment,
                now=now,
            )
            self._append_event(
                conn,
                case_id=case["id"],
                work_item_id=work_item_id,
                event_type="WORK_ITEM_COMPLETED",
                node_id=node.node_id,
                node_name=node.node_name,
                actor_id=actor_user_id,
                actor_name=actor["name"],
                decision=decision,
                comment=comment,
                message=f"{actor['name']} 在{node.node_name}选择{decision}，提交路径：{path.path_name}",
                payload={"path_name": path.path_name},
                created_at=now,
                from_node_id=node.node_id,
                to_node_id=target_node_id,
            )
            if target_node_id == "DRAFT":
                self._return_to_draft(conn, case=case, process=process, actor=actor, work_item_id=work_item_id, comment=comment, now=now)
            elif target_node_id == "END":
                self._complete_case(conn, case_id=case["id"], actor=actor, work_item_id=work_item_id, now=now)
            else:
                target_node = self._node(process, target_node_id)
                if len(next_ids) == 1 and next_ids[0] == actor_user_id:
                    self._skip_same_actor_and_complete(
                        conn,
                        case=case,
                        workflow_id=workflow["id"],
                        process=process,
                        node=target_node,
                        actor=actor,
                        summary=work_item["summary"] or case["title"],
                        now=now,
                    )
                else:
                    conn.execute(
                        """
                        UPDATE workflow_cases
                        SET status = 'IN_PROGRESS', current_node_id = ?, current_node_name = ?,
                            current_assignee_ids_json = ?, updated_at = ?
                        WHERE id = ?
                        """,
                        (target_node.node_id, target_node.node_name, _json_dumps(next_ids), now, case["id"]),
                    )
                    for handler_id, handler_name in zip(next_ids, next_names, strict=True):
                        next_work_item_id = self._create_work_item(
                            conn,
                            case_id=case["id"],
                            workflow_definition_id=workflow["id"],
                            node=target_node,
                            item_type="APPROVAL",
                            case_initiator_id=case["initiator_id"],
                            case_initiator_name=case["initiator_name"],
                            handler_id=handler_id,
                            handler_name=handler_name,
                            summary=work_item["summary"] or case["title"],
                            now=now,
                        )
                        self._append_event(
                            conn,
                            case_id=case["id"],
                            work_item_id=next_work_item_id,
                            event_type="WORK_ITEM_CREATED",
                            node_id=target_node.node_id,
                            node_name=target_node.node_name,
                            actor_id=None,
                            actor_name=None,
                            message=f"{target_node.node_name} 分配给 {handler_name}",
                            payload={"assignee_id": handler_id},
                            created_at=now,
                        )
        return self.case_detail(case["id"], user_id=actor_user_id)

    def workflow_definition(self, workflow_definition_id: str) -> dict[str, Any]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM workflow_definitions WHERE id = ?",
                (workflow_definition_id,),
            ).fetchone()
        if row is None:
            raise KeyError(f"workflow definition {workflow_definition_id} not found")
        return dict(row)

    def user_payload(self, user_id: str) -> dict[str, Any]:
        with self._connect() as conn:
            user = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
            positions = conn.execute(
                """
                SELECT up.*, ou.name AS org_unit_name, ou.type AS org_unit_type
                FROM user_positions up
                JOIN org_units ou ON ou.id = up.org_unit_id
                WHERE up.user_id = ?
                ORDER BY up.is_primary DESC, up.position_code
                """,
                (user_id,),
            ).fetchall()
        if user is None:
            raise KeyError(f"user {user_id} not found")
        position_payloads = [
            {
                **dict(row),
                "is_primary": bool(row["is_primary"]),
                "is_manager": bool(row["is_manager"]),
                "role_tags": _json_loads(row["role_tags_json"], []),
            }
            for row in positions
        ]
        return {
            "user_id": user["id"],
            "name": user["name"],
            "employee_no": user["employee_no"],
            "status": user["status"],
            "display_role": self._display_role(user_id),
            "positions": position_payloads,
        }

    def initial_form_values(self, process: ProcessDefinition, *, user_id: str | None = None) -> dict[str, Any]:
        user = self.user_payload(user_id) if user_id else None
        dept_name = self._primary_dept_name(user_id) if user_id else ""
        values: dict[str, Any] = {}
        for field in process.form_fields:
            if field.field_name == "标题":
                values[field.field_name] = f"{user['name']}请假申请" if user else "员工请假申请"
            elif field.field_name == "申请人":
                values[field.field_name] = user["name"] if user else ""
            elif field.field_name == "所属部门":
                values[field.field_name] = dept_name
            elif field.options:
                values[field.field_name] = field.options[0]
            elif field.default_value and not _looks_like_auto_rule(field.default_value):
                values[field.field_name] = field.default_value
            else:
                values[field.field_name] = ""
        return values

    def _return_to_draft(
        self,
        conn: sqlite3.Connection,
        *,
        case: dict[str, Any],
        process: ProcessDefinition,
        actor: dict[str, Any],
        work_item_id: str,
        comment: str | None,
        now: str,
    ) -> None:
        draft_node = self._node(process, "draft")
        initiator_id = case["initiator_id"]
        initiator_name = case["initiator_name"]
        conn.execute(
            """
            UPDATE workflow_cases
            SET status = 'RETURNED', current_node_id = 'draft', current_node_name = ?,
                current_assignee_ids_json = ?, updated_at = ?
            WHERE id = ?
            """,
            (draft_node.node_name, _json_dumps([initiator_id]), now, case["id"]),
        )
        draft_item_id = self._create_work_item(
            conn,
            case_id=case["id"],
            workflow_definition_id=case["workflow_definition_id"],
            node=draft_node,
            item_type="DRAFT",
            case_initiator_id=initiator_id,
            case_initiator_name=initiator_name,
            handler_id=initiator_id,
            handler_name=initiator_name,
            summary=case["title"],
            now=now,
        )
        self._append_event(
            conn,
            case_id=case["id"],
            work_item_id=work_item_id,
            event_type="CASE_RETURNED",
            node_id=draft_node.node_id,
            node_name=draft_node.node_name,
            actor_id=actor["user_id"],
            actor_name=actor["name"],
            decision="不同意",
            comment=comment,
            message=f"{actor['name']} 退回申请，草稿任务回到 {initiator_name}",
            payload={"draft_work_item_id": draft_item_id},
            created_at=now,
            to_node_id="draft",
        )

    def _complete_case(
        self,
        conn: sqlite3.Connection,
        *,
        case_id: str,
        actor: dict[str, Any],
        work_item_id: str,
        now: str,
    ) -> None:
        conn.execute(
            """
            UPDATE workflow_cases
            SET status = 'COMPLETED', current_node_id = 'END', current_node_name = '流程结束',
                current_assignee_ids_json = ?, updated_at = ?, completed_at = ?
            WHERE id = ?
            """,
            (_json_dumps([]), now, now, case_id),
        )
        self._append_event(
            conn,
            case_id=case_id,
            work_item_id=work_item_id,
            event_type="CASE_COMPLETED",
            node_id="END",
            node_name="流程结束",
            actor_id=actor["user_id"],
            actor_name=actor["name"],
            message="流程已完成",
            payload={},
            created_at=now,
            to_node_id="END",
        )

    def _skip_same_actor_and_complete(
        self,
        conn: sqlite3.Connection,
        *,
        case: dict[str, Any],
        workflow_id: str,
        process: ProcessDefinition,
        node: FlowNode,
        actor: dict[str, Any],
        summary: str,
        now: str,
    ) -> None:
        end_path = next((path for path in node.submit_paths if path.target_node_id == "END"), None)
        skipped_item_id = self._create_work_item(
            conn,
            case_id=case["id"],
            workflow_definition_id=workflow_id,
            node=node,
            item_type="APPROVAL",
            case_initiator_id=case["initiator_id"],
            case_initiator_name=case["initiator_name"],
            handler_id=actor["user_id"],
            handler_name=actor["name"],
            summary=summary,
            now=now,
            status="SKIPPED",
        )
        conn.execute(
            """
            UPDATE work_items
            SET completed_at = ?, submitted_by_id = ?, submitted_by_name = ?, decision = ?,
                selected_edge_id = ?, selected_edge_label = ?, target_node_id = 'END',
                target_node_name = '流程结束', updated_at = ?
            WHERE id = ?
            """,
            (
                now,
                actor["user_id"],
                actor["name"],
                "同意",
                self._edge_id(node, end_path) if end_path else None,
                end_path.path_name if end_path else "流程结束",
                now,
                skipped_item_id,
            ),
        )
        self._append_event(
            conn,
            case_id=case["id"],
            work_item_id=skipped_item_id,
            event_type="WORK_ITEM_SKIPPED",
            node_id=node.node_id,
            node_name=node.node_name,
            actor_id=actor["user_id"],
            actor_name=actor["name"],
            decision="同意",
            message=f"{node.node_name} 与上一处理人为同一人，自动跳过并完成流程",
            payload={"reason": "same_actor"},
            created_at=now,
            to_node_id="END",
        )
        self._complete_case(conn, case_id=case["id"], actor=actor, work_item_id=skipped_item_id, now=now)

    def _create_work_item(
        self,
        conn: sqlite3.Connection,
        *,
        case_id: str,
        workflow_definition_id: str,
        node: FlowNode,
        item_type: str,
        case_initiator_id: str,
        case_initiator_name: str,
        handler_id: str,
        handler_name: str,
        summary: str,
        now: str,
        status: str = "OPEN",
    ) -> str:
        work_item_id = f"wi_{uuid4().hex[:12]}"
        conn.execute(
            """
            INSERT INTO work_items (
                id, case_id, workflow_definition_id, node_id, node_name, type, status,
                case_initiator_id, case_initiator_name, current_handler_id, current_handler_name,
                arrived_at, due_at, completed_at, priority, risk_level, summary,
                submitted_by_id, submitted_by_name, decision, selected_edge_id,
                selected_edge_label, target_node_id, target_node_name,
                next_handler_ids_json, next_handler_names_json, comment, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                work_item_id,
                case_id,
                workflow_definition_id,
                node.node_id,
                node.node_name,
                item_type,
                status,
                case_initiator_id,
                case_initiator_name,
                handler_id,
                handler_name,
                now,
                None,
                now if status in {"COMPLETED", "SKIPPED"} else None,
                "normal",
                "low",
                summary,
                None,
                None,
                None,
                None,
                None,
                None,
                None,
                _json_dumps([]),
                _json_dumps([]),
                None,
                now,
                now,
            ),
        )
        return work_item_id

    def _complete_work_item_row(
        self,
        conn: sqlite3.Connection,
        *,
        work_item_id: str,
        submitted_by: dict[str, Any],
        decision: str | None,
        edge_id: str,
        path: SubmitPath,
        target_node_name: str,
        next_ids: list[str],
        next_names: list[str],
        comment: str | None,
        now: str,
    ) -> None:
        conn.execute(
            """
            UPDATE work_items
            SET status = 'COMPLETED', completed_at = ?, submitted_by_id = ?, submitted_by_name = ?,
                decision = ?, selected_edge_id = ?, selected_edge_label = ?, target_node_id = ?,
                target_node_name = ?, next_handler_ids_json = ?, next_handler_names_json = ?,
                comment = ?, updated_at = ?
            WHERE id = ?
            """,
            (
                now,
                submitted_by["user_id"],
                submitted_by["name"],
                decision,
                edge_id,
                path.path_name,
                path.target_node_id,
                target_node_name,
                _json_dumps(next_ids),
                _json_dumps(next_names),
                comment,
                now,
                work_item_id,
            ),
        )

    def _append_event(
        self,
        conn: sqlite3.Connection,
        *,
        case_id: str,
        work_item_id: str | None,
        event_type: str,
        node_id: str | None,
        node_name: str | None,
        actor_id: str | None,
        actor_name: str | None,
        message: str,
        payload: dict[str, Any],
        created_at: str,
        decision: str | None = None,
        comment: str | None = None,
        from_node_id: str | None = None,
        to_node_id: str | None = None,
    ) -> None:
        conn.execute(
            """
            INSERT INTO timeline_events (
                case_id, work_item_id, type, node_id, node_name, actor_id, actor_name,
                decision, comment, from_node_id, to_node_id, message, payload_json, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                case_id,
                work_item_id,
                event_type,
                node_id,
                node_name,
                actor_id,
                actor_name,
                decision,
                comment,
                from_node_id,
                to_node_id,
                message,
                _json_dumps(payload),
                created_at,
            ),
        )

    def _resolve_node_handlers(self, node: FlowNode, initiator_user_id: str) -> list[dict[str, Any]]:
        if node.is_draft:
            return [self.user_payload(initiator_user_id)]
        if not node.handler:
            raise RuntimeError(f"node {node.node_id} has no handler config")
        resolved = resolve_role(self.org_data, node.handler.role, applicant_user_id=initiator_user_id)
        if not resolved:
            raise RuntimeError(f"cannot resolve handler role: {node.handler.role}")
        if node.handler.mode.value == "单选-单人处理":
            resolved = resolved[:1]
        return [self.user_payload(item.user_id) for item in resolved]

    def _missing_required_draft_fields(self, process: ProcessDefinition, values: dict[str, Any]) -> list[str]:
        missing: list[str] = []
        for field in process.form_fields:
            is_required = "draft" in field.required_stages or "all" in field.required_stages
            is_editable = "draft" in field.editable_stages or "all" in field.editable_stages
            if is_required and is_editable and _is_empty(values.get(field.field_name)):
                missing.append(field.field_name)
        return missing

    def _normalize_form_values(self, values: dict[str, Any], *, initiator_user_id: str) -> dict[str, Any]:
        normalized = dict(values)
        initiator = self.user_payload(initiator_user_id)
        normalized["申请人"] = normalized.get("申请人") or initiator["name"]
        normalized["所属部门"] = normalized.get("所属部门") or self._primary_dept_name(initiator_user_id)
        days = self._calculate_leave_days(normalized.get("开始日期"), normalized.get("结束日期"))
        if days is not None:
            normalized["请假天数"] = days
        normalized["标题"] = self._case_title(normalized, initiator["name"])
        return normalized

    def _case_title(self, values: dict[str, Any], initiator_name: str) -> str:
        leave_type = values.get("假期类型") or ""
        days = values.get("请假天数")
        if leave_type and days not in {None, ""}:
            return f"{initiator_name}{leave_type}请假申请（{days}天）"
        if leave_type:
            return f"{initiator_name}{leave_type}请假申请"
        return f"{initiator_name}请假申请"

    def _calculate_leave_days(self, start: Any, end: Any) -> int | None:
        if not start or not end:
            return None
        try:
            start_date = date.fromisoformat(str(start))
            end_date = date.fromisoformat(str(end))
        except ValueError:
            return None
        return max((end_date - start_date).days + 1, 1)

    def _path_from_edge_or_name(
        self,
        node: FlowNode,
        *,
        selected_edge_id: str | None,
        path_name: str | None,
    ) -> SubmitPath:
        for path in node.submit_paths:
            if selected_edge_id and self._edge_id(node, path) == selected_edge_id:
                return path
            if path_name and path.path_name == path_name:
                return path
        if not selected_edge_id and not path_name:
            return node.submit_paths[0]
        raise RuntimeError("selected path not found")

    def _edge_id(self, node: FlowNode, path: SubmitPath | None) -> str:
        if path is None:
            return f"{node.node_id}__none"
        index = node.submit_paths.index(path) + 1
        return f"{node.node_id}__{index}"

    def _node(self, process: ProcessDefinition, node_id: str) -> FlowNode:
        normalized = "draft" if node_id == "DRAFT" else node_id
        node = process.get_node_by_id(normalized)
        if node is None:
            raise RuntimeError(f"node {node_id} not found")
        return node

    def _open_work_item_for_case(self, case_id: str, user_id: str, *, item_type: str) -> dict[str, Any]:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT * FROM work_items
                WHERE case_id = ? AND current_handler_id = ? AND status = 'OPEN' AND type = ?
                ORDER BY created_at DESC
                LIMIT 1
                """,
                (case_id, user_id, item_type),
            ).fetchone()
        if row is None:
            raise RuntimeError(f"no open {item_type.lower()} item for current user")
        return dict(row)

    def _work_item_row(self, work_item_id: str) -> dict[str, Any]:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM work_items WHERE id = ?", (work_item_id,)).fetchone()
        if row is None:
            raise KeyError(f"work item {work_item_id} not found")
        return dict(row)

    def _case_row(self, case_id: str) -> dict[str, Any]:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM workflow_cases WHERE id = ?", (case_id,)).fetchone()
        if row is None:
            raise KeyError(f"case {case_id} not found")
        return dict(row)

    def _work_item_card(self, row: dict[str, Any], *, bucket: str) -> dict[str, Any]:
        return {
            "work_item_id": row["id"],
            "case_id": row["case_id"],
            "case_no": row["case_no"],
            "case_title": row["case_title"],
            "workflow_name": row["workflow_name"],
            "bucket": bucket,
            "node_id": row["node_id"],
            "node_name": row["node_name"],
            "type": row["type"],
            "status": row["status"],
            "case_status": row["case_status"],
            "initiator_id": row["initiator_id"],
            "initiator_name": row["initiator_name"],
            "current_handler_id": row["current_handler_id"],
            "current_handler_name": row["current_handler_name"],
            "decision": row["decision"],
            "selected_edge_label": row["selected_edge_label"],
            "target_node_name": row["target_node_name"],
            "handled_count": row.get("handled_count"),
            "case_current_node_id": row.get("case_current_node_id"),
            "case_current_node_name": row.get("case_current_node_name"),
            "completed_at": row.get("completed_at"),
            "updated_at": row["updated_at"],
            "case_updated_at": row["case_updated_at"],
        }

    def _case_card(self, row: dict[str, Any]) -> dict[str, Any]:
        return {
            "case_id": row["id"],
            "case_no": row["case_no"],
            "title": row["title"],
            "workflow_name": row["workflow_name"],
            "status": row["status"],
            "initiator_id": row["initiator_id"],
            "initiator_name": row["initiator_name"],
            "current_node_id": row["current_node_id"],
            "current_node_name": row["current_node_name"],
            "current_assignee_ids": _json_loads(row["current_assignee_ids_json"], []),
            "updated_at": row["updated_at"],
            "completed_at": row["completed_at"],
        }

    def _case_payload(self, row: dict[str, Any]) -> dict[str, Any]:
        payload = dict(row)
        payload["current_assignee_ids"] = _json_loads(row["current_assignee_ids_json"], [])
        payload["form_values"] = _json_loads(row["form_values_json"], {})
        payload.pop("current_assignee_ids_json", None)
        payload.pop("form_values_json", None)
        return payload

    def _work_item_payload(self, row: dict[str, Any]) -> dict[str, Any]:
        payload = dict(row)
        payload["next_handler_ids"] = _json_loads(row["next_handler_ids_json"], [])
        payload["next_handler_names"] = _json_loads(row["next_handler_names_json"], [])
        payload.pop("next_handler_ids_json", None)
        payload.pop("next_handler_names_json", None)
        return payload

    def _event_payload(self, row: dict[str, Any]) -> dict[str, Any]:
        payload = dict(row)
        payload["payload"] = _json_loads(row["payload_json"], {})
        payload.pop("payload_json", None)
        return payload

    def _workflow_payload(self, row: dict[str, Any], *, include_definition: bool) -> dict[str, Any]:
        definition = _json_loads(row["definition_json"])
        process = ProcessDefinition.model_validate(definition)
        payload = {
            "workflow_definition_id": row["id"],
            "workflow_id": row["workflow_id"],
            "code": row["code"],
            "name": row["name"],
            "category": row["category"],
            "owner_dept_id": row["owner_dept_id"],
            "owner_dept_name": row["owner_dept_name"],
            "version": row["version"],
            "status": row["status"],
            "description": row["description"],
            "launch_scope": _json_loads(row["launch_scope_json"], {}),
            "field_count": len(process.form_fields),
            "node_count": len(process.flow_nodes),
            "path_count": sum(len(node.submit_paths) for node in process.flow_nodes),
            "updated_at": row["updated_at"],
        }
        if include_definition:
            payload["definition"] = definition
        return payload

    def _workflow_management_payload(self, row: dict[str, Any], *, include_definition: bool) -> dict[str, Any]:
        payload = self._workflow_payload(row, include_definition=include_definition)
        definition = _json_loads(row["definition_json"])
        process = ProcessDefinition.model_validate(definition)
        # 处理期限是可选建议项、不是规定项（同 workflow_design_service._validate_definition）：
        # missing_time_limit_nodes 仍算出来供环节详情面板展示，但不再当"issue"计入
        # issue_count/健康度/待处理——这条原始（无设计会话）路径本来也只检查这一项，
        # 去掉之后就没有别的可报的问题了，issue_count 恒为 0。
        missing_time_limit_nodes = [node.node_id for node in process.flow_nodes if node.time_limit_days is None]
        issue_count = 0
        payload["runtime_stats"] = self._workflow_runtime_stats(row["id"])
        payload["management"] = {
            "owner_name": row["owner_dept_name"] or "流程管理员",
            "publish_state": "未发布草稿" if row["status"] == "DRAFT" else ("已发布" if row["status"] == "PUBLISHED" else row["status"]),
            "last_published": "未发布" if row["status"] == "DRAFT" else f"{row['version']} · 已发布",
            "draft_state": "AI 新建草稿" if row["status"] == "DRAFT" else "暂无草稿",
            "draft_version": row["version"] if row["status"] == "DRAFT" else "未生成",
            "draft_progress": "35%" if row["status"] == "DRAFT" else "0%",
            "issue_count": issue_count,
            "health": "待完善" if issue_count else "正常",
            "health_tone": "amber" if issue_count else "green",
            "next_action": (
                "新建流程草稿尚未发布，可继续进入 AI 流程设计完善。"
                if row["status"] == "DRAFT"
                else "当前发布版本配置完整，可继续观察运行数据。"
            ),
            "missing_time_limit_nodes": missing_time_limit_nodes,
            "non_time_limit_issue_count": issue_count,
            "can_delete_draft": row["status"] == "DRAFT",
            "is_new_draft": row["status"] == "DRAFT",
        }
        payload["summary"] = {
            "field_count": len(process.form_fields),
            "node_count": len(process.flow_nodes),
            "path_count": sum(len(node.submit_paths) for node in process.flow_nodes),
            "role_count": len(process.roles or []),
            "attachment_count": len(process.attachments or []),
            "time_limit_configured_count": len(process.flow_nodes) - len(missing_time_limit_nodes),
        }
        return payload

    def _workflow_runtime_stats(self, workflow_definition_id: str) -> dict[str, int]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT status, COUNT(*) AS value
                FROM workflow_cases
                WHERE workflow_definition_id = ?
                GROUP BY status
                """,
                (workflow_definition_id,),
            ).fetchall()
        by_status = {row["status"]: int(row["value"]) for row in rows}
        return {
            "total_instances": sum(by_status.values()),
            "running_instances": by_status.get("IN_PROGRESS", 0),
            "draft_instances": by_status.get("DRAFT", 0),
            "returned_instances": by_status.get("RETURNED", 0),
            "completed_instances": by_status.get("COMPLETED", 0),
        }

    def _read_leave_process(self) -> ProcessDefinition:
        # 发布的请假 V1.0 = 金标准 target.json，但故意在「部门总经理审批」环节保留一档
        # 4~7 天的路径覆盖漏洞（把「流程结束」的天数上限从 ≤7天 降回 ≤3天）——这就是
        # 运维/分析在运行侧发现、经反哺闭环让设计副驾修复的那个真实缺口。金标准 target.json
        # 本身不含此缺口（评测答案 = 修好后的样子），二者不同正是"反哺闭环"存在的前提：
        # 上线的 V1.0 有优化空间，修好后向金标准收敛。评测/分析都直接读 target.json，
        # 不经过这里，所以不受影响。
        return _inject_leave_published_gap(load_process_definition(find_standard_json(LEAVE_CASE_DIR)))

    def _read_eoa140_process(self) -> ProcessDefinition:
        return load_process_definition(find_standard_json(EOA140_CASE_DIR))

    def _read_expense_process(self) -> ProcessDefinition:
        return load_process_definition(find_standard_json(EXPENSE_CASE_DIR))

    def _read_procurement_process(self) -> ProcessDefinition:
        return load_process_definition(find_standard_json(PROCUREMENT_CASE_DIR))

    def _read_seal_process(self) -> ProcessDefinition:
        return load_process_definition(find_standard_json(SEAL_CASE_DIR))

    def _next_case_no(self) -> str:
        today = date.today().strftime("%Y%m%d")
        prefix = f"LEAVE-{today}-"
        with self._connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS value FROM workflow_cases WHERE case_no LIKE ?",
                (f"{prefix}%",),
            ).fetchone()
        return f"{prefix}{int(row['value']) + 1:04d}"

    def _primary_dept_name(self, user_id: str | None) -> str:
        if not user_id:
            return ""
        assignments = get_user_assignments(self.org_data, user_id).get("primary", [])
        if not assignments:
            return ""
        dept_id = assignments[0].get("dept_id")
        for dept in self.org_data.get("departments", []):
            if dept.get("dept_id") == dept_id:
                return dept.get("dept_name", dept_id)
        return dept_id or ""

    def _display_role(self, user_id: str) -> str:
        assignments = get_user_assignments(self.org_data, user_id)
        primary = assignments.get("primary") or []
        secondary = assignments.get("secondary") or []
        first = primary[0] if primary else secondary[0] if secondary else None
        if not first:
            return "未配置岗位"
        dept_name = self._dept_name(first.get("dept_id"))
        suffix = f" + {len(secondary)} 个兼岗" if secondary else ""
        return f"{dept_name} / {first.get('title') or first.get('position_code')}{suffix}"

    def _dept_name(self, dept_id: str | None) -> str:
        for dept in self.org_data.get("departments", []):
            if dept.get("dept_id") == dept_id:
                return dept.get("dept_name", dept_id or "")
        return dept_id or ""

    def _require_user(self, user_id: str) -> None:
        self.user_payload(user_id)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn


def _json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False)


def _json_loads(value: str | None, default: Any | None = None) -> Any:
    if value is None or value == "":
        return default
    return json.loads(value)


def _is_empty(value: Any) -> bool:
    return value is None or value == "" or value == []


def _looks_like_auto_rule(value: str) -> bool:
    return any(token in value for token in ("系统自动", "自动带出", "当前登录", "自动生成"))
