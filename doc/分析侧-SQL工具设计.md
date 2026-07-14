# 分析侧 · SQL 工具（tool-calling 镜头）设计方案

## 落地状态：已完成

与本文档原方案的三处出入（均为用户拍板的调整，实现已按调整版落地）：

1. **存储选 SQLite，不是 DuckDB**——§2 原方案权衡的是"大数据秒回"的演示效果；
   拍板后改为 SQLite（标准库自带，少一个依赖），"大数据"镜头改用更保守的行数
   （几万级）而不是几十万级去演示，叙事从"极速"调整为"超出上下文容量也能
   按需取"，见 §7 demo 脚本已按此口径调整。
2. **typed 查询也包成了一个 tool（`run_metric_query`），不是留作背景机制**——
   原方案是"service 层预判要不要走 SQL"，拍板后改为**两个 tool 都给 agent**、
   由它自己按"先便宜安全、不够再升级"的顺序判断调用哪个（甚至可以先调
   typed、发现不够再追加调 SQL）。这比原方案更好：把"要不要用工具、用哪个"
   也变成了工具选择的判断力展示，而不只是"会不会写 SQL"；顺带也补上了旧版
   `AdHocMetricQuery` 单次结构化输出"看不到结果、不能追问"的老毛病。
3. **落地文件跟本方案的路径规划基本一致**，实际文件见
   `doc/agent架构与harness.md` §3.2（指标问答 Agent）——完整接口/prompt/护栏
   细节以那份文档为准，本文档保留作设计动机与取舍的历史记录。

**实现过程中发现并修复的一个真实 bug**（非设计问题，是 sqlite3 标准库的线程
默认行为）：`create_agent` 的工具执行会被 LangGraph/FastAPI 派发到跟建库不同
的线程，sqlite3 默认连接按创建线程绑定，跨线程直接用会抛
`SQLite objects created in a thread can only be used in that same thread`。
修法是建只读连接时传 `check_same_thread=False`（单请求内只有一个调用方在用
这个连接，没有真实并发竞争，禁用同线程检查是安全的）。已补回归测试
`tests/test_analytics_sql_store.py::test_connection_usable_from_a_different_thread`。

四个 demo 镜头（§7）、三层防御（§4）、护栏"管安全不管正确"的边界（§1）均按
原方案实现，未变。

---

## 0. 一句话定位

给「指标问答 agent」加**一个** tool——`run_sql`，让它在快照答不了、或数据大到不该全量预算时，
自己写 SQL 取数；一层**确定性守卫**保证这条 SQL 只读、只碰白名单表、限行限时。

这是全项目第一个"agent 自主决定何时调工具"的**产品内**镜头（产物生成那个 ReAct agent 未接线）。

---

## 1. 先回答一个自我矛盾

`app/analytics/query.py` 开头写得很明白，当初**故意不做 SQL agent**：

> LLM 现写 SQL 会"错得很安静"——算错的数字看起来和算对的一样权威，用户没有对照物。

这个顾虑今天依然成立，所以本方案**不是推翻它，是给它加一条受控的逃生舱**，三条约束保住原则：

1. **typed 查询仍是默认主路径**。`AdHocMetricQuery` 那套（可穷举测试、错不了）不动，继续接
   80% 的常见问题。`run_sql` 只在快照 + typed 查询都覆盖不到的长尾/大数据下ча启用。
2. **数字的可信来源是渲染卡片，不是 agent 的话**。`run_sql` 返回结构化 rows，前端渲染成
   「确定统计」卡片（复用现有 `query_results` 卡片），SQL 原文一并展示。agent 的自然语言只是
   框注，用户信的是卡片里那张表 + 那条 SQL，不是 agent 的转述——这就是"对照物"。
3. **守卫管安全、不管正确**。守卫拦的是"碰了不该碰的表/写库/拖垮库"，拦不了"JOIN 错了"。
   这条边界在 demo 讲解里**主动说出来**（见 §7），承认它比假装它全能更有说服力。

---

## 2. 数据地基：内存 DuckDB（两张表）

分析数据现在是 `data/analytics/leave_request/` 的事件日志，`load_case_records` 现算，**不在可查表里**。
先补地基：

- 新文件 `app/analytics/sql_store.py`：
  - `build_store(cases: list[CaseRecord]) -> Path`
    用现成的 `_case_rows(cases)` / `_event_rows(cases)`（[query.py:59/91](app/analytics/query.py#L59)）拍平，
    灌进 DuckDB，建两张表 → 落成一个**临时 `.duckdb` 文件**。
    - `cases`：case_id, case_status, flow_code, flow_name, initiator_dept_name,
      duration_hours, node_visits, is_completed, has_return, has_manual_intervention, 请假类型, 请假天数 …
      （`node_id`/`node_name` 列表列不进 SQL 表，下钻环节走 events 表 JOIN，更干净）
    - `events`：case_id, node_id, node_name, action, action_category, resource_dept_name,
      task_order, dwell_hours, is_ops, 请假类型, 请假天数 …
  - `open_readonly(path) -> duckdb.Connection`
    用 `duckdb.connect(path, read_only=True)` 打开——**真只读连接**，纵深防御的第二道（就算解析器漏了也写不动）。
- **为什么 DuckDB 不是 SQLite**：列式、聚合快、直接读 Parquet/CSV。"大数据"这个卖点要可信——
  丢 50 万行合成事件进去，`GROUP BY dept` 秒回，才撑得起"全量预算塞不下、必须按需查"的叙事。
- **大数据脚本**：`scripts/gen_big_analytics.py`（复用现有 `event_log_generator`），
  生成 N 档（1k / 50k / 500k）合成 case，demo 现场切档位。

---

## 3. 工具：`run_sql`

`app/tools/sql_query_tool.py`

```python
ALLOWED_TABLES = {"cases", "events"}
ROW_CAP = 200          # 返回给 agent 的行上限
TIMEOUT_MS = 3000

def run_sql(sql: str, *, conn) -> dict:
    ok, err = _guard(sql)                 # §4，不过守卫直接拒
    if not ok:
        return {"error": err}             # 结构化错误，agent 会据此改写或放弃
    try:
        rows, cols, truncated = _exec_readonly(sql, conn, ROW_CAP, TIMEOUT_MS)
    except Exception as e:
        return {"error": f"执行失败：{e}"}
    return {"columns": cols, "rows": rows, "truncated": truncated, "sql": sql}
```

- 用 `langchain_core.tools.StructuredTool.from_function` 包装（对齐现有 artifact_tools 的写法）。
- 返回体里带回 `sql` 原文——供前端卡片展示、供 demo 透明。

---

## 4. 守卫 `_guard(sql)` —— 本方案的判断力所在

**用解析器，不用正则**（子查询/CTE/JOIN/别名都能把表名藏起来）。依赖 `sqlglot`：

```python
import sqlglot
from sqlglot import exp

def _guard(sql: str) -> tuple[bool, str | None]:
    try:
        stmts = sqlglot.parse(sql, read="duckdb")
    except Exception:
        return False, "SQL 无法解析"
    if len(stmts) != 1:                                   # 单语句，防 ; 拼接
        return False, "只允许单条语句"
    root = stmts[0]
    if not isinstance(root, exp.Select):                  # 只允许 SELECT
        return False, "只允许 SELECT 查询"
    # 显式封杀危险节点：写库 / DDL / ATTACH / PRAGMA / COPY / 读文件函数
    for node in root.walk():
        if isinstance(node, (exp.Insert, exp.Update, exp.Delete, exp.Create,
                             exp.Drop, exp.Alter, exp.Command, exp.Attach, exp.Pragma)):
            return False, "只允许只读查询"
    # 表引用 ⊆ 白名单
    for t in root.find_all(exp.Table):
        if t.name.lower() not in ALLOWED_TABLES:
            return False, f"表 {t.name} 不在允许范围（仅 cases / events）"
    return True, None
```

**纵深防御四道**：① 解析器只放 SELECT + 白名单表 → ② 只读连接（写不动）→
③ 行数上限（`fetchmany(ROW_CAP)`，不注入 LIMIT 以免改语义）→ ④ 语句超时。

> `exp.Attach` 那条最关键——`ATTACH DATABASE` 是嵌入式库最经典的沙箱逃逸口，必须显式封。

---

## 5. 问答 agent 循环改造

现状 `AnalyticsQueryAgent.run()` 是**单次结构化输出**。改造成**有界 ReAct 循环**（保留 typed 主路径）：

- 新增 `AnalyticsQueryAgent.run_with_tools(message, snapshot, schema_ddl, history)`：
  - 用 `langchain.agents.create_agent(model, tools=[run_sql_tool], system_prompt=...)`（对齐 artifact_generation）。
  - **快照仍进 system context** → 简单问题 **0 次 tool call** 直接答（快路径不退化）。
  - system prompt 里贴两张表的 `schema_ddl`（列名 + 类型 + 一句用途），agent 才知道能查什么。
  - **步数上限**：`recursion_limit` / 手动 `max_tool_calls=3`——防跑飞、封顶 token。
  - 出参在现有 `{reply, out_of_scope, query_results, persisted_metric_names}` 基础上加
    `sql_runs: [{sql, columns, rows, truncated}]`，供前端渲染卡片。
- **路由不变**：`AnalyticsService.answer_question` 仍是入口；内部先跑轻量分类，
  纯快照能答→走老单次路径（省一次循环开销），判为需要下钻/大数据→走 `run_with_tools`。
- **System prompt 要点**（新增）：
  > 你可用 `run_sql` 查询 cases / events 两张只读表。**先看快照能不能答，能就别查。**
  > 需要快照没有的切片/下钻/大范围聚合时才写 SQL。SQL 出错（返回 error）就据错误改写或如实说明查不了。
  > 只写 SELECT、只碰这两张表。**不要在文字里编造数字**——要报的数会以查询结果卡片呈现，你只做解读。

---

## 6. 接口 / 前端

- 后端：`POST /analytics/leave-request/ask` 出参加 `sql_runs`。
- 前端 `askAnalytics()`（app.html:936）：
  - agent 思考中 → 已有的动画点。
  - 有 `sql_runs` → 每条渲一张「确定统计」卡片：顶部折叠展示 SQL 原文（`<code>`），下面是结果表（截断标注 `已截断，仅显示前 200 行`）。
  - 守卫拒绝（error）→ 渲一条「已拦截」提示条（"该查询超出允许范围：仅 cases/events、只读"），**这条本身就是 demo 亮点**。

---

## 7. Demo 脚本（四个镜头，最后一个是 money shot）

1. **快路径**：问"请假流程的退回率是多少" → 快照里有 → **0 次 tool call** → 直接答。
   *讲解：常见问题不查库，确定性预置指标直接命中。*
2. **工具调用**：问"各部门里，请假超过10天但1天内就办完的，各有几个" → 快照没有 →
   屏幕跳 `🔧 run_sql` → 守卫通过 → 结果卡片（含 SQL 原文）。
   *讲解：长尾问题，agent 自己写 SQL，你看得见它查了什么。*
3. **大数据**：把表切到 50 万行，重问上一题 → 依然秒回。
   *讲解：这就是为什么不全量塞进上下文——大数据下按需查才可行。*
4. **护栏（money shot）**：诱导它 `DROP TABLE events` / 查一张不存在的 `salaries` 表 →
   守卫拦下 → 「已拦截」提示。
   *讲解：让 LLM 写 SQL 的前提是有确定性护栏——只读、白名单、限行限时；
   但护栏管的是安全，不是"这条 SQL 答得对不对"，所以 typed 预置指标仍是主路径，SQL 是长尾逃生舱。*

---

## 8. 工作量拆解

| # | 事项 | 文件 | 量级 |
|---|---|---|---|
| 1 | DuckDB 地基 + 只读连接 | `app/analytics/sql_store.py`（新） | 小，复用 `_case_rows`/`_event_rows` |
| 2 | `run_sql` 工具 + 守卫 | `app/tools/sql_query_tool.py`（新） | 中，守卫是核心 |
| 3 | 问答 agent ReAct 循环 | `app/agents/analytics_query_agent.py` | 中 |
| 4 | service 路由 + 出参 | `app/api/analytics_service.py` | 小 |
| 5 | 前端 SQL 卡片 + 拦截提示 | `app/static/app.html` | 小 |
| 6 | 大数据合成脚本 | `scripts/gen_big_analytics.py`（新） | 小 |
| 7 | 守卫单测（注入/逃逸用例） | `tests/test_sql_query_guard.py`（新） | 中，**必须**——守卫是安全边界，要有对抗用例 |
| 8 | 文档：给 `doc/agent架构与harness.md` 补一节 | 现有文档 | 小 |

新依赖：`duckdb`、`sqlglot`。

---

## 9. 一句话取舍复述

**做，但守住三条**：typed 查询仍是主路径（`run_sql` 只接长尾/大数据）；数字信卡片不信话术；
守卫用解析器 + 只读连接双保险，且在讲解里诚实划出"管安全不管正确"这条线。
这样这个 tool-call 镜头既有观赏性，又长在你"确定的事不交给 LLM"的哲学上，不是贴上去的。
