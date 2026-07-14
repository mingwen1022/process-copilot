from __future__ import annotations

from datetime import datetime, timedelta

from app.analytics.sql_store import open_sql_store
from data.schema import ActionCategory, CaseRecord, CaseStatus, EventRecord


def _case(i: int) -> CaseRecord:
    created = datetime(2026, 1, 1, 8, 0, 0) + timedelta(days=i)
    closed = created + timedelta(hours=10 + i)
    event = EventRecord(
        event_id=f"e{i}",
        case_id=f"c{i}",
        task_order=1,
        node_id="dept_supervisor",
        node_name="部门主管审批",
        resource_user_id="u1",
        resource_name="张三",
        resource_dept_name="研发部",
        enter_time=created,
        leave_time=closed,
        dwell_seconds=(closed - created).total_seconds(),
        action="流程结束",
        action_category=ActionCategory.END,
        target_node_id="END",
    )
    return CaseRecord(
        case_id=f"c{i}",
        flow_code="LEAVE-001",
        flow_name="员工请假申请流程",
        process_version="V1.0.0",
        case_status=CaseStatus.COMPLETED,
        initiator_user_id="u0",
        initiator_name="李四",
        initiator_dept_name="研发部",
        case_attributes={"请假类型": "事假" if i % 2 == 0 else "病假", "请假天数": i + 1},
        created_at=created,
        closed_at=closed,
        events=[event],
    )


def test_store_creates_cases_and_events_tables_with_row_counts() -> None:
    cases = [_case(i) for i in range(5)]
    with open_sql_store(cases) as store:
        case_count = store.conn.execute("SELECT COUNT(*) FROM cases").fetchone()[0]
        event_count = store.conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
        assert case_count == 5
        assert event_count == 5  # 每案例一个事件


def test_case_table_excludes_list_valued_node_columns() -> None:
    with open_sql_store([_case(0)]) as store:
        assert "node_id" not in store.case_columns
        assert "node_name" not in store.case_columns
        assert "node_id" in store.event_columns


def test_dynamic_case_attributes_become_columns() -> None:
    with open_sql_store([_case(0)]) as store:
        assert "请假类型" in store.case_columns
        assert "请假天数" in store.case_columns
        row = store.conn.execute("SELECT 请假类型, 请假天数 FROM cases WHERE case_id = 'c0'").fetchone()
        assert row == ("事假", 1)


def test_cases_and_events_joinable_on_case_id() -> None:
    with open_sql_store([_case(0), _case(1)]) as store:
        rows = store.conn.execute(
            "SELECT c.case_id, e.dwell_hours FROM cases c JOIN events e ON c.case_id = e.case_id ORDER BY c.case_id"
        ).fetchall()
        assert rows == [("c0", 10.0), ("c1", 11.0)]


def test_connection_is_engine_level_readonly() -> None:
    """守卫之外的第二道防线：就算解析器漏了，连接本身也写不动。"""
    with open_sql_store([_case(0)]) as store:
        import sqlite3

        try:
            store.conn.execute("DELETE FROM cases")
            store.conn.commit()
            assert False, "read-only connection unexpectedly allowed a write"
        except sqlite3.OperationalError as exc:
            assert "readonly" in str(exc).lower()


def test_schema_ddl_mentions_both_tables_and_join_key() -> None:
    with open_sql_store([_case(0)]) as store:
        ddl = store.schema_ddl()
        assert "cases" in ddl
        assert "events" in ddl
        assert "case_id" in ddl


def test_connection_usable_from_a_different_thread() -> None:
    """回归：LangGraph 的工具执行可能被派发到跟建库不同的线程；sqlite3 默认按
    创建线程绑定连接，这里必须显式关闭这项检查（发现于真实 tool-calling 调试：
    'SQLite objects created in a thread can only be used in that same thread'）。"""
    import concurrent.futures

    with open_sql_store([_case(0), _case(1)]) as store:
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            count = pool.submit(lambda: store.conn.execute("SELECT COUNT(*) FROM cases").fetchone()[0]).result()
        assert count == 2


def test_temp_file_cleaned_up_after_context_exit() -> None:
    from pathlib import Path

    captured_path: Path | None = None
    with open_sql_store([_case(0)]) as store:
        # database_list PRAGMA 第 3 列是文件路径
        row = store.conn.execute("PRAGMA database_list").fetchone()
        captured_path = Path(row[2])
        assert captured_path.exists()
    assert captured_path is not None
    assert not captured_path.exists()
