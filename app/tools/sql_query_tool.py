"""分析侧 SQL 工具（tool-calling 逃生舱）——全项目第一个"agent 自主决定要不要
调用"的产品内工具（区别于 artifact_generation 那个未接入任何产品流程的 ReAct
agent）。

心法：LLM 负责"要不要查、查什么"（把自然语言变成一条 SQL），确定性代码负责
"这条 SQL 能不能碰"（三层防御，见 guard_sql）。护栏管的是**安全**（能碰什么
表、能不能写库、查多久/多少行），不是**正确**（这条 SQL 有没有真的答对问题）
——所以这不是 typed 查询（app.analytics.query.AdHocMetricQuery）的替代品，是
它答不了时的逃生舱：typed 查询可穷举测试、错不了，是主路径；SQL 是长尾/多表
JOIN/复杂下钻时才用的次路径。agent 该先试 typed、typed 不够再上 SQL——这条
"先试便宜安全的、不够再升级"的顺序写在 analytics_query_agent 的 system prompt
里，不是这里的职责。

三层防御：
1. sqlglot 解析 AST：单语句、只允许 SELECT、显式封 ATTACH/PRAGMA/DDL/DML、
   表引用（排除 CTE 自身别名后）必须 ⊆ 白名单——挡子查询/CTE/JOIN 把不该碰的
   表名藏起来的招。
2. 只读连接（app.analytics.sql_store 用 file:...?mode=ro 打开）——就算解析器
   漏了也写不动，纵深防御的第二道。
3. 行数上限 + 进度回调超时——防止一次查询把整张大表搬进 context、或跑一条
   笛卡尔积拖垮进程。
"""

from __future__ import annotations

import sqlite3
import time
from typing import Any

import sqlglot
from langchain_core.tools import StructuredTool
from sqlglot import exp

from app.analytics.sql_store import ALLOWED_TABLES

ROW_CAP = 200
STATEMENT_TIMEOUT_S = 3.0

# 显式封杀的语句/节点类型：写库、DDL、附加数据库、PRAGMA、任意 shell/管理命令。
_DISALLOWED_EXPR: tuple[type[exp.Expression], ...] = (
    exp.Insert,
    exp.Update,
    exp.Delete,
    exp.Create,
    exp.Drop,
    exp.Alter,
    exp.Command,
    exp.Attach,
    exp.Pragma,
    exp.Transaction,
    exp.Copy,
    exp.Merge,
)


def guard_sql(sql: str) -> tuple[bool, str | None]:
    """只用解析器判断，不用正则——正则挡不住子查询/CTE/JOIN 把表名藏起来。"""
    clean = (sql or "").strip()
    if not clean:
        return False, "SQL 不能为空"
    try:
        statements = [s for s in sqlglot.parse(clean, read="sqlite") if s is not None]
    except Exception as exc:  # noqa: BLE001 - 解析失败一律拒绝，不猜测意图
        return False, f"SQL 无法解析：{exc}"

    if len(statements) != 1:
        return False, "只允许单条 SELECT 语句（不允许用分号拼接多条语句）"

    root = statements[0]
    if not isinstance(root, exp.Select):
        return False, f"只允许 SELECT 查询，检测到 {type(root).__name__}"

    for node in root.walk():
        if isinstance(node, _DISALLOWED_EXPR):
            return False, f"不允许的语句/节点类型：{type(node).__name__}"

    # CTE 自身的别名（如 WITH agg AS (...)）不是真实表，排除后再校验表引用白名单。
    cte_names = {cte.alias.lower() for cte in root.find_all(exp.CTE) if cte.alias}
    for table in root.find_all(exp.Table):
        name = (table.name or "").lower()
        if name in cte_names:
            continue
        if name not in ALLOWED_TABLES:
            return False, f"表 {table.name} 不在允许范围内（仅 cases / events）"

    return True, None


def run_sql_readonly(sql: str, conn: sqlite3.Connection, *, row_cap: int = ROW_CAP) -> dict[str, Any]:
    ok, err = guard_sql(sql)
    if not ok:
        return {"error": err, "sql": sql}

    start = time.monotonic()

    def _abort_if_slow() -> int:
        return 1 if (time.monotonic() - start) > STATEMENT_TIMEOUT_S else 0

    conn.set_progress_handler(_abort_if_slow, 1000)
    try:
        cursor = conn.execute(sql)
        columns = [d[0] for d in cursor.description] if cursor.description else []
        rows = cursor.fetchmany(row_cap + 1)
    except sqlite3.Error as exc:
        return {"error": f"执行失败：{exc}", "sql": sql}
    finally:
        conn.set_progress_handler(None, 0)

    truncated = len(rows) > row_cap
    rows = rows[:row_cap]
    return {
        "columns": columns,
        "rows": [list(r) for r in rows],
        "row_count": len(rows),
        "truncated": truncated,
        "sql": sql,
    }


def build_run_sql_tool(conn: sqlite3.Connection) -> StructuredTool:
    """把 run_sql_readonly 绑定到一个具体的只读连接上，包成 agent 可调用的 tool。"""

    def run_sql(sql: str) -> dict[str, Any]:
        return run_sql_readonly(sql, conn)

    return StructuredTool.from_function(
        func=run_sql,
        name="run_sql",
        description=(
            "对 cases / events 两张只读表执行一条 SELECT 查询（可以 JOIN、GROUP BY、"
            "聚合函数），返回 columns/rows。只在预置指标快照和 run_metric_query 都答不了"
            "时使用——例如需要多表 JOIN、复杂条件组合、或大范围聚合。只允许 SELECT，"
            "只能查询这两张表，会被确定性护栏拦截其它请求。"
        ),
    )
