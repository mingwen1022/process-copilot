from __future__ import annotations

from typing import Any

from app.agents.analytics_query_agent import AnalyticsQueryAgent, AnalyticsQueryProposal
from app.analytics.query import AdHocMetricQuery, MetricFilter


def _snapshot() -> dict[str, Any]:
    return {
        "process_name": "员工请假申请流程",
        "overview": {"case_count": 600, "avg_case_duration_hours": 76.1},
        "node_metrics": {"line_leader": {"avg_dwell_hours": 116.6}},
        "path_metrics": {"rework_rate": 0.42, "conformance_violations": [{"case_id": "x"}]},
    }


class FakeStructuredRunner:
    def __init__(self, response: dict[str, Any]) -> None:
        self.response = response
        self.messages: list[list[dict[str, str]]] = []

    def invoke(self, messages: list[dict[str, str]]) -> dict[str, Any]:
        self.messages.append(messages)
        return self.response


class FakeStructuredModel:
    def __init__(self, response: dict[str, Any]) -> None:
        self.runner = FakeStructuredRunner(response)

    def with_structured_output(self, schema: Any, **kwargs: Any) -> FakeStructuredRunner:
        return self.runner


class LegacyTextModel:
    def generate(self, system_prompt: str, user_prompt: str) -> str:
        return "legacy"


def test_empty_message_short_circuits() -> None:
    model = FakeStructuredModel({"parsed": None, "parsing_error": None})
    agent = AnalyticsQueryAgent(model=model)
    proposal = agent.run(message="  ", metrics_snapshot=_snapshot())
    assert proposal.queries == []
    assert model.runner.messages == []


def test_legacy_model_degrades() -> None:
    agent = AnalyticsQueryAgent(model=LegacyTextModel())
    proposal = agent.run(message="病假超过3天的有几个", metrics_snapshot=_snapshot())
    assert proposal.queries == []
    assert "不可用" in proposal.reply


def test_pure_question_returns_no_queries() -> None:
    proposal = AnalyticsQueryProposal(reply="条线分管领导审批平均耗时约 116.6 小时。", queries=[])
    model = FakeStructuredModel({"parsed": proposal, "parsing_error": None})
    agent = AnalyticsQueryAgent(model=model)
    result = agent.run(message="条线分管领导审批多慢", metrics_snapshot=_snapshot())
    assert result.queries == []
    assert "116.6" in result.reply


def test_new_metric_query_is_returned() -> None:
    proposal = AnalyticsQueryProposal(
        reply="这就为你统计病假超过3天的案例数。",
        queries=[AdHocMetricQuery(
            name="病假超3天案例数", scope="case",
            filters=[MetricFilter(field="请假类型", operator="=", value="病假"), MetricFilter(field="请假天数", operator=">", value=3)],
            aggregate="count",
        )],
    )
    model = FakeStructuredModel({"parsed": proposal, "parsing_error": None})
    agent = AnalyticsQueryAgent(model=model)
    result = agent.run(message="病假超过3天的有几个", metrics_snapshot=_snapshot())
    assert len(result.queries) == 1
    assert result.queries[0].aggregate == "count"


def test_out_of_scope_clears_queries() -> None:
    proposal = AnalyticsQueryProposal(
        reply="这超出了当前流程分析范围。",
        queries=[AdHocMetricQuery(name="x", scope="case", filters=[], aggregate="count")],
        out_of_scope=True,
    )
    model = FakeStructuredModel({"parsed": proposal, "parsing_error": None})
    agent = AnalyticsQueryAgent(model=model)
    result = agent.run(message="帮我改一下报销流程", metrics_snapshot=_snapshot())
    assert result.out_of_scope is True
    assert result.queries == []


def test_parsing_error_degrades_to_didnt_understand() -> None:
    model = FakeStructuredModel({"parsed": None, "parsing_error": "boom"})
    agent = AnalyticsQueryAgent(model=model)
    result = agent.run(message="???", metrics_snapshot=_snapshot())
    assert result.queries == []
    assert "没能理解" in result.reply


# ── run_with_tools（L2 typed / L3 SQL 工具循环）──────────────────────

def test_run_with_tools_empty_message_short_circuits() -> None:
    model = FakeStructuredModel({"parsed": None, "parsing_error": None})
    agent = AnalyticsQueryAgent(model=model)
    result = agent.run_with_tools(message="  ", metrics_snapshot=_snapshot(), schema_ddl="表 cases: ...", tools=[])
    assert result.tool_calls == []
    assert "请输入" in result.reply


def test_run_with_tools_legacy_model_degrades() -> None:
    agent = AnalyticsQueryAgent(model=LegacyTextModel())
    result = agent.run_with_tools(
        message="各部门平均办结时长", metrics_snapshot=_snapshot(), schema_ddl="表 cases: ...", tools=[]
    )
    assert result.tool_calls == []
    assert "不可用" in result.reply


def test_extract_tool_run_result_parses_zero_tool_call_reply() -> None:
    from langchain_core.messages import AIMessage

    from app.agents.analytics_query_agent import _extract_tool_run_result

    response = {"messages": [AIMessage(content="退回率约为 42%。")]}
    result = _extract_tool_run_result(response)
    assert result.tool_calls == []
    assert "42%" in result.reply


def test_extract_tool_run_result_pairs_tool_call_with_its_result() -> None:
    from langchain_core.messages import AIMessage, ToolMessage

    from app.agents.analytics_query_agent import _extract_tool_run_result

    response = {
        "messages": [
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "run_sql",
                        "args": {"sql": "SELECT COUNT(*) FROM cases"},
                        "id": "call_1",
                        "type": "tool_call",
                    }
                ],
            ),
            ToolMessage(
                content='{"columns": ["n"], "rows": [[7]]}', tool_call_id="call_1", name="run_sql"
            ),
            AIMessage(content="共有 7 个案例。"),
        ]
    }
    result = _extract_tool_run_result(response)
    assert len(result.tool_calls) == 1
    assert result.tool_calls[0].tool == "run_sql"
    assert result.tool_calls[0].args == {"sql": "SELECT COUNT(*) FROM cases"}
    assert result.tool_calls[0].result == {"columns": ["n"], "rows": [[7]]}
    assert "7 个案例" in result.reply


def test_extract_tool_run_result_surfaces_guard_rejection_as_dict() -> None:
    from langchain_core.messages import AIMessage, ToolMessage

    from app.agents.analytics_query_agent import _extract_tool_run_result

    response = {
        "messages": [
            AIMessage(
                content="",
                tool_calls=[
                    {"name": "run_sql", "args": {"sql": "DROP TABLE cases"}, "id": "call_1", "type": "tool_call"}
                ],
            ),
            ToolMessage(
                content='{"error": "只允许 SELECT 查询，检测到 Drop", "sql": "DROP TABLE cases"}',
                tool_call_id="call_1",
                name="run_sql",
            ),
            AIMessage(content="这条查询超出了允许范围，我拦下了它。"),
        ]
    }
    result = _extract_tool_run_result(response)
    assert result.tool_calls[0].result["error"].startswith("只允许 SELECT")
    assert "拦下" in result.reply


def test_extract_tool_run_result_falls_back_when_no_final_text() -> None:
    from app.agents.analytics_query_agent import _extract_tool_run_result

    result = _extract_tool_run_result({"messages": []})
    assert result.tool_calls == []
    assert result.reply  # 有兜底文案，不是空字符串
