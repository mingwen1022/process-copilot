"""SQL 守卫对抗测试——这是安全边界，不是普通功能，必须有对抗用例。

每个用例对应设计方案里的一层防御：单语句、只读 SELECT、白名单表、CTE 别名不
误伤、进度回调超时。正常查询也要覆盖，防止守卫矫枉过正把合法查询也拦了。
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from app.analytics.sql_store import open_sql_store
from app.tools.sql_query_tool import guard_sql, run_sql_readonly
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
        case_attributes={"请假类型": "事假", "请假天数": i + 1},
        created_at=created,
        closed_at=closed,
        events=[event],
    )


# ── 纯守卫单测（不碰连接）──────────────────────────

@pytest.mark.parametrize(
    "sql",
    [
        "SELECT * FROM cases",
        "SELECT case_id, COUNT(*) FROM events GROUP BY case_id",
        "SELECT c.case_id, e.dwell_hours FROM cases c JOIN events e ON c.case_id = e.case_id",
        "WITH agg AS (SELECT case_id, COUNT(*) n FROM events GROUP BY case_id) SELECT * FROM agg",
        "SELECT * FROM cases WHERE 请假天数 > 3 ORDER BY 请假天数 DESC LIMIT 10",
    ],
)
def test_guard_allows_legit_select(sql: str) -> None:
    ok, err = guard_sql(sql)
    assert ok is True
    assert err is None


@pytest.mark.parametrize(
    "sql,expect_snippet",
    [
        ("DROP TABLE events", "SELECT"),
        ("DELETE FROM cases", "SELECT"),
        ("UPDATE cases SET case_status = 'x'", "SELECT"),
        ("INSERT INTO cases (case_id) VALUES ('x')", "SELECT"),
        ("CREATE TABLE evil (x TEXT)", "SELECT"),
        ("ALTER TABLE cases ADD COLUMN x TEXT", "SELECT"),
        ("ATTACH DATABASE '/etc/passwd' AS x", "SELECT"),
        ("PRAGMA table_info(cases)", "SELECT"),
        ("SELECT * FROM cases; DROP TABLE cases;", "单条"),
        ("SELECT * FROM salaries", "允许范围"),
        ("SELECT * FROM cases WHERE case_id IN (SELECT case_id FROM salaries)", "允许范围"),
        ("SELECT * FROM sqlite_master", "允许范围"),
        ("", "不能为空"),
        ("这不是 SQL 是一句中文", None),
    ],
)
def test_guard_rejects_dangerous_or_out_of_scope_sql(sql: str, expect_snippet: str | None) -> None:
    ok, err = guard_sql(sql)
    assert ok is False
    assert err
    if expect_snippet:
        assert expect_snippet in err


def test_guard_does_not_false_positive_on_cte_alias() -> None:
    """CTE 别名（如 tmp_result）不是真实表，即使名字不在白名单里，也不该被误判——
    只要它的定义体只碰了白名单内的表。"""
    ok, err = guard_sql("WITH tmp_result AS (SELECT case_id FROM cases) SELECT * FROM tmp_result")
    assert ok is True, err


def test_guard_still_rejects_disallowed_table_hidden_inside_cte_body() -> None:
    """CTE 别名豁免不能被滥用来夹带真正的越权表访问。"""
    ok, err = guard_sql("WITH tmp_result AS (SELECT * FROM salaries) SELECT * FROM tmp_result")
    assert ok is False
    assert "允许范围" in err


# ── 结合真实只读连接的执行测试 ──────────────────────

def test_run_sql_readonly_executes_legit_query() -> None:
    with open_sql_store([_case(0), _case(1), _case(2)]) as store:
        result = run_sql_readonly("SELECT COUNT(*) AS n FROM cases", store.conn)
        assert "error" not in result
        assert result["rows"] == [[3]]
        assert result["columns"] == ["n"]


def test_run_sql_readonly_blocks_write_before_reaching_engine() -> None:
    with open_sql_store([_case(0)]) as store:
        result = run_sql_readonly("DELETE FROM cases", store.conn)
        assert "error" in result
        assert "SELECT" in result["error"]


def test_run_sql_readonly_blocks_disallowed_table() -> None:
    with open_sql_store([_case(0)]) as store:
        result = run_sql_readonly("SELECT * FROM sqlite_master", store.conn)
        assert "error" in result
        assert "允许范围" in result["error"]


def test_run_sql_readonly_reports_syntax_errors_without_crashing() -> None:
    with open_sql_store([_case(0)]) as store:
        result = run_sql_readonly("SELECT FROM WHERE", store.conn)
        assert "error" in result


def test_run_sql_readonly_row_cap_truncates() -> None:
    cases = [_case(i) for i in range(10)]
    with open_sql_store(cases) as store:
        result = run_sql_readonly("SELECT * FROM cases", store.conn, row_cap=3)
        assert result["row_count"] == 3
        assert result["truncated"] is True


def test_run_sql_readonly_engine_level_readonly_backstops_guard_gap() -> None:
    """即使假设守卫被绕过，只读连接本身也应该拒绝写操作（纵深防御第二道）。"""
    with open_sql_store([_case(0)]) as store:
        import sqlite3

        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            store.conn.execute("DELETE FROM cases")
