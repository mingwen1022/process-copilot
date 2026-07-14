"""typed 指标查询包成 tool（app.analytics.query.AdHocMetricQuery 的 tool-calling 包装）。

不是新写一遍查询逻辑——run_ad_hoc_query 本来就是"过滤+聚合+可选分组"这一种
查询形状，可穷举测试、错不了。这里只是让 agent 能在 ReAct 循环里主动调用它，
而不是像原来那样在一次结构化输出里被动吐出来、看不到结果、不能追问。

这是取数梯度里的 L2：比直接读快照（L1，已在 system prompt 里）贵一次调用，
但仍然是类型化、不会凭空编数字的答案；比 run_sql（L3，见 sql_query_tool.py）
更安全、更便宜，agent 应该优先尝试这个，不够表达才升级到 SQL——这条顺序写在
system prompt 里，不是这里的职责。persist（转常态化指标）作为这个 tool 的一个
参数，不单独设计一套机制。
"""

from __future__ import annotations

import json
from typing import Any

from langchain_core.tools import StructuredTool

from app.analytics.query import AdHocMetricQuery, run_ad_hoc_query, upsert_custom_metric
from data.schema import CaseRecord


def build_run_metric_query_tool(
    cases: list[CaseRecord], *, custom_metrics_path: str | None = None
) -> StructuredTool:
    def run_metric_query(query_json: str) -> str:
        try:
            query = AdHocMetricQuery.model_validate_json(query_json)
        except Exception as exc:  # noqa: BLE001 - 结构化错误交给 agent 看着改写
            return json.dumps(
                {"error": f"query_json 不符合 AdHocMetricQuery schema：{exc}"}, ensure_ascii=False
            )

        result = run_ad_hoc_query(query, cases)
        payload = result.model_dump(mode="json")
        if query.persist and custom_metrics_path:
            upsert_custom_metric(custom_metrics_path, query)
            payload["persisted"] = True
        return json.dumps(payload, ensure_ascii=False)

    return StructuredTool.from_function(
        func=run_metric_query,
        name="run_metric_query",
        description=(
            "对当前流程的 case/event 记录执行一条类型化的过滤+聚合查询（不是 SQL）。"
            "参数 query_json 是一个 JSON 字符串，字段为：name（指标中文名）、"
            "scope（case 或 event）、filters（[{field, operator, value}] 列表，"
            "operator 可选 = != > >= < <= in）、aggregate（count/rate/avg_dwell_hours）、"
            "group_by（可选分组字段）、persist（是否转为常态化指标，默认 false）。"
            "这是安全、可穷举测试的查询方式，优先于 run_sql 使用；只有它表达不了的"
            "需求（多表 JOIN、复杂条件组合）才用 run_sql。"
        ),
    )
