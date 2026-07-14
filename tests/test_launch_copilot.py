from __future__ import annotations

from app.copilots.launch_agent import LaunchAnswer
from app.copilots.launch_catalog import build_catalog, preview_path
from app.copilots.launch_service import LaunchCopilotService


class _StubAgent:
    def __init__(self, answer: LaunchAnswer) -> None:
        self._answer = answer
        self.seen: list[str] = []

    def answer(self, catalog, rules, user_message):  # noqa: ANN001
        self.seen.append(user_message)
        return self._answer


class _StubIndex:
    def semantic_search(self, query, *, k=6):  # noqa: ANN001
        return []


def _service(answer: LaunchAnswer) -> LaunchCopilotService:
    return LaunchCopilotService(agent=_StubAgent(answer), knowledge_index=_StubIndex())


def test_path_preview_deterministic_by_days() -> None:
    leave = build_catalog()[0].definition
    assert preview_path(leave, {"请假天数": 2}) == ["起草", "部门主管审批", "流程结束"]
    assert preview_path(leave, {"请假天数": 5}) == ["起草", "部门主管审批", "部门总经理审批", "流程结束"]
    assert preview_path(leave, {"请假天数": 10})[-2] == "条线分管领导审批"


def test_ask_attaches_path_preview_for_leave() -> None:
    svc = _service(LaunchAnswer(reply="你请8天病假，属于长假，会多一级审批。", recommended_process_id="LEAVE-001",
                                leave_type="病假", leave_days=8))
    result = svc.ask("我要请8天病假，该走哪个流程，会经过哪些环节？")
    assert result.recommended_process_id == "LEAVE-001"
    assert result.recommended_process_name == "员工请假申请流程"
    assert "条线分管领导审批" in result.path_preview  # 8天 → 走到条线领导


def test_ask_no_path_preview_for_catalog_only_process() -> None:
    # 推荐到一个只有目录项、无定义的流程 → 不预演路径，但仍给推荐
    svc = _service(LaunchAnswer(reply="这属于报销，走费用报销流程。", recommended_process_id="EXPENSE-001"))
    result = svc.ask("我垫付了差旅费想报销")
    assert result.recommended_process_id == "EXPENSE-001"
    assert result.path_preview == []


def test_ask_empty_message_short_circuits_without_agent() -> None:
    called: list[str] = []

    class _Track(_StubAgent):
        def answer(self, catalog, rules, user_message):  # noqa: ANN001
            called.append(user_message)
            return self._answer

    svc = LaunchCopilotService(agent=_Track(LaunchAnswer(reply="x")), knowledge_index=_StubIndex())
    result = svc.ask("   ")
    assert called == []  # 空输入不触发 agent
    assert "描述你的情况" in result.reply


def test_catalog_view_lists_processes() -> None:
    svc = _service(LaunchAnswer(reply="x"))
    items = svc.catalog_view()["items"]
    assert any(i["process_id"] == "LEAVE-001" for i in items)
    assert all("name" in i and "description" in i for i in items)
