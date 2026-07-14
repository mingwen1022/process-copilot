from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Any

import pytest

from app.agents.design_edit_agent import EditProposal
from app.api.slice1_service import Slice1Service
from app.api.workflow_design_service import WorkflowDesignService
from app.insights.store import InsightStore
from app.tools.process_edit_tools import (
    AddAttachment,
    AddSubmitPath,
    AttachmentUpdate,
    FlowNodeUpdate,
    FormFieldUpdate,
    SubmitPathUpdate,
    UpdateAttachment,
    UpdateFlowNode,
    UpdateFormField,
    UpdateSubmitPath,
)
from data.schema import AttachmentConfig, InsightKind, InsightSource, ProcessDefinition, SubmitPath

CREATED_BY = "u_it_app_staff"


def _fake_graph_initializer(**_kwargs: Any) -> dict[str, Any]:
    process = ProcessDefinition.model_validate(
        {
            "meta": {
                "process_id": "TEST-001",
                "process_name": "测试请假流程",
                "version": "V0.1.0",
                "responsible_dept": "人力资源部",
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
                {
                    "node_id": "approval",
                    "node_name": "审批",
                    "is_draft": False,
                    "handler": {"mode": "单选-单人处理", "source": "本部门", "role": "部门主管", "source_field": None},
                    "opinion": {"conclusive_required": True, "detail_required": False, "conclusive_options": ["同意", "不同意"]},
                    "opinion_label": "部门主管意见",
                    "time_limit_days": None,
                    "submit_paths": [{"path_name": "流程结束", "condition": "结论性意见=同意", "target_node_id": "END"}],
                },
            ],
            "attachments": None,
            "roles": None,
        }
    )
    payload = process.model_dump(mode="json")
    return {
        "output_paths": {},
        "workflow_design_output": {"process_definition": payload},
        "designer_assistant_message": {},
        "user_clarification_requests": [
            {"id": "clarify_sla", "question": "是否需要配置处理期限？", "options": ["需要", "不需要"]},
        ],
        "design_persistence_report": {},
        "schema_validation_report": {},
        "business_validation_result": {},
    }


class FakeEditAgent:
    def __init__(self, proposal: EditProposal | list[EditProposal]) -> None:
        self.proposals = proposal if isinstance(proposal, list) else [proposal]
        self.calls: list[dict[str, Any]] = []

    def run(self, **kwargs: Any) -> EditProposal:
        self.calls.append(kwargs)
        index = min(len(self.calls) - 1, len(self.proposals) - 1)
        return self.proposals[index]


@pytest.fixture()
def service_factory(tmp_path: Path):
    db_path = tmp_path / "runtime.db"
    Slice1Service(db_path)  # 建 users/workflow_definitions 等共享表 + 种子用户

    def make(proposal: EditProposal | list[EditProposal]) -> tuple[WorkflowDesignService, FakeEditAgent]:
        agent = FakeEditAgent(proposal)
        service = WorkflowDesignService(db_path, graph_initializer=_fake_graph_initializer, edit_agent=agent)
        return service, agent

    return make


def _new_session(service: WorkflowDesignService) -> str:
    payload = service.initialize_new_session(
        created_by=CREATED_BY,
        workflow_type="approval",
        workflow_name="测试请假流程",
        category="审批流程",
        instruction="员工请假申请流程",
        sources=[],
        uploaded_files=[],
    )
    return payload["session"]["session_id"]


def test_generate_draft_holds_edit_pending_then_applies_on_confirm(service_factory) -> None:
    """任何真实改动流程定义的编辑都先进"待确认"（含改字段必填这类纯属性微调），
    草稿此刻不变；用户 confirm 后才真正写入。pending_reason=edit（非合规、非结构性的一般改动）。"""
    proposal = EditProposal(
        reply="已将请假事由设为必填。",
        operations=[UpdateFormField(field_name="请假事由", updates=FormFieldUpdate(required_stages=["draft"]))],
    )
    service, agent = service_factory(proposal)
    session_id = _new_session(service)

    service.add_message(session_id=session_id, role="user", content="把请假事由改成必填")
    payload = service.generate_draft(session_id=session_id)

    # 拦下：草稿此刻还没变
    field_before = next(f for f in payload["draft_definition"]["form_fields"] if f["field_name"] == "请假事由")
    assert field_before["required_stages"] == []
    assert len(agent.calls) == 1
    assert agent.calls[0]["instruction"] == "把请假事由改成必填"

    last_message = payload["messages"][-1]
    assert last_message["role"] == "assistant"
    assert "已将请假事由设为必填" in last_message["content"]
    assert last_message["payload"]["event"] == "edit_pending_confirmation"
    assert last_message["payload"]["pending_reason"] == "edit"  # 非合规、非结构性的一般定义改动
    assert last_message["payload"]["diff"]["has_changes"] is True

    # 确认后才真正写入草稿
    confirmed = service.confirm_pending_edit(session_id=session_id)
    field_after = next(f for f in confirmed["draft_definition"]["form_fields"] if f["field_name"] == "请假事由")
    assert field_after["required_stages"] == ["draft"]


def test_reply_claiming_a_change_but_operation_is_noop_gets_corrective_note(service_factory) -> None:
    """真实复现的 bug：LLM 的 reply 文字描述了一个具体改动（"已把 XX 改成 YY"），但对应
    operation 的 updates 字段实际是空的/值跟原来一样——确定性应用层判定没有真变化
    （diff.has_changes=False），可 reply 文字却让用户以为已经改完了。这时不能让 reply
    文字单方面当真，必须用确定性的 diff 结果去纠正：追加一条系统提示，明确告诉用户
    这轮实际没有产生变化。"""
    proposal = EditProposal(
        reply="已修复「审批」环节的路径条件。",
        operations=[
            # target_node_id 跟现有值完全一样、condition 留空未指定——套用
            # _apply_plain_updates 的语义，这个操作不会产生任何真实字段变化
            UpdateSubmitPath(node_id="approval", path_name="流程结束", updates=SubmitPathUpdate(target_node_id="END")),
        ],
    )
    service, agent = service_factory(proposal)
    session_id = _new_session(service)

    service.add_message(session_id=session_id, role="user", content="把审批环节的条件修一下")
    payload = service.generate_draft(session_id=session_id)

    last_message = payload["messages"][-1]
    assert last_message["payload"]["event"] == "edit_no_change"
    assert last_message["payload"]["diff"]["has_changes"] is False
    assert "已修复「审批」环节的路径条件。" in last_message["content"]  # 原文保留
    assert "本轮虽然尝试了编辑操作，但流程定义实际未发生变化" in last_message["content"]  # 追加纠正


def test_pure_qa_turn_with_no_operations_has_no_corrective_note(service_factory) -> None:
    """负向对照：纯问答轮次（operations 为空，LLM 正确判断这轮不需要改）不该被追加
    纠正提示——那是正常情况，不是"说了但没做"的假成功，加上去只会制造噪音。"""
    proposal = EditProposal(reply="当前流程的审批环节已经有兜底路径，不需要修改。", operations=[])
    service, agent = service_factory(proposal)
    session_id = _new_session(service)

    service.add_message(session_id=session_id, role="user", content="审批环节是不是缺兜底路径")
    payload = service.generate_draft(session_id=session_id)

    last_message = payload["messages"][-1]
    assert last_message["content"] == "当前流程的审批环节已经有兜底路径，不需要修改。"
    assert "本轮虽然尝试了编辑操作" not in last_message["content"]


def test_is_structural_change_classification() -> None:
    """结构性判定的边界——改流程骨架的算 structural，纯文案/属性微调不算。注意：这个判定
    现在只用来给待确认卡片分类文案（pending_reason=structural vs edit），不再决定拦不拦——
    所有真实改动都要确认（见 test_holds_edit_pending_then_applies_on_confirm）。"""
    f = WorkflowDesignService._is_structural_change
    # 结构性：环节增删 / 路径增删改 / 处理人变更 / 字段增删
    assert f({"flow_nodes": {"added": ["x"], "removed": [], "changed": []}}) is True
    assert f({"flow_nodes": {"added": [], "removed": ["x"], "changed": []}}) is True
    assert f({"flow_nodes": {"added": [], "removed": [], "changed": [
        {"key": "n", "changes": [], "submit_paths": {"added": ["p"], "removed": [], "changed": []}}]}}) is True
    assert f({"flow_nodes": {"added": [], "removed": [], "changed": [
        {"key": "n", "changes": [{"attribute": "handler"}]}]}}) is True
    assert f({"form_fields": {"added": ["x"], "removed": [], "changed": []}}) is True
    # 非结构性：改环节名/处理期限、改字段必填/说明、改流程描述
    assert f({"flow_nodes": {"added": [], "removed": [], "changed": [
        {"key": "n", "changes": [{"attribute": "time_limit_days"}, {"attribute": "node_name"}]}]}}) is False
    assert f({"form_fields": {"added": [], "removed": [], "changed": [
        {"key": "f", "changes": [{"attribute": "required_stages"}]}]}}) is False
    assert f({"meta": [{"attribute": "description"}]}) is False
    assert f({}) is False


def test_structural_change_requires_confirmation_before_writing_draft(service_factory) -> None:
    """结构性改动（这里是新增一条提交路径）先进"待确认"——草稿保持原样、
    pending_reason=structural；用户 confirm 后才真正写入。这里专门盯 structural 这个
    分类文案；一般属性微调走 pending_reason=edit（见 test_holds_edit_pending_then_applies_on_confirm）。"""
    proposal = EditProposal(
        reply="已在「审批」环节新增一条兜底路径。",
        operations=[
            AddSubmitPath(
                node_id="approval",
                path=SubmitPath(path_name="兜底-流程结束", condition=None, target_node_id="END"),
            )
        ],
    )
    service, _agent = service_factory(proposal)
    session_id = _new_session(service)

    def _approval_paths(payload: dict[str, Any]) -> set[str]:
        return {
            p["path_name"]
            for n in payload["draft_definition"]["flow_nodes"]
            if n["node_id"] == "approval"
            for p in n["submit_paths"]
        }

    before = _approval_paths(service.session_payload(session_id))

    service.add_message(session_id=session_id, role="user", content="加一条兜底路径送结束")
    pending = service.generate_draft(session_id=session_id)

    assert pending["session"]["has_pending_edit"] is True
    msg = pending["messages"][-1]
    assert msg["payload"]["event"] == "edit_pending_confirmation"
    assert msg["payload"]["pending_reason"] == "structural"
    assert _approval_paths(pending) == before  # 草稿此刻没变，新路径还在 pending 里

    confirmed = service.confirm_pending_edit(session_id=session_id)
    assert "兜底-流程结束" in _approval_paths(confirmed)  # 确认后才真正写入草稿


def test_generate_draft_is_noop_without_new_user_message(service_factory) -> None:
    proposal = EditProposal(
        reply="已将请假事由设为必填。",
        operations=[UpdateFormField(field_name="请假事由", updates=FormFieldUpdate(required_stages=["draft"]))],
    )
    service, agent = service_factory(proposal)
    session_id = _new_session(service)

    service.add_message(session_id=session_id, role="user", content="把请假事由改成必填")
    service.generate_draft(session_id=session_id)
    assert len(agent.calls) == 1

    # 没有新的用户消息（且有待确认项）重复调用 generate_draft 不应再次触发 agent 或重复追加
    payload_again = service.generate_draft(session_id=session_id)
    assert len(agent.calls) == 1
    edit_messages = [m for m in payload_again["messages"] if m["payload"].get("event") == "edit_pending_confirmation"]
    assert len(edit_messages) == 1  # 只提议了一次，没有因为重复点击而重复追加


def test_undo_last_change_restores_previous_draft(service_factory) -> None:
    proposal = EditProposal(
        reply="已将请假事由设为必填。",
        operations=[UpdateFormField(field_name="请假事由", updates=FormFieldUpdate(required_stages=["draft"]))],
    )
    service, _agent = service_factory(proposal)
    session_id = _new_session(service)

    service.add_message(session_id=session_id, role="user", content="把请假事由改成必填")
    service.generate_draft(session_id=session_id)
    service.confirm_pending_edit(session_id=session_id)  # 真实改动先进待确认，确认后才落草稿、可撤销

    undone = service.undo_last_change(session_id=session_id)
    field = next(f for f in undone["draft_definition"]["form_fields"] if f["field_name"] == "请假事由")
    assert field["required_stages"] == []

    with pytest.raises(RuntimeError):
        service.undo_last_change(session_id=session_id)


def test_undo_survives_a_no_op_turn_after_a_real_edit(service_factory) -> None:
    """一次真实编辑之后再来一轮不产生任何操作的问答/重复确认，undo 仍应能撤销
    最近一次真实编辑，而不是被这轮空操作悄悄'消费'掉可撤销状态。"""
    real_edit = EditProposal(
        reply="已将请假事由设为必填。",
        operations=[UpdateFormField(field_name="请假事由", updates=FormFieldUpdate(required_stages=["draft"]))],
    )
    no_op_followup = EditProposal(reply="「请假事由」字段目前已经是必填，无需重复修改。", operations=[])
    service, agent = service_factory([real_edit, no_op_followup])
    session_id = _new_session(service)

    service.add_message(session_id=session_id, role="user", content="把请假事由改成必填")
    service.generate_draft(session_id=session_id)
    after_real_edit = service.confirm_pending_edit(session_id=session_id)  # 真实改动确认后落草稿
    assert after_real_edit["session"]["can_undo"] is True

    service.add_message(session_id=session_id, role="user", content="请假事由改成必填")
    after_no_op = service.generate_draft(session_id=session_id)
    assert len(agent.calls) == 2
    assert after_no_op["session"]["can_undo"] is True  # 空操作这轮不应清空可撤销状态


def test_undo_survives_a_redundant_same_value_update_operation(service_factory) -> None:
    """LLM 有时会重复提出'改成同一个值'的更新操作——这类操作对 apply_edit_operations
    而言是'成功执行'（不报错、计入 applied），但实际数据没有变化。这一轮不应被当作
    真实编辑而覆盖 undo 指针，判定标准是 diff 里数据是否真的变了，不是操作有没有报错。
    """
    real_edit = EditProposal(
        reply="已将联系电话最大长度设为20。",
        operations=[UpdateFormField(field_name="请假事由", updates=FormFieldUpdate(max_length=20))],
    )
    redundant_same_value_update = EditProposal(
        reply="联系电话最大长度已经是20。",
        operations=[UpdateFormField(field_name="请假事由", updates=FormFieldUpdate(max_length=20))],
    )
    service, agent = service_factory([real_edit, redundant_same_value_update])
    session_id = _new_session(service)

    service.add_message(session_id=session_id, role="user", content="请假事由最大长度设为20")
    service.generate_draft(session_id=session_id)
    after_real_edit = service.confirm_pending_edit(session_id=session_id)  # 真实改动确认后落草稿
    assert after_real_edit["session"]["can_undo"] is True

    service.add_message(session_id=session_id, role="user", content="请假事由最大长度设为20")
    after_redundant = service.generate_draft(session_id=session_id)
    assert len(agent.calls) == 2
    # 第二轮操作本身"执行成功"（无报错），但值和当前一致——不算真实变更，直接 edit_no_change（不进待确认）
    assert after_redundant["messages"][-1]["payload"]["event"] == "edit_no_change"
    assert after_redundant["session"]["can_undo"] is True

    undone = service.undo_last_change(session_id=session_id)
    field = next(f for f in undone["draft_definition"]["form_fields"] if f["field_name"] == "请假事由")
    assert field["max_length"] is None  # 撤销回到了第一次真实编辑之前，而不是被第二轮悄悄覆盖


def test_edit_errors_are_reported_without_blocking_reply(service_factory) -> None:
    proposal = EditProposal(
        reply="尝试更新一个不存在的字段。",
        operations=[UpdateFormField(field_name="不存在的字段", updates=FormFieldUpdate(required_stages=["draft"]))],
    )
    service, _agent = service_factory(proposal)
    session_id = _new_session(service)

    service.add_message(session_id=session_id, role="user", content="改一下某个字段")
    payload = service.generate_draft(session_id=session_id)

    last_message = payload["messages"][-1]
    assert last_message["content"] == "尝试更新一个不存在的字段。"  # content 只放 reply，错误走结构化 payload
    assert last_message["payload"]["errors"]
    assert last_message["payload"]["diff"]["has_changes"] is False


def test_source_context_is_passed_to_edit_agent(service_factory) -> None:
    proposal = EditProposal(reply="收到补充材料。", operations=[])
    service, agent = service_factory(proposal)
    session_id = _new_session(service)

    service.add_source(
        session_id=session_id,
        title="补充说明.txt",
        content="病假需要上传就诊证明。",
        created_by=CREATED_BY,
    )
    service.add_message(session_id=session_id, role="user", content="根据补充说明，加一个附件要求")
    service.generate_draft(session_id=session_id)

    assert "病假需要上传就诊证明" in agent.calls[0]["source_context"]


def test_open_insights_are_passed_to_edit_agent_every_turn(service_factory) -> None:
    """反哺闭环：设计副驾不该只靠"让副驾诊断"按钮把证据现拼进指令文本才看得到运行侧
    洞察——每轮 generate_draft 都该把当前 open 的洞察传给 agent，不管这轮用户问的是不是
    跟某条具体洞察相关的话（自由聊天问"这条流程现状如何"时也一样看得到）。"""
    proposal = EditProposal(reply="流程结构基本正常。", operations=[])
    service, agent = service_factory(proposal)
    insight_store = InsightStore()
    service._insight_store = insight_store
    session_id = _new_session(service)
    process_id = service.session_payload(session_id)["draft_definition"]["meta"]["process_id"]
    _seed_coverage_gap_insight(insight_store, process_id=process_id)

    service.add_message(session_id=session_id, role="user", content="随便看看这条流程现在怎么样")
    service.generate_draft(session_id=session_id)

    insights = agent.calls[0]["open_insights"]
    assert len(insights) == 1
    assert insights[0]["kind"] == "coverage_gap"
    assert insights[0]["node_id"] == "approval"


def test_open_insights_is_empty_without_insight_store(service_factory) -> None:
    """没注入 insight_store 时（比如没有反哺闭环上下文的场景）静默传空列表，不报错。"""
    proposal = EditProposal(reply="收到。", operations=[])
    service, agent = service_factory(proposal)
    session_id = _new_session(service)

    service.add_message(session_id=session_id, role="user", content="随便问问")
    service.generate_draft(session_id=session_id)

    assert agent.calls[0]["open_insights"] == []


def test_open_clarifications_uses_structured_langgraph_items(service_factory) -> None:
    proposal = EditProposal(reply="好的。", operations=[], resolved_clarification_ids=["clarify_sla"])
    service, agent = service_factory(proposal)
    session_id = _new_session(service)

    service.add_message(session_id=session_id, role="user", content="不需要配置处理期限")
    service.generate_draft(session_id=session_id)

    clarifications = agent.calls[0]["open_clarifications"]
    assert clarifications == [{"id": "clarify_sla", "question": "是否需要配置处理期限？", "options": ["需要", "不需要"]}]


def _leave_certificate_scenario(service_factory):
    """复现真实场景的公共前置：先加一个合规的按条件必传附件，再让 agent 把
    必传条件清空——这一步违反 leave.sick_leave_certificate。"""
    add_attachment = EditProposal(
        reply="已新增「就诊证明」附件，病假且请假天数≥3天时必传。",
        operations=[
            AddAttachment(
                attachment=AttachmentConfig(
                    attachment_type="就诊证明",
                    upload_stages=["all"],
                    required_stages=[],
                    required_condition="请假类型=病假 且 请假天数>=3",
                )
            )
        ],
    )
    clear_condition = EditProposal(
        reply="已将「就诊证明」附件的必传条件清空。",
        operations=[
            UpdateAttachment(
                attachment_type="就诊证明",
                updates=AttachmentUpdate(clear_required_condition=True),
            )
        ],
    )
    service, agent = service_factory([add_attachment, clear_condition])
    session_id = _new_session(service)

    service.add_message(session_id=session_id, role="user", content="加一个就诊证明附件，病假超过3天必传")
    after_add = service.generate_draft(session_id=session_id)
    findings_after_add = after_add["messages"][-1]["payload"]["compliance_findings"]
    assert not any(f["rule_id"] == "leave.sick_leave_certificate" for f in findings_after_add)  # 这步没引入违规
    # 任何真实改动都先进待确认——这步是合规的一般改动（pending_reason=edit），确认后落草稿
    assert after_add["messages"][-1]["payload"]["pending_reason"] == "edit"
    service.confirm_pending_edit(session_id=session_id)

    return service, agent, session_id


def test_edit_introducing_new_violation_is_held_pending_not_committed(service_factory) -> None:
    """新引入违规时应该拦下：不写入 draft_definition_json，等用户确认/放弃——
    不能一边提示风险一边已经把违规草稿落库了。"""
    service, _agent, session_id = _leave_certificate_scenario(service_factory)
    before = service.session_payload(session_id)
    attachment_before = next(a for a in before["draft_definition"]["attachments"] if a["attachment_type"] == "就诊证明")
    assert attachment_before["required_condition"]  # 拦截前草稿还是合规状态

    service.add_message(session_id=session_id, role="user", content="把附件必传条件去掉")
    after_clear = service.generate_draft(session_id=session_id)

    assert after_clear["session"]["has_pending_edit"] is True
    last_message = after_clear["messages"][-1]
    assert last_message["payload"]["event"] == "edit_pending_confirmation"
    assert last_message["payload"]["requires_confirmation"] is True
    finding = next(
        f for f in last_message["payload"]["compliance_findings"] if f["rule_id"] == "leave.sick_leave_certificate"
    )
    assert finding["severity"] == "high"
    assert finding["source_doc"] == "leave_management_policy"

    # 草稿本身还没变——被拦下的编辑没有落库
    attachment_after = next(
        a for a in after_clear["draft_definition"]["attachments"] if a["attachment_type"] == "就诊证明"
    )
    assert attachment_after["required_condition"] == attachment_before["required_condition"]


def test_generate_draft_ignores_new_instruction_while_pending(service_factory) -> None:
    """有未解决的合规确认时，不接受新指令、不再调用 LLM——必须先 confirm/discard。"""
    service, agent, session_id = _leave_certificate_scenario(service_factory)
    service.add_message(session_id=session_id, role="user", content="把附件必传条件去掉")
    service.generate_draft(session_id=session_id)
    calls_before = len(agent.calls)

    service.add_message(session_id=session_id, role="user", content="再随便改点别的")
    payload = service.generate_draft(session_id=session_id)

    assert len(agent.calls) == calls_before  # 没有再触发 LLM
    assert payload["session"]["has_pending_edit"] is True


def test_confirm_pending_edit_commits_draft_and_enables_undo(service_factory) -> None:
    service, _agent, session_id = _leave_certificate_scenario(service_factory)
    service.add_message(session_id=session_id, role="user", content="把附件必传条件去掉")
    service.generate_draft(session_id=session_id)

    confirmed = service.confirm_pending_edit(session_id=session_id)
    assert confirmed["session"]["has_pending_edit"] is False
    assert confirmed["session"]["can_undo"] is True
    attachment = next(a for a in confirmed["draft_definition"]["attachments"] if a["attachment_type"] == "就诊证明")
    assert not attachment["required_condition"]
    assert confirmed["messages"][-1]["payload"]["event"] == "edit_pending_resolved"
    assert confirmed["messages"][-1]["payload"]["resolution"] == "confirmed"

    # undo 应该能回到确认前（附件仍必传）的状态
    undone = service.undo_last_change(session_id=session_id)
    attachment = next(a for a in undone["draft_definition"]["attachments"] if a["attachment_type"] == "就诊证明")
    assert attachment["required_condition"]


# ——— 反哺闭环复检：改草稿之后，覆盖漏洞洞察该不该自动消解 ———
# 注意：initialize_new_session 会给草稿生成一个新的合成 process_id（如 AI-xxxxxxxx），
# 不会保留 fake_graph_initializer 里写的 "TEST-001"——所以洞察必须在建好会话、读到
# 真实生成的 process_id 之后再按那个 id 种，不能在建会话前硬编码 "TEST-001"。

def _seed_coverage_gap_insight(store: InsightStore, *, process_id: str, node_id: str = "approval") -> str:
    insight = store.record(
        workflow_definition_id=process_id,
        node_id=node_id,
        kind=InsightKind.COVERAGE_GAP,
        severity="high",
        source=InsightSource.OPS,
        evidence={"blocked_paths": [{"path_name": "流程结束", "target_node_id": "END", "reason": "结论性意见≠同意"}]},
        window="近30天",
        headline="「审批」存在覆盖漏洞：某区间无审批路径，落进的申请卡住",
    )
    return insight.insight_id


def _seed_analytics_insight(store: InsightStore, *, process_id: str, node_id: str, kind: InsightKind) -> str:
    """分析/组织类洞察（sla_breach/slow_node/high_return/org_gap）——这些是历史运行数据的
    事实，改设计不能确定性消解，只能标 acknowledged。"""
    insight = store.record(
        workflow_definition_id=process_id,
        node_id=node_id,
        kind=kind,
        severity="high",
        source=InsightSource.ANALYTICS,
        evidence={},
        window="近30天",
        headline=f"{node_id} 环节效能异常",
    )
    return insight.insight_id


def test_editing_a_node_acknowledges_its_analytics_insights_not_resolves(service_factory) -> None:
    """分析/组织类洞察改设计后不能像覆盖漏洞那样确定性消解（要等新数据），设计侧对该环节做了
    真实改动后应标成 acknowledged（已响应·待验证）——退出待处理、但不假装已解决。"""
    proposal = EditProposal(
        reply="将把「审批」环节的处理期限设为 3 天。",
        operations=[UpdateFlowNode(node_id="approval", updates=FlowNodeUpdate(time_limit_days=3))],
    )
    service, _agent = service_factory(proposal)
    insight_store = InsightStore()
    service._insight_store = insight_store
    session_id = _new_session(service)
    process_id = service.session_payload(session_id)["draft_definition"]["meta"]["process_id"]
    sla_id = _seed_analytics_insight(insight_store, process_id=process_id, node_id="approval", kind=InsightKind.SLA_BREACH)
    other_id = _seed_analytics_insight(insight_store, process_id=process_id, node_id="draft", kind=InsightKind.SLOW_NODE)

    service.add_message(session_id=session_id, role="user", content="给审批环节设个3天处理期限")
    payload = service.generate_draft(session_id=session_id)

    assert payload["messages"][-1]["payload"]["event"] == "edit_pending_confirmation"  # 改期限也先进待确认
    assert insight_store.get(sla_id).status.value == "open"  # 确认前不动洞察
    service.confirm_pending_edit(session_id=session_id)

    # 被改动的 approval 环节上的分析洞察 → acknowledged（不是 resolved，也不是还 open）
    assert insight_store.get(sla_id).status.value == "acknowledged"
    # 没被这次改动碰到的 draft 环节洞察 → 仍 open
    assert insight_store.get(other_id).status.value == "open"


def test_acknowledged_insight_stays_visible_but_marked(service_factory) -> None:
    """acknowledged 的洞察仍在 open_for 返回里（设计侧读得到、可折叠展示"已响应"），只是不再是
    open——跟 resolved（彻底消解、不再返回）区分开。"""
    proposal = EditProposal(
        reply="将把「审批」环节的处理期限设为 2 天。",
        operations=[UpdateFlowNode(node_id="approval", updates=FlowNodeUpdate(time_limit_days=2))],
    )
    service, _agent = service_factory(proposal)
    insight_store = InsightStore()
    service._insight_store = insight_store
    session_id = _new_session(service)
    process_id = service.session_payload(session_id)["draft_definition"]["meta"]["process_id"]
    _seed_analytics_insight(insight_store, process_id=process_id, node_id="approval", kind=InsightKind.SLA_BREACH)

    service.add_message(session_id=session_id, role="user", content="审批环节设2天期限")
    service.generate_draft(session_id=session_id)
    confirmed = service.confirm_pending_edit(session_id=session_id)

    statuses = {i["kind"]: i["status"] for i in confirmed["insights"]}
    assert statuses.get("sla_breach") == "acknowledged"  # 仍返回、但标已响应


def test_form_field_edit_acknowledges_insight_llm_explicitly_addressed(service_factory) -> None:
    """改的是表单字段（不落在任何 flow_node 上），diff.flow_nodes 里没有 touched_ids——
    这类改动只能靠 LLM 在结构化输出里声明「这次方案回应了哪条洞察」来 ack，不能靠环节匹配。"""
    insight_store = InsightStore()

    def make_proposal(insight_id: str) -> EditProposal:
        return EditProposal(
            reply="将把「请假事由」改成必填。",
            operations=[UpdateFormField(field_name="请假事由", updates=FormFieldUpdate(required_stages=["approval"]))],
            addressed_insight_ids=[insight_id],
        )

    # insight_id 要等 session 建完才知道，先用占位 proposal 构造 service，再替换成真正的 proposal
    service, agent = service_factory(EditProposal(reply="占位", operations=[]))
    service._insight_store = insight_store
    session_id = _new_session(service)
    process_id = service.session_payload(session_id)["draft_definition"]["meta"]["process_id"]
    approval_insight_id = _seed_analytics_insight(
        insight_store, process_id=process_id, node_id="approval", kind=InsightKind.SLA_BREACH
    )

    agent.proposals = [make_proposal(approval_insight_id)]
    service.add_message(session_id=session_id, role="user", content="把请假事由改成必填")
    payload = service.generate_draft(session_id=session_id)

    assert payload["messages"][-1]["payload"]["event"] == "edit_pending_confirmation"  # 字段改动也先进待确认
    assert payload["messages"][-1]["payload"]["addressed_insight_ids"] == [approval_insight_id]  # 声明随 pending 存下
    assert insight_store.get(approval_insight_id).status.value == "open"  # 确认前不动
    service.confirm_pending_edit(session_id=session_id)
    assert insight_store.get(approval_insight_id).status.value == "acknowledged"


def test_addressed_insight_id_not_shown_this_turn_is_ignored(service_factory) -> None:
    """LLM 声明了一个这轮根本没在 open_insights 里给它看过的 id——防编造，不予采信。"""
    insight_store = InsightStore()
    service, agent = service_factory(EditProposal(reply="占位", operations=[]))
    service._insight_store = insight_store
    session_id = _new_session(service)
    process_id = service.session_payload(session_id)["draft_definition"]["meta"]["process_id"]
    real_insight_id = _seed_analytics_insight(
        insight_store, process_id=process_id, node_id="approval", kind=InsightKind.SLA_BREACH
    )

    agent.proposals = [
        EditProposal(
            reply="将把「请假事由」改成必填。",
            operations=[UpdateFormField(field_name="请假事由", updates=FormFieldUpdate(required_stages=["approval"]))],
            addressed_insight_ids=["insight_does_not_exist"],
        )
    ]
    service.add_message(session_id=session_id, role="user", content="把请假事由改成必填")
    service.generate_draft(session_id=session_id)
    service.confirm_pending_edit(session_id=session_id)  # 走完确认路径，acknowledge 逻辑真的跑一遍

    assert insight_store.get(real_insight_id).status.value == "open"  # 编造的 id 不予采信，保持 open


def test_generate_draft_resolves_coverage_gap_insight_when_fallback_path_added(service_factory) -> None:
    """真实复现场景：副驾诊断出「审批」环节覆盖漏洞（反哺洞察 OPEN），用户让副驾加一条
    无条件兜底路径。加路径是结构性改动，先进待确认——确认前草稿没真更新、洞察保持 OPEN；
    用户点「确认应用」后草稿更新、这条洞察才自动 resolve（不然顶部提醒栏和总览页的
    「覆盖漏洞·待修」卡片还在，误导用户以为没修好）。"""
    add_fallback = EditProposal(
        reply="已在「审批」环节新增一条无条件兜底路径，防止条件判断失效时单子无法流转。",
        operations=[
            AddSubmitPath(
                node_id="approval",
                path=SubmitPath(path_name="兜底-流程结束", condition=None, target_node_id="END"),
            )
        ],
    )
    service, _agent = service_factory(add_fallback)
    insight_store = InsightStore()
    service._insight_store = insight_store
    session_id = _new_session(service)
    process_id = service.session_payload(session_id)["draft_definition"]["meta"]["process_id"]
    insight_id = _seed_coverage_gap_insight(insight_store, process_id=process_id)

    service.add_message(session_id=session_id, role="user", content="加一条兜底路径，送结束")
    pending = service.generate_draft(session_id=session_id)
    # 加路径 = 结构性改动 → 先进待确认；草稿未变、洞察此时不该关闭
    assert pending["session"]["has_pending_edit"] is True
    assert insight_store.get(insight_id).status == "open"

    payload = service.confirm_pending_edit(session_id=session_id)
    assert insight_store.get(insight_id).status == "resolved"
    assert payload["insights"] == []  # session_payload 只读 open_for，resolved 的不再出现


def test_confirm_pending_edit_resolves_coverage_gap_with_conditional_new_path(service_factory) -> None:
    """真实复现的 bug：覆盖漏洞的修复方案通常不是无条件兜底，而是一条带具体条件区间的
    路径（如"3天<请假天数≤7天"）——这类修复此前不会被复检认出（旧逻辑只认无条件路径），
    确认应用后草稿明明更新了，提醒栏和"覆盖漏洞·待修"卡片却一直不消失。"""
    add_conditional = EditProposal(
        reply="已新增「送条线分管领导审批（中档）」路径，条件为 3天<请假天数≤7天。",
        operations=[
            AddSubmitPath(
                node_id="approval",
                path=SubmitPath(
                    path_name="送条线分管领导审批（中档）",
                    condition="请假天数>3天 且 请假天数<=7天",
                    target_node_id="END",
                ),
            )
        ],
    )
    service, _agent = service_factory(add_conditional)
    insight_store = InsightStore()
    service._insight_store = insight_store
    session_id = _new_session(service)
    process_id = service.session_payload(session_id)["draft_definition"]["meta"]["process_id"]
    insight_id = _seed_coverage_gap_insight(insight_store, process_id=process_id)

    service.add_message(session_id=session_id, role="user", content="加一条3-7天的中档路径")
    pending = service.generate_draft(session_id=session_id)
    assert pending["session"]["has_pending_edit"] is True
    assert insight_store.get(insight_id).status == "open"  # 待确认阶段草稿没变，不该关

    service.confirm_pending_edit(session_id=session_id)
    assert insight_store.get(insight_id).status == "resolved"  # 有条件的新路径也该被认出


def test_confirm_pending_edit_resolves_coverage_gap_when_existing_path_condition_widened(service_factory) -> None:
    """真实复现（发布 V1.0 带缺口后）：修覆盖漏洞的另一种手法是放宽现有某档路径的条件
    （update_submit_path 改 condition，不是加新路径）——走 diff 的 submit_paths.changed，
    复检也要认这个信号，不然"把≤3天放宽到≤7天"这类修复确认完了提醒还赖着不走。"""
    widen = EditProposal(
        reply="已把「流程结束」的天数上限从≤3天放宽到≤7天，补上 3~7 天的空白。",
        operations=[
            UpdateSubmitPath(
                node_id="approval",
                path_name="流程结束",
                updates=SubmitPathUpdate(condition="结论性意见=同意 且 请假天数≤7天"),
            )
        ],
    )
    service, _agent = service_factory(widen)
    insight_store = InsightStore()
    service._insight_store = insight_store
    session_id = _new_session(service)
    process_id = service.session_payload(session_id)["draft_definition"]["meta"]["process_id"]
    insight_id = _seed_coverage_gap_insight(insight_store, process_id=process_id)

    service.add_message(session_id=session_id, role="user", content="把流程结束的天数放宽到7天")
    pending = service.generate_draft(session_id=session_id)
    assert pending["session"]["has_pending_edit"] is True  # 改路径条件=结构性→待确认
    assert insight_store.get(insight_id).status == "open"

    service.confirm_pending_edit(session_id=session_id)
    assert insight_store.get(insight_id).status == "resolved"  # 改现有路径条件也该被认出


def test_generate_draft_leaves_coverage_gap_open_when_no_fallback_path_added(service_factory) -> None:
    """负向对照：跟覆盖漏洞无关的编辑，不该把洞察一并误关掉。"""
    unrelated_edit = EditProposal(
        reply="已将请假事由设为必填。",
        operations=[UpdateFormField(field_name="请假事由", updates=FormFieldUpdate(required_stages=["draft"]))],
    )
    service, _agent = service_factory(unrelated_edit)
    insight_store = InsightStore()
    service._insight_store = insight_store
    session_id = _new_session(service)
    process_id = service.session_payload(session_id)["draft_definition"]["meta"]["process_id"]
    insight_id = _seed_coverage_gap_insight(insight_store, process_id=process_id)

    service.add_message(session_id=session_id, role="user", content="把请假事由改成必填")
    payload = service.generate_draft(session_id=session_id)

    assert insight_store.get(insight_id).status == "open"
    assert len(payload["insights"]) == 1


def test_confirm_pending_edit_resolves_coverage_gap_insight_when_fallback_path_added(service_factory) -> None:
    """同一个复检也要在「拦下 → 用户 confirm」这条路径上生效——不能只在直接应用时生效。
    把"加兜底路径"跟一个会触发合规拦截的改动放进同一次编辑，逼这次编辑走 pending 确认。"""
    add_attachment = EditProposal(
        reply="已新增「就诊证明」附件，病假且请假天数≥3天时必传。",
        operations=[
            AddAttachment(
                attachment=AttachmentConfig(
                    attachment_type="就诊证明", upload_stages=["all"], required_stages=[],
                    required_condition="请假类型=病假 且 请假天数>=3",
                )
            )
        ],
    )
    combined_edit = EditProposal(
        reply="已加兜底路径，并清空就诊证明的必传条件。",
        operations=[
            UpdateAttachment(attachment_type="就诊证明", updates=AttachmentUpdate(clear_required_condition=True)),
            AddSubmitPath(
                node_id="approval",
                path=SubmitPath(path_name="兜底-流程结束", condition=None, target_node_id="END"),
            ),
        ],
    )
    service, _agent = service_factory([add_attachment, combined_edit])
    insight_store = InsightStore()
    service._insight_store = insight_store
    session_id = _new_session(service)
    process_id = service.session_payload(session_id)["draft_definition"]["meta"]["process_id"]
    insight_id = _seed_coverage_gap_insight(insight_store, process_id=process_id)

    service.add_message(session_id=session_id, role="user", content="加一个就诊证明附件，病假超过3天必传")
    service.generate_draft(session_id=session_id)
    service.confirm_pending_edit(session_id=session_id)  # 加附件这步也先待确认，确认后才落草稿
    service.add_message(session_id=session_id, role="user", content="加兜底路径，并把附件必传条件去掉")
    pending = service.generate_draft(session_id=session_id)
    assert pending["session"]["has_pending_edit"] is True  # 确认真的走了拦截路径，不是直接应用
    assert insight_store.get(insight_id).status == "open"  # 还没 confirm，不该提前 resolve

    confirmed = service.confirm_pending_edit(session_id=session_id)
    assert confirmed["session"]["has_pending_edit"] is False
    assert insight_store.get(insight_id).status == "resolved"


def test_generate_draft_resolves_insight_when_node_id_only_loosely_matches(service_factory) -> None:
    """回归：真实场景里，运维 mock 库（app/copilots/mock_instances.py 的合成请假流程用
    node_id "gm"）跟真实发布流程（用 "dept_gm"）对同一个"部门总经理审批"环节命名不完全
    一致——前端 renderDesignws 里的 resolveLoose 已经在按子串松匹配处理这个问题，复检也
    得一样松匹配，不然对着真实草稿永远找不到节点、这条洞察永远关不掉。这里用 "appr"
    （"approval" 的子串）复现同一类命名不一致，不依赖具体某条流程的真实 node_id 拼法。"""
    add_fallback = EditProposal(
        reply="已在「审批」环节新增一条无条件兜底路径。",
        operations=[
            AddSubmitPath(
                node_id="approval",
                path=SubmitPath(path_name="兜底-流程结束", condition=None, target_node_id="END"),
            )
        ],
    )
    service, _agent = service_factory(add_fallback)
    insight_store = InsightStore()
    service._insight_store = insight_store
    session_id = _new_session(service)
    process_id = service.session_payload(session_id)["draft_definition"]["meta"]["process_id"]
    insight_id = _seed_coverage_gap_insight(insight_store, process_id=process_id, node_id="appr")  # 不是 "approval"

    service.add_message(session_id=session_id, role="user", content="加一条兜底路径，送结束")
    pending = service.generate_draft(session_id=session_id)
    assert pending["session"]["has_pending_edit"] is True  # 结构性改动先进待确认
    assert insight_store.get(insight_id).status == "open"

    service.confirm_pending_edit(session_id=session_id)
    assert insight_store.get(insight_id).status == "resolved"


def test_discard_pending_edit_leaves_draft_unchanged(service_factory) -> None:
    service, _agent, session_id = _leave_certificate_scenario(service_factory)
    before = service.session_payload(session_id)
    service.add_message(session_id=session_id, role="user", content="把附件必传条件去掉")
    service.generate_draft(session_id=session_id)

    discarded = service.discard_pending_edit(session_id=session_id)
    assert discarded["session"]["has_pending_edit"] is False
    attachment = next(a for a in discarded["draft_definition"]["attachments"] if a["attachment_type"] == "就诊证明")
    assert attachment["required_condition"] == next(
        a for a in before["draft_definition"]["attachments"] if a["attachment_type"] == "就诊证明"
    )["required_condition"]
    assert discarded["messages"][-1]["payload"]["event"] == "edit_pending_resolved"
    assert discarded["messages"][-1]["payload"]["resolution"] == "discarded"

    with pytest.raises(RuntimeError):
        service.confirm_pending_edit(session_id=session_id)


# ——— 智能门控接进 generate_draft：定性规则（LLM-judge）新引入违规也要能在编辑门里
# 拦下——跟确定性规则同一个待遇，走同一套 pending_reason=compliance/compliance_findings。
# process_id 特意用 "LEAVE-001"，这样 design.node_naming_role_action（真实知识库里
# applies_to_processes 含 LEAVE-001）能被 _select_applicable_rules 选中，不用另外
# 注入自定义规则列表。———

class _SequencedStubJudge:
    """跟 test_rag_compliance.py 的同名类同一个用途：按调用顺序（before 一次、after
    一次）弹出不同 violations，用于验证"改前这个环节合规、改后不合规"才报的语义。
    verdicts_by_call 每项是 rule_id -> [(node_id, detail), ...]；某规则不在映射里
    或列表为空＝该规则这次调用完全合规。"""

    def __init__(self, verdicts_by_call: list[dict[str, list[tuple[str, str]]]]):
        self._verdicts_by_call = verdicts_by_call
        self.calls: list[list[str]] = []
        self.structured_model = object()

    def judge(self, process, rules):
        from app.agents.compliance_judge_agent import RuleVerdict, RuleViolation

        self.calls.append([r.rule_id for r in rules])
        index = min(len(self.calls) - 1, len(self._verdicts_by_call) - 1)
        mapping = self._verdicts_by_call[index]
        return [
            RuleVerdict(
                rule_id=r.rule_id,
                violations=[RuleViolation(node_id=nid, detail=detail) for nid, detail in mapping.get(r.rule_id, [])],
            )
            for r in rules
        ]


def _fake_graph_initializer_leave001(**_kwargs: Any) -> dict[str, Any]:
    process = ProcessDefinition.model_validate({
        "meta": {
            "process_id": "LEAVE-001", "process_name": "测试请假流程", "version": "V0.1.0",
            "responsible_dept": "人力资源部", "description": "测试用流程",
            "applicant_scope": "全员", "entry_point": "OA系统",
        },
        "form_fields": [],
        "flow_nodes": [
            {"node_id": "draft", "node_name": "起草", "is_draft": True, "handler": None, "opinion": None,
             "opinion_label": None, "time_limit_days": None,
             "submit_paths": [{"path_name": "送审批", "condition": None, "target_node_id": "approval"}]},
            {"node_id": "approval", "node_name": "部门主管审批", "is_draft": False,
             "handler": {"mode": "单选-单人处理", "source": "本部门", "role": "部门主管", "source_field": None},
             "opinion": {"conclusive_required": True, "detail_required": False, "conclusive_options": ["同意", "不同意"]},
             "opinion_label": "审批意见", "time_limit_days": None,
             "submit_paths": [{"path_name": "流程结束", "condition": "结论性意见=同意", "target_node_id": "END"}]},
        ],
        "attachments": None, "roles": None,
    })
    payload = process.model_dump(mode="json")
    return {
        "output_paths": {}, "workflow_design_output": {"process_definition": payload},
        "designer_assistant_message": {}, "user_clarification_requests": [],
        "design_persistence_report": {}, "schema_validation_report": {}, "business_validation_result": {},
    }


def test_generate_draft_holds_newly_introduced_qualitative_violation_pending(service_factory) -> None:
    """真实场景复现：把"部门主管审批"改名成缺动作词的名字——诊断规则命中
    （trigger_attributes 含 node_name），智能门控判定这轮值得为它打 LLM，且
    stub judge 声明"改前合规、改后不合规"——应该被拦进待确认、走 compliance 分类。"""
    proposal = EditProposal(
        reply="将把「部门主管审批」环节的名称修改为「部门主管」。",
        operations=[UpdateFlowNode(node_id="approval", updates=FlowNodeUpdate(node_name="部门主管"))],
    )
    agent = FakeEditAgent(proposal)
    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "runtime.db"
        Slice1Service(db_path)
        service = WorkflowDesignService(db_path, graph_initializer=_fake_graph_initializer_leave001, edit_agent=agent)
        stub_judge = _SequencedStubJudge([
            {"design.node_naming_role_action": []},                              # before：改前的名字合规，无违规
            {"design.node_naming_role_action": [("approval", "缺动作词")]},  # after：改后的名字不合规
        ])
        service._compliance_judge = stub_judge

        session_id = _new_session(service)
        service.add_message(session_id=session_id, role="user", content="把部门主管审批改名成部门主管")
        payload = service.generate_draft(session_id=session_id)

        assert payload["session"]["has_pending_edit"] is True
        last_message = payload["messages"][-1]
        assert last_message["payload"]["event"] == "edit_pending_confirmation"
        assert last_message["payload"]["pending_reason"] == "compliance"
        findings = last_message["payload"]["compliance_findings"]
        assert any(f["rule_id"] == "design.node_naming_role_action" for f in findings)
        assert len(stub_judge.calls) == 2  # 触发了：before 一次、after 一次


def test_generate_draft_skips_qualitative_judge_call_for_unrelated_edit(service_factory) -> None:
    """智能门控的核心收益：改处理期限这种跟命名/handler/submit_paths 都不相关的编辑，
    不该触发定性规则检查——不调 judge，不多等那次 LLM。"""
    proposal = EditProposal(
        reply="将把「部门主管审批」环节的处理期限设为 3 天。",
        operations=[UpdateFlowNode(node_id="approval", updates=FlowNodeUpdate(time_limit_days=3))],
    )
    agent = FakeEditAgent(proposal)
    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "runtime.db"
        Slice1Service(db_path)
        service = WorkflowDesignService(db_path, graph_initializer=_fake_graph_initializer_leave001, edit_agent=agent)
        stub_judge = _SequencedStubJudge([{}])
        service._compliance_judge = stub_judge

        session_id = _new_session(service)
        service.add_message(session_id=session_id, role="user", content="给部门主管审批设置3天处理期限")
        payload = service.generate_draft(session_id=session_id)

        last_message = payload["messages"][-1]
        assert last_message["payload"]["pending_reason"] != "compliance"  # 没有定性违规
        assert stub_judge.calls == []  # 智能门控判定不相关，压根没调 judge


def test_generate_draft_surfaces_related_rules_when_edit_touches_governed_term(service_factory) -> None:
    """主动提醒：改路由条件时碰到了制度规则管辖的词（这里把审批路径条件加上'请假天数>7天'，
    命中 leave.line_leader_for_long_or_special / leave.gm_approval_present 管辖的'请假天数'），
    待确认卡的 payload 里应带上 related_rules 提醒——不判违规，纯确定性文本命中。"""
    proposal = EditProposal(
        reply="将把「部门主管审批」的流程结束路径条件补上请假天数上限。",
        operations=[UpdateSubmitPath(
            node_id="approval", path_name="流程结束",
            updates=SubmitPathUpdate(condition="结论性意见=同意 且 请假天数>7天"),
        )],
    )
    agent = FakeEditAgent(proposal)
    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "runtime.db"
        Slice1Service(db_path)
        service = WorkflowDesignService(db_path, graph_initializer=_fake_graph_initializer_leave001, edit_agent=agent)
        service._compliance_judge = _SequencedStubJudge([{}])  # 定性判定返回无违规，隔离出 related_rules

        session_id = _new_session(service)
        service.add_message(session_id=session_id, role="user", content="把流程结束条件加上请假天数>7天")
        payload = service.generate_draft(session_id=session_id)

        related = payload["messages"][-1]["payload"]["related_rules"]
        related_ids = {r["rule_id"] for r in related}
        assert "leave.line_leader_for_long_or_special" in related_ids
        # 提醒项带出处，供卡片引用
        r = next(r for r in related if r["rule_id"] == "leave.line_leader_for_long_or_special")
        assert r["source_doc"] and r["clause"] and r["statement"]


def test_generate_draft_related_rules_empty_for_untouched_edit(service_factory) -> None:
    """改处理期限这种没碰到任何制度管辖词的编辑——related_rules 为空，不刷屏。"""
    proposal = EditProposal(
        reply="将把「部门主管审批」处理期限设为 3 天。",
        operations=[UpdateFlowNode(node_id="approval", updates=FlowNodeUpdate(time_limit_days=3))],
    )
    agent = FakeEditAgent(proposal)
    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "runtime.db"
        Slice1Service(db_path)
        service = WorkflowDesignService(db_path, graph_initializer=_fake_graph_initializer_leave001, edit_agent=agent)
        service._compliance_judge = _SequencedStubJudge([{}])

        session_id = _new_session(service)
        service.add_message(session_id=session_id, role="user", content="设3天处理期限")
        payload = service.generate_draft(session_id=session_id)
        assert payload["messages"][-1]["payload"]["related_rules"] == []
