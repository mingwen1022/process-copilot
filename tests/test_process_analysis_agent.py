from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from app.agents.process_analysis_agent import (
    DiagnosisAttributionOutput,
    ProcessAnalysisAgent,
)


def _metrics_with_two_candidates() -> dict[str, Any]:
    return {
        "process_name": "员工请假申请流程",
        "overview": {"case_count": 100, "manual_intervention_case_ratio": 0.0, "manual_intervention_case_count": 0},
        "node_metrics": {
            "draft": {"avg_dwell_hours": 0.2, "return_rate": 0.0, "avg_visits_per_case": 1.0},
            "dept_supervisor": {"avg_dwell_hours": 5.0, "return_rate": 0.4, "sla_achievement_rate": 1.0, "avg_visits_per_case": 1.6},
            "dept_gm": {"avg_dwell_hours": 8.0, "return_rate": 0.05, "sla_achievement_rate": 0.95, "avg_visits_per_case": 1.0},
            "line_leader": {"avg_dwell_hours": 120.0, "return_rate": 0.05, "sla_achievement_rate": 0.9, "avg_visits_per_case": 1.0},
        },
        "path_metrics": {"rework_rate": 0.05, "rework_case_count": 5, "conformance_violation_count": 0},
        "manual_intervention": {"by_prior_node": {}},
        "node_names": {
            "draft": "起草",
            "dept_supervisor": "部门主管审批",
            "dept_gm": "部门总经理审批",
            "line_leader": "条线分管领导审批",
        },
    }


def _healthy_metrics() -> dict[str, Any]:
    return {
        "process_name": "员工请假申请流程",
        "overview": {"case_count": 100, "manual_intervention_case_ratio": 0.0, "manual_intervention_case_count": 0},
        "node_metrics": {
            "dept_supervisor": {"avg_dwell_hours": 5.0, "return_rate": 0.05, "sla_achievement_rate": 1.0, "avg_visits_per_case": 1.0},
        },
        "path_metrics": {"rework_rate": 0.05, "rework_case_count": 5, "conformance_violation_count": 0},
        "manual_intervention": {"by_prior_node": {}},
        "node_names": {"dept_supervisor": "部门主管审批"},
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


class FakeKnowledgeIndex:
    """假知识库（不打 Bedrock）：返回预设 hits。hits 为空即"诚实空"。"""

    def __init__(self, hits=None) -> None:
        from app.rag.index import SemanticHit

        self._hits = [
            SemanticHit(kind=h.get("kind", "rule"), text=h["text"], metadata=h.get("metadata", {}), score=h.get("score", 0.5))
            for h in (hits or [])
        ]
        self.queries: list[str] = []

    def semantic_search(self, query, *, k=6, **kwargs):
        self.queries.append(query)
        return self._hits


def test_no_candidates_returns_healthy_report_without_calling_model() -> None:
    model = FakeStructuredModel({"parsed": None, "parsing_error": None})
    agent = ProcessAnalysisAgent(model=model)
    report = agent.diagnose(_healthy_metrics())
    assert report.candidate_count == 0
    assert report.items == []
    assert "正常区间" in report.summary
    assert model.runner.messages == []  # 没候选就不该调用模型


def test_legacy_model_degrades_to_candidates_without_attribution() -> None:
    agent = ProcessAnalysisAgent(model=LegacyTextModel())
    report = agent.diagnose(_metrics_with_two_candidates())
    assert report.llm_available is False
    assert report.candidate_count == len(report.items) >= 2
    # 确定性字段在，但没有 LLM 文案
    for item in report.items:
        assert item.description
        assert item.severity in {"high", "medium"}
        assert item.root_cause == ""
        assert item.suggestion == ""


def test_llm_attributions_merged_onto_candidates_by_id() -> None:
    metrics = _metrics_with_two_candidates()
    # 先确定性算出候选，拿到真实的 bottleneck_id
    from app.analytics.thresholds import detect_candidates

    candidate_ids = [c.bottleneck_id for c in detect_candidates(metrics)]
    output = DiagnosisAttributionOutput(
        summary="该流程主要问题是条线分管领导审批耗时过长与部门主管退回率偏高。",
        attributions=[
            {
                "bottleneck_id": candidate_ids[0],
                "root_cause": "成因A",
                "suggestion": "建议A",
                "suggested_process_edit_instruction": "给条线分管领导审批环节增加3天处理时限",
            },
            {"bottleneck_id": candidate_ids[1], "root_cause": "成因B", "suggestion": "建议B"},
        ],
    )
    model = FakeStructuredModel({"parsed": output, "parsing_error": None})
    agent = ProcessAnalysisAgent(model=model, knowledge_index=FakeKnowledgeIndex())
    report = agent.diagnose(metrics)

    assert report.llm_available is True
    assert report.summary.startswith("该流程主要问题")
    by_id = {item.bottleneck_id: item for item in report.items}
    assert by_id[candidate_ids[0]].root_cause == "成因A"
    assert by_id[candidate_ids[0]].suggested_process_edit_instruction == "给条线分管领导审批环节增加3天处理时限"
    assert by_id[candidate_ids[1]].suggestion == "建议B"
    assert by_id[candidate_ids[1]].suggested_process_edit_instruction is None
    # severity 仍来自确定性判定，未被 LLM 改动
    for item in report.items:
        assert item.severity in {"high", "medium"}


def test_candidate_without_matching_attribution_keeps_empty_text() -> None:
    metrics = _metrics_with_two_candidates()
    output = DiagnosisAttributionOutput(summary="只覆盖了一个候选。", attributions=[])
    model = FakeStructuredModel({"parsed": output, "parsing_error": None})
    agent = ProcessAnalysisAgent(model=model, knowledge_index=FakeKnowledgeIndex())
    report = agent.diagnose(metrics)
    assert report.llm_available is True
    for item in report.items:
        assert item.root_cause == ""  # LLM 没给就留空，不编


def test_parsing_error_degrades_to_candidates_without_attribution() -> None:
    model = FakeStructuredModel({"parsed": None, "parsing_error": "boom"})
    agent = ProcessAnalysisAgent(model=model, knowledge_index=FakeKnowledgeIndex())
    report = agent.diagnose(_metrics_with_two_candidates())
    assert report.llm_available is True
    assert report.items and all(item.root_cause == "" for item in report.items)


def test_retrieved_knowledge_is_injected_into_prompt_and_report() -> None:
    metrics = _metrics_with_two_candidates()
    output = DiagnosisAttributionOutput(summary="s", attributions=[])
    model = FakeStructuredModel({"parsed": output, "parsing_error": None})
    index = FakeKnowledgeIndex([
        {"kind": "rule", "text": "条线分管领导审批处理时限3个工作日。", "metadata": {"rule_id": "ops.sla_line_leader"}},
    ])
    agent = ProcessAnalysisAgent(model=model, knowledge_index=index)
    report = agent.diagnose(metrics)

    # 检索到的知识进了报告
    assert any("处理时限" in k.text for k in report.knowledge_context)
    assert report.knowledge_context[0].source == "ops.sla_line_leader"
    # 也注入了 prompt（retrieve-then-read，不是让 LLM 自己调工具）
    user_msg = model.runner.messages[0][1]["content"]
    assert "参考知识" in user_msg and "处理时限" in user_msg
    # 检索 query 里带了堵点描述
    assert index.queries and "员工请假申请流程" in index.queries[0]


def test_honest_empty_when_no_relevant_knowledge() -> None:
    metrics = _metrics_with_two_candidates()
    output = DiagnosisAttributionOutput(summary="s", attributions=[])
    model = FakeStructuredModel({"parsed": output, "parsing_error": None})
    agent = ProcessAnalysisAgent(model=model, knowledge_index=FakeKnowledgeIndex([]))  # 检索为空
    report = agent.diagnose(metrics)
    assert report.knowledge_context == []
    user_msg = model.runner.messages[0][1]["content"]
    assert "未检索到" in user_msg  # prompt 明确告知无依据、约束不编造


# ——— stringified-attributions 修复（跟 compliance_judge_agent 同一类真实踩过的 Bedrock 畸形） ———

class _SequentialStructuredRunner:
    def __init__(self, responses: list[dict[str, Any]]) -> None:
        self.responses = responses
        self.calls: list[list[dict[str, str]]] = []

    def invoke(self, messages: list[dict[str, str]]) -> dict[str, Any]:
        self.calls.append(messages)
        index = min(len(self.calls) - 1, len(self.responses) - 1)
        return self.responses[index]


class _SequentialModel:
    def __init__(self, responses: list[dict[str, Any]]) -> None:
        self.runner = _SequentialStructuredRunner(responses)

    def with_structured_output(self, schema: Any, **kwargs: Any) -> _SequentialStructuredRunner:
        return self.runner


def _tool_call_response(args: dict[str, Any], *, parsed: Any = None, parsing_error: Any = None) -> dict[str, Any]:
    raw = SimpleNamespace(tool_calls=[{"name": "DiagnosisAttributionOutput", "args": args, "id": "1"}])
    return {"raw": raw, "parsed": parsed, "parsing_error": parsing_error}


def test_diagnose_repairs_stringified_attributions_deterministically_without_second_call() -> None:
    """真实踩过的失败模式：attributions 被模型整体编码成一段字符串而不是原生数组，但那段
    字符串本身恰好是合法 JSON——应该纯确定性修好，不用再多打一次 LLM。"""
    metrics = _metrics_with_two_candidates()
    from app.analytics.thresholds import detect_candidates

    candidate_ids = [c.bottleneck_id for c in detect_candidates(metrics)]
    attributions_json = (
        f'[{{"bottleneck_id": "{candidate_ids[0]}", "root_cause": "成因A", "suggestion": "建议A"}}]'
    )
    response = _tool_call_response(
        {"summary": "整体偏慢", "attributions": attributions_json}, parsed=None, parsing_error="mocked schema error",
    )
    model = _SequentialModel([response])
    agent = ProcessAnalysisAgent(model=model, knowledge_index=FakeKnowledgeIndex())
    report = agent.diagnose(metrics)
    assert report.summary == "整体偏慢"
    by_id = {item.bottleneck_id: item for item in report.items}
    assert by_id[candidate_ids[0]].root_cause == "成因A"
    assert len(model.runner.calls) == 1  # 确定性修复成功，不该触发 retry


def test_diagnose_falls_back_to_retry_when_stringified_attributions_is_malformed_json() -> None:
    """字符串本身就是畸形 JSON 时，纯 json.loads 修不好，要退一步让模型自己修一次。"""
    metrics = _metrics_with_two_candidates()
    broken_json = '[{"bottleneck_id": "x", "root_cause": "包含"未转义"引号", "suggestion": "s"}]'
    first = _tool_call_response({"summary": "s", "attributions": broken_json}, parsed=None, parsing_error="mocked schema error")
    repaired_output = DiagnosisAttributionOutput(summary="修复后的总结", attributions=[])
    second = {"raw": None, "parsed": repaired_output, "parsing_error": None}

    model = _SequentialModel([first, second])
    agent = ProcessAnalysisAgent(model=model, knowledge_index=FakeKnowledgeIndex())
    report = agent.diagnose(metrics)
    assert report.summary == "修复后的总结"
    assert len(model.runner.calls) == 2  # 第一次失败、第二次修复轮成功
    repair_user_msg = model.runner.calls[1][1]["content"]
    assert "mocked schema error" in repair_user_msg
    assert "attributions" in repair_user_msg


def test_diagnose_degrades_to_empty_summary_when_both_repair_attempts_fail() -> None:
    metrics = _metrics_with_two_candidates()
    broken_json = '[{"bottleneck_id": "x"'  # 连修复轮也解析不出来
    first = _tool_call_response({"summary": "s", "attributions": broken_json}, parsed=None, parsing_error="mocked schema error")
    second = _tool_call_response({"summary": "s", "attributions": broken_json}, parsed=None, parsing_error="still broken")
    model = _SequentialModel([first, second])
    agent = ProcessAnalysisAgent(model=model, knowledge_index=FakeKnowledgeIndex())
    report = agent.diagnose(metrics)
    assert report.summary == ""  # 两次都失败，如实降级为空，不编
    assert len(model.runner.calls) == 2
