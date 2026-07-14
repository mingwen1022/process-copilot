from __future__ import annotations

from typing import Any

from data.schema import ProcessDefinition
from app.agents.design_edit_agent import DesignEditAgent, EditProposal, _render_open_insights
from app.tools.process_edit_tools import UpdateFormField, FormFieldUpdate


def _process() -> ProcessDefinition:
    return ProcessDefinition.model_validate(
        {
            "meta": {
                "process_id": "TEST-001",
                "process_name": "测试流程",
                "version": "V1.0.0",
                "responsible_dept": "测试部门",
                "description": "测试用流程",
                "applicant_scope": "全员",
                "entry_point": "OA系统",
            },
            "form_fields": [
                {
                    "seq": 1,
                    "field_name": "请假事由",
                    "required_stages": [],
                    "visible_stages": ["all"],
                    "editable_stages": ["draft"],
                    "component_type": "多行文本",
                    "logic_description": None,
                    "default_value": None,
                    "options": None,
                    "placeholder": None,
                    "max_length": None,
                }
            ],
            "flow_nodes": [
                {
                    "node_id": "draft",
                    "node_name": "起草",
                    "is_draft": True,
                    "handler": None,
                    "opinion": None,
                    "opinion_label": None,
                    "time_limit_days": None,
                    "submit_paths": [{"path_name": "送审批", "condition": None, "target_node_id": "approval"}],
                },
            ],
            "attachments": None,
            "roles": None,
        }
    )


class FakeStructuredRunner:
    def __init__(self, responses: list[dict[str, Any]]) -> None:
        self.responses = responses
        self.messages: list[list[dict[str, str]]] = []

    def invoke(self, messages: list[dict[str, str]]) -> dict[str, Any]:
        self.messages.append(messages)
        index = min(len(self.messages) - 1, len(self.responses) - 1)
        return self.responses[index]


class FakeStructuredModel:
    def __init__(self, responses: list[dict[str, Any]]) -> None:
        self.runner = FakeStructuredRunner(responses)
        self.structured_output_calls: list[dict[str, Any]] = []

    def with_structured_output(self, schema: Any, **kwargs: Any) -> FakeStructuredRunner:
        self.structured_output_calls.append({"schema": schema, "kwargs": kwargs})
        return self.runner


class LegacyTextModel:
    def generate(self, system_prompt: str, user_prompt: str) -> str:
        return "legacy"


def test_empty_instruction_short_circuits_without_calling_model() -> None:
    model = FakeStructuredModel([{"raw": "", "parsed": None, "parsing_error": None}])
    agent = DesignEditAgent(model=model)

    proposal = agent.run(process=_process(), instruction="   ")

    assert proposal.operations == []
    assert "没有新的设计指令" in proposal.reply
    assert model.runner.messages == []  # 没有真的调用模型


def test_legacy_model_degrades_gracefully() -> None:
    agent = DesignEditAgent(model=LegacyTextModel())

    proposal = agent.run(process=_process(), instruction="把请假事由改成必填")

    assert proposal.operations == []
    assert "不支持结构化编辑" in proposal.reply


def test_valid_edit_proposal_is_returned_as_is() -> None:
    proposal = EditProposal(
        reply="已把请假事由设为必填。",
        operations=[UpdateFormField(field_name="请假事由", updates=FormFieldUpdate(required_stages=["draft"]))],
        resolved_clarification_ids=[],
        out_of_scope=False,
    )
    model = FakeStructuredModel([{"raw": proposal.model_dump_json(), "parsed": proposal, "parsing_error": None}])
    agent = DesignEditAgent(model=model)

    result = agent.run(process=_process(), instruction="把请假事由改成必填")

    assert result.reply == "已把请假事由设为必填。"
    assert len(result.operations) == 1
    assert result.operations[0].op == "update_form_field"
    assert model.structured_output_calls[0]["kwargs"]["method"] == "function_calling"
    assert model.structured_output_calls[0]["kwargs"]["include_raw"] is True


def test_parsing_error_falls_back_to_could_not_understand() -> None:
    model = FakeStructuredModel([{"raw": "garbled", "parsed": None, "parsing_error": "boom"}])
    agent = DesignEditAgent(model=model)

    result = agent.run(process=_process(), instruction="做点什么")

    assert result.operations == []
    assert "没能理解" in result.reply


def test_out_of_scope_clears_operations_even_if_model_included_some() -> None:
    proposal = EditProposal(
        reply="这超出了当前流程的编辑范围。",
        operations=[UpdateFormField(field_name="请假事由", updates=FormFieldUpdate(required_stages=["draft"]))],
        out_of_scope=True,
    )
    model = FakeStructuredModel([{"raw": "", "parsed": proposal, "parsing_error": None}])
    agent = DesignEditAgent(model=model)

    result = agent.run(process=_process(), instruction="帮我把公司所有流程都发布上线")

    assert result.out_of_scope is True
    assert result.operations == []


def test_open_insights_render_empty_placeholder_when_none() -> None:
    assert "没有运行侧反哺的洞察" in _render_open_insights([])


def test_open_insights_render_includes_headline_and_blocked_path_reasons() -> None:
    """反哺闭环：渲染给 agent 看的文本必须带上具体证据（blocked_paths 的 reason），
    不能只有一句笼统的 headline——不然 agent 照样得反过来问用户细节，等于没传。"""
    text = _render_open_insights(
        [
            {
                "node_id": "dept_gm",
                "kind": "coverage_gap",
                "source": "ops",
                "severity": "high",
                "occurrences": 4,
                "window": "近30天",
                "headline": "「部门总经理审批」存在覆盖漏洞：某区间无审批路径，落进的申请卡住",
                "evidence": {
                    "blocked_paths": [
                        {"path_name": "直接结束", "target_node_id": "END", "reason": "请假天数=5 不满足 ≤3"},
                    ]
                },
            }
        ]
    )
    assert "dept_gm" in text
    assert "覆盖漏洞" in text
    assert "4" in text and "近30天" in text
    assert "请假天数=5 不满足 ≤3" in text  # 具体原因必须带上，不能只有 headline


def test_open_insights_render_includes_insight_id_so_llm_can_reference_it() -> None:
    """id 必须出现在渲染文本里——agent 靠这个 id 填 addressed_insight_ids，声明「这次改动
    回应了哪条洞察」；渲染不出 id，这个声明就没法回填到具体某一条上。"""
    text = _render_open_insights([{"insight_id": "insight_7", "node_id": "gm", "headline": "gm 慢"}])
    assert "insight_7" in text


def test_open_insights_are_included_in_user_prompt_sent_to_model() -> None:
    """反哺闭环：run() 收到的 open_insights 必须真的进了发给模型的 prompt，不是传了但没用上。"""
    proposal = EditProposal(reply="流程基本正常。", operations=[])
    model = FakeStructuredModel([{"raw": "", "parsed": proposal, "parsing_error": None}])
    agent = DesignEditAgent(model=model)

    agent.run(
        process=_process(),
        instruction="看看这条流程现在怎么样",
        open_insights=[
            {
                "node_id": "dept_gm",
                "source": "ops",
                "severity": "high",
                "occurrences": 4,
                "window": "近30天",
                "headline": "「部门总经理审批」存在覆盖漏洞",
                "evidence": {},
            }
        ],
    )

    user_message = model.runner.messages[0][1]["content"]
    assert "部门总经理审批" in user_message
    assert "覆盖漏洞" in user_message
