from __future__ import annotations

from app.agents.design_edit_agent import EditProposal
from app.agents.design_feedback import apply_diagnosis_feedback
from app.tools.process_edit_tools import FlowNodeUpdate, UpdateFlowNode
from data.schema import ProcessDefinition


def _process() -> ProcessDefinition:
    return ProcessDefinition.model_validate(
        {
            "meta": {
                "process_id": "TEST-001", "process_name": "测试流程", "version": "V1.0.0",
                "responsible_dept": "测试部门", "description": "测试", "applicant_scope": "全员", "entry_point": "OA系统",
            },
            "form_fields": [],
            "flow_nodes": [
                {"node_id": "draft", "node_name": "起草", "is_draft": True, "handler": None, "opinion": None,
                 "opinion_label": None, "time_limit_days": None,
                 "submit_paths": [{"path_name": "送审批", "condition": None, "target_node_id": "approval"}]},
                {"node_id": "approval", "node_name": "审批", "is_draft": False,
                 "handler": {"mode": "单选-单人处理", "source": "本部门", "role": "部门主管", "source_field": None},
                 "opinion": {"conclusive_required": True, "detail_required": False, "conclusive_options": ["同意", "不同意"]},
                 "opinion_label": "意见", "time_limit_days": None,
                 "submit_paths": [{"path_name": "流程结束", "condition": "结论性意见=同意", "target_node_id": "END"}]},
            ],
            "attachments": None, "roles": None,
        }
    )


class FakeEditAgent:
    """按指令内容返回预设 EditProposal，记录被调用的指令。"""

    def __init__(self, by_instruction: dict[str, EditProposal]) -> None:
        self.by_instruction = by_instruction
        self.seen_instructions: list[str] = []

    def run(self, *, process, instruction, **kwargs):
        self.seen_instructions.append(instruction)
        return self.by_instruction.get(instruction, EditProposal(reply="无法解析", operations=[]))


def test_adopted_suggestions_are_translated_by_edit_agent_and_applied() -> None:
    agent = FakeEditAgent({
        "给审批环节加处理时限2天": EditProposal(
            reply="已为审批环节设置处理时限2天。",
            operations=[UpdateFlowNode(node_id="approval", updates=FlowNodeUpdate(time_limit_days=2))],
        ),
    })
    items = [{"bottleneck_id": "slow:approval", "category": "slow_node", "suggested_process_edit_instruction": "给审批环节加处理时限2天"}]
    result = apply_diagnosis_feedback(process=_process(), diagnosis_items=items, adopted_bottleneck_ids=None, edit_agent=agent)

    # 翻译是 agent 做的（人没手写编辑），且被应用
    assert agent.seen_instructions == ["给审批环节加处理时限2天"]
    assert result.v2_process.get_node_by_id("approval").time_limit_days == 2
    assert result.applied[0].has_changes is True
    assert result.applied[0].operation_count == 1
    assert result.changed_suggestion_count == 1


def test_only_adopted_ids_are_applied_human_in_the_loop() -> None:
    agent = FakeEditAgent({
        "改A": EditProposal(reply="A", operations=[UpdateFlowNode(node_id="approval", updates=FlowNodeUpdate(time_limit_days=2))]),
        "改B": EditProposal(reply="B", operations=[UpdateFlowNode(node_id="approval", updates=FlowNodeUpdate(time_limit_days=5))]),
    })
    items = [
        {"bottleneck_id": "b_a", "suggested_process_edit_instruction": "改A"},
        {"bottleneck_id": "b_b", "suggested_process_edit_instruction": "改B"},
    ]
    # 人只采纳 b_a
    result = apply_diagnosis_feedback(process=_process(), diagnosis_items=items, adopted_bottleneck_ids=["b_a"], edit_agent=agent)
    assert agent.seen_instructions == ["改A"]
    assert result.v2_process.get_node_by_id("approval").time_limit_days == 2


def test_suggestion_without_instruction_is_skipped() -> None:
    agent = FakeEditAgent({})
    items = [{"bottleneck_id": "x", "suggested_process_edit_instruction": None}, {"bottleneck_id": "y"}]
    result = apply_diagnosis_feedback(process=_process(), diagnosis_items=items, adopted_bottleneck_ids=None, edit_agent=agent)
    assert result.applied == []
    assert agent.seen_instructions == []


def test_vague_suggestion_that_produces_no_edit_is_recorded_honestly() -> None:
    agent = FakeEditAgent({"含糊建议": EditProposal(reply="指令不够具体，无法生成修改。", operations=[])})
    items = [{"bottleneck_id": "z", "suggested_process_edit_instruction": "含糊建议"}]
    result = apply_diagnosis_feedback(process=_process(), diagnosis_items=items, adopted_bottleneck_ids=None, edit_agent=agent)
    assert result.applied[0].operation_count == 0
    assert result.applied[0].has_changes is False  # 如实记录：没转出编辑
    assert result.changed_suggestion_count == 0
    # 流程未被推进
    assert result.v2_process.get_node_by_id("approval").time_limit_days is None


def test_human_clarification_is_appended_to_instruction() -> None:
    agent = FakeEditAgent({})  # 记录看到的指令
    items = [{"bottleneck_id": "slow:ll", "suggested_process_edit_instruction": "短假豁免高层审批"}]
    apply_diagnosis_feedback(
        process=_process(),
        diagnosis_items=items,
        adopted_bottleneck_ids=None,
        clarifications={"slow:ll": "门槛设为14天"},
        edit_agent=agent,
    )
    assert len(agent.seen_instructions) == 1
    assert "短假豁免高层审批" in agent.seen_instructions[0]
    assert "业务决策补充：门槛设为14天" in agent.seen_instructions[0]
