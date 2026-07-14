"""分析侧 SQL 工具的数据地基（tool-calling 逃生舱 · 确定性层）。

把内存里已有的 case/event 行——复用 `app.analytics.query` 里已经写好的拍平逻辑
（`_case_rows` / `_event_rows`），不重新实现一遍——灌进一个临时 SQLite 文件的
`cases` / `events` 两张表，供 `run_sql` 工具查询。

用真实文件 + 只读 URI 连接（`file:...?mode=ro`），不是内存库上开 `query_only`
pragma：让"只读"是连接层的硬约束，是 `app.tools.sql_query_tool` 里 SQL 解析器
之外独立的第二道防线，不是唯一防线。

每次请求现建现拆——分析侧本就"不缓存、重算成本可忽略"（见 analytics_service.py
模块docstring），这张表也不例外，不引入新的缓存失效问题。

`cases` 表**不含** node_id/node_name（案例经过的环节集合，列表值，SQL 表放不下
列表列）——要按环节下钻用 JOIN events，这样表结构更干净，也逼着 agent 用
关系型的方式思考，而不是把列表塞进一个字段里做字符串匹配。
"""

from __future__ import annotations

import sqlite3
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator

from app.analytics.query import _case_rows, _event_rows
from data.schema import CaseRecord

ALLOWED_TABLES = {"cases", "events"}

_CASE_FIXED_COLUMNS = [
    "case_id",
    "case_status",
    "flow_code",
    "flow_name",
    "initiator_name",
    "initiator_dept_name",
    "duration_hours",
    "node_visits",
    "is_completed",
    "has_return",
    "has_manual_intervention",
]
_CASE_EXCLUDED_COLUMNS = {"node_id", "node_name"}  # 列表值字段，SQL 表放不下，走 events JOIN

_EVENT_FIXED_COLUMNS = [
    "case_id",
    "node_id",
    "node_name",
    "action",
    "action_category",
    "resource_name",
    "resource_dept_name",
    "task_order",
    "dwell_hours",
    "is_ops",
]

_COLUMN_COMMENTS = {
    "case_id": "实例 id，与 events.case_id 关联",
    "duration_hours": "办结时长（小时），进行中为 NULL",
    "node_visits": "经过的环节任务数",
    "is_completed": "1=已办结，0=否",
    "has_return": "1=经历过退回，0=否",
    "has_manual_intervention": "1=有过人工介入，0=否",
    "dwell_hours": "该环节任务的停留时长（小时）",
    "is_ops": "1=运维人工介入产生的任务，0=正常任务",
    "task_order": "任务序号，同一实例内按顺序递增",
}


@dataclass(frozen=True)
class SqlAnalyticsStore:
    conn: sqlite3.Connection
    case_columns: list[str]
    event_columns: list[str]

    def schema_ddl(self) -> str:
        """给 agent system prompt 用的人话 schema 说明，不是真实 DDL 语法。"""

        def _describe(table: str, columns: list[str]) -> str:
            lines = [f"表 {table}："]
            for col in columns:
                comment = _COLUMN_COMMENTS.get(col, "")
                lines.append(f"  - {col}" + (f"  -- {comment}" if comment else ""))
            return "\n".join(lines)

        return "\n\n".join(
            [
                _describe("cases", self.case_columns),
                _describe("events", self.event_columns),
                "两表通过 case_id 关联；只能查询 cases / events 这两张表。",
            ]
        )


def _dynamic_attribute_columns(rows: list[dict[str, Any]], fixed: list[str]) -> list[str]:
    fixed_set = set(fixed) | _CASE_EXCLUDED_COLUMNS
    dynamic: set[str] = set()
    for row in rows:
        dynamic.update(k for k in row.keys() if k not in fixed_set)
    return sorted(dynamic)


def _create_and_populate(conn: sqlite3.Connection, table: str, columns: list[str], rows: list[dict[str, Any]]) -> None:
    quoted_cols = ", ".join(f'"{c}"' for c in columns)
    conn.execute(f'CREATE TABLE "{table}" ({quoted_cols})')
    placeholders = ", ".join("?" for _ in columns)
    values = [tuple(_sql_value(row.get(col)) for col in columns) for row in rows]
    if values:
        conn.executemany(f'INSERT INTO "{table}" ({quoted_cols}) VALUES ({placeholders})', values)


def _sql_value(value: Any) -> Any:
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, (str, int, float)) or value is None:
        return value
    return str(value)


@contextmanager
def open_sql_store(cases: list[CaseRecord]) -> Iterator[SqlAnalyticsStore]:
    """建库、灌数据、切只读连接、用完清理——一次请求的生命周期。"""
    case_rows = [
        {k: v for k, v in row.items() if k not in _CASE_EXCLUDED_COLUMNS} for row in _case_rows(cases)
    ]
    event_rows = _event_rows(cases)

    case_columns = _CASE_FIXED_COLUMNS + _dynamic_attribute_columns(case_rows, _CASE_FIXED_COLUMNS)
    event_columns = _EVENT_FIXED_COLUMNS + _dynamic_attribute_columns(event_rows, _EVENT_FIXED_COLUMNS)

    fd, tmp_path_str = tempfile.mkstemp(suffix=".sqlite", prefix="analytics_sql_store_")
    tmp_path = Path(tmp_path_str)
    import os

    os.close(fd)

    writer = sqlite3.connect(tmp_path_str)
    try:
        _create_and_populate(writer, "cases", case_columns, case_rows)
        _create_and_populate(writer, "events", event_columns, event_rows)
        writer.commit()
    finally:
        writer.close()

    # check_same_thread=False：这个连接在请求线程里建好，但 LangGraph 的工具
    # 执行可能被派发到线程池的另一个线程调用——单请求内只有一个调用方在用它，
    # 不存在并发读写竞争，禁用同线程检查是安全的。
    reader = sqlite3.connect(f"file:{tmp_path_str}?mode=ro", uri=True, check_same_thread=False)
    try:
        yield SqlAnalyticsStore(conn=reader, case_columns=case_columns, event_columns=event_columns)
    finally:
        reader.close()
        tmp_path.unlink(missing_ok=True)
