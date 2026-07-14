from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from app.agents.compliance_judge_agent import ComplianceJudgeAgent
from data.schema import AtomicRule, ProcessDefinition


def _rule(rule_id: str, check_type: str = "llm_judge") -> AtomicRule:
    return AtomicRule.model_validate({
        "rule_id": rule_id,
        "dimension": "design_standard",
        "statement": "测试定性规则",
        "applies_to_processes": [],
        "applies_to_domains": ["design"],
        "applicability_condition": None,
        "check_type": check_type,
        "requirement": None,
        "provenance": {"source_doc": "doc", "clause": "x"},
        "severity": "medium",
    })


def _process() -> ProcessDefinition:
    return ProcessDefinition.model_validate({
        "meta": {"process_id": "P", "process_name": "测试流程", "version": "V1", "responsible_dept": "d", "description": "x", "applicant_scope": "全员", "entry_point": "OA"},
        "form_fields": [],
        "flow_nodes": [{"node_id": "draft", "node_name": "起草", "is_draft": True, "handler": None, "opinion": None,
                         "opinion_label": None, "time_limit_days": None, "submit_paths": []}],
        "attachments": None,
        "roles": None,
    })


class _FakeStructuredRunner:
    """镜像 test_design_edit_agent.py 的假 structured model——按调用顺序弹出预设响应。"""

    def __init__(self, responses: list[dict[str, Any]]) -> None:
        self.responses = responses
        self.calls: list[list[dict[str, str]]] = []

    def invoke(self, messages: list[dict[str, str]]) -> dict[str, Any]:
        self.calls.append(messages)
        index = min(len(self.calls) - 1, len(self.responses) - 1)
        return self.responses[index]


class _FakeModel:
    def __init__(self, responses: list[dict[str, Any]]) -> None:
        self.runner = _FakeStructuredRunner(responses)

    def with_structured_output(self, schema: Any, **kwargs: Any) -> _FakeStructuredRunner:
        return self.runner


def _tool_call_response(args: dict[str, Any], *, parsed: Any = None, parsing_error: Any = None) -> dict[str, Any]:
    raw = SimpleNamespace(tool_calls=[{"name": "ComplianceJudgeOutput", "args": args, "id": "1"}])
    return {"raw": raw, "parsed": parsed, "parsing_error": parsing_error}


def _agent(responses: list[dict[str, Any]]) -> tuple[ComplianceJudgeAgent, _FakeStructuredRunner]:
    model = _FakeModel(responses)
    agent = ComplianceJudgeAgent(model=model)
    return agent, model.runner


def test_judge_returns_empty_without_calling_model_when_no_qualitative_rules() -> None:
    agent, runner = _agent([])
    verdicts = agent.judge(_process(), [_rule("r1", check_type="deterministic")])
    assert verdicts == []
    assert runner.calls == []  # 没有定性规则时不该调模型


def test_judge_returns_verdicts_from_well_formed_structured_output() -> None:
    from app.agents.compliance_judge_agent import ComplianceJudgeOutput, RuleVerdict, RuleViolation

    parsed = ComplianceJudgeOutput(verdicts=[
        RuleVerdict(rule_id="design.x", violations=[RuleViolation(node_id="n1", detail="不符合")])
    ])
    agent, runner = _agent([{"raw": None, "parsed": parsed, "parsing_error": None}])
    verdicts = agent.judge(_process(), [_rule("design.x")])
    assert len(verdicts) == 1 and verdicts[0].compliant is False
    assert len(runner.calls) == 1  # 一次成功，不需要修复


def test_judge_repairs_stringified_verdicts_deterministically_without_second_call() -> None:
    """真实踩过的失败模式：verdicts 被模型整体编码成一段字符串而不是原生数组，但那段
    字符串本身恰好是合法 JSON——这种应该纯确定性修好，不用再多打一次 LLM。"""
    verdicts_json = '[{"rule_id": "design.x", "violations": [{"node_id": "n1", "detail": "命名缺动作词"}]}]'
    response = _tool_call_response({"verdicts": verdicts_json}, parsed=None, parsing_error="mocked schema error")
    agent, runner = _agent([response])
    verdicts = agent.judge(_process(), [_rule("design.x")])
    assert len(verdicts) == 1
    assert verdicts[0].violations[0].detail == "命名缺动作词"
    assert len(runner.calls) == 1  # 确定性修复成功，不该触发 retry


def test_judge_falls_back_to_retry_when_stringified_verdicts_is_malformed_json() -> None:
    """字符串本身就是畸形 JSON（如内部双引号未转义）时，纯 json.loads 修不好，
    要退一步让模型自己修一次——这次修复轮返回合法结果。"""
    broken_json = '[{"rule_id": "design.x", "violations": [{"node_id": "n1", "detail": "包含"未转义"引号"}]}]'
    first = _tool_call_response({"verdicts": broken_json}, parsed=None, parsing_error="mocked schema error")

    from app.agents.compliance_judge_agent import ComplianceJudgeOutput, RuleVerdict, RuleViolation

    repaired_output = ComplianceJudgeOutput(verdicts=[
        RuleVerdict(rule_id="design.x", violations=[RuleViolation(node_id="n1", detail="修复后的理由")])
    ])
    second = {"raw": None, "parsed": repaired_output, "parsing_error": None}

    agent, runner = _agent([first, second])
    verdicts = agent.judge(_process(), [_rule("design.x")])
    assert len(verdicts) == 1 and verdicts[0].violations[0].detail == "修复后的理由"
    assert len(runner.calls) == 2  # 第一次失败、第二次修复轮成功
    # 修复轮的 prompt 里要带上原始错误信息和原始输出，不能凭空瞎修
    repair_user_msg = runner.calls[1][1]["content"]
    assert "mocked schema error" in repair_user_msg
    assert "verdicts" in repair_user_msg


def test_judge_returns_empty_when_both_attempts_fail() -> None:
    broken_json = '[{"rule_id": "design.x", "violations": [{"node_id": "n1"'  # 连修复轮也解析不出来
    first = _tool_call_response({"verdicts": broken_json}, parsed=None, parsing_error="mocked schema error")
    second = _tool_call_response({"verdicts": broken_json}, parsed=None, parsing_error="still broken")
    agent, runner = _agent([first, second])
    verdicts = agent.judge(_process(), [_rule("design.x")])
    assert verdicts == []
    assert len(runner.calls) == 2  # 两次都试过了，不是没试就放弃


def test_judge_filters_out_rule_ids_not_in_requested_set() -> None:
    """防止模型编造不在这轮请求范围内的 rule_id 混进结果——跟 EditProposal 那套
    "只信这轮真的给它看过的 id" 是同一个防线。"""
    from app.agents.compliance_judge_agent import ComplianceJudgeOutput, RuleVerdict, RuleViolation

    parsed = ComplianceJudgeOutput(verdicts=[
        RuleVerdict(rule_id="design.x", violations=[RuleViolation(node_id="n1", detail="真的不合规")]),
        RuleVerdict(rule_id="design.not_requested", violations=[RuleViolation(node_id="n1", detail="不该出现")]),
    ])
    agent, _runner = _agent([{"raw": None, "parsed": parsed, "parsing_error": None}])
    verdicts = agent.judge(_process(), [_rule("design.x")])
    assert [v.rule_id for v in verdicts] == ["design.x"]
