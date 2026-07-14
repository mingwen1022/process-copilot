"""流程总览副驾（ManageCopilotService）测试——重点覆盖全局/跨流程效能问答这条新路径：
target_workflow_id 缺失或是模型吐的无效占位符时，应该遍历已接入效能分析的流程各自
作答再汇总，而不是直接把占位符或"暂未接入"甩给用户。"""

from __future__ import annotations

from typing import Any

from app.copilots.manage_service import ManageCopilotService, ManageRoute


class _StubAgent:
    """按顺序吐预设路由，替代真实 LLM 分类。"""

    def __init__(self, routes: list[ManageRoute]) -> None:
        self._routes = routes
        self.calls: list[dict[str, Any]] = []

    def classify(self, catalog, message, history=None):  # noqa: ANN001
        self.calls.append({"catalog": catalog, "message": message, "history": history})
        return self._routes[min(len(self.calls) - 1, len(self._routes) - 1)]


class _StubAnalyticsService:
    """替代真实 AnalyticsService.answer_question——记录被问了什么，回一句预设答案。"""

    def __init__(self, reply: str) -> None:
        self._reply = reply
        self.calls: list[dict[str, Any]] = []

    def answer_question(self, *, message: str, history=None):  # noqa: ANN001
        self.calls.append({"message": message, "history": history})
        return {"reply": self._reply}


class _StubSynthesisModel:
    """替代跨流程汇总用的 LLM——把收到的 user content 原样吐回去，方便断言拼接内容对不对。"""

    def __init__(self) -> None:
        self.invocations: list[list[dict[str, str]]] = []

    def invoke(self, messages):  # noqa: ANN001
        self.invocations.append(messages)

        class _Resp:
            content = "已汇总：" + messages[-1]["content"]

        return _Resp()


class _StubSlice1:
    def __init__(self, defs: list[dict[str, Any]]) -> None:
        self._defs = defs

    def workflow_definitions(self, user_id: str | None = None):  # noqa: ANN001
        return self._defs


class _StubWorkflowDesign:
    def design_summary_for_workflow(self, workflow_definition_id: str):  # noqa: ANN001
        return None


def _def_row(workflow_id: str, name: str, *, status: str = "PUBLISHED", issue_count: int = 0) -> dict[str, Any]:
    return {
        "workflow_definition_id": f"wfd_{workflow_id.lower()}",
        "workflow_id": workflow_id,
        "name": name,
        "status": status,
        "management": {"issue_count": issue_count},
    }


def _service(
    *,
    routes: list[ManageRoute],
    defs: list[dict[str, Any]],
    analytics_by_workflow_id: dict[str, Any] | None = None,
    model: Any | None = None,
) -> tuple[ManageCopilotService, _StubAgent]:
    agent = _StubAgent(routes)
    svc = ManageCopilotService(
        slice1_service=_StubSlice1(defs),
        workflow_design_service=_StubWorkflowDesign(),
        analytics_by_workflow_id=analytics_by_workflow_id or {},
        agent=agent,
        model=model,
    )
    return svc, agent


def test_analytics_answer_for_specific_flow_uses_its_own_service() -> None:
    leave_analytics = _StubAnalyticsService("请假流程有 7 单在跑。")
    svc, _agent = _service(
        routes=[ManageRoute(route="analytics", target_workflow_id="LEAVE-001")],
        defs=[_def_row("LEAVE-001", "员工请假申请流程")],
        analytics_by_workflow_id={"LEAVE-001": leave_analytics},
    )
    result = svc.ask("请假流程现在有多少单在跑")
    assert result["reply"] == "请假流程有 7 单在跑。"
    assert leave_analytics.calls[0]["message"] == "请假流程现在有多少单在跑"


def test_analytics_answer_reports_not_connected_for_specific_unwired_flow() -> None:
    svc, _agent = _service(
        routes=[ManageRoute(route="analytics", target_workflow_id="SEAL-001")],
        defs=[_def_row("SEAL-001", "用印申请流程")],
        analytics_by_workflow_id={},
    )
    result = svc.ask("用印流程效率怎么样")
    assert "用印申请流程" in result["reply"]
    assert "暂未接入" in result["reply"]


def test_global_analytics_question_aggregates_across_wired_flows() -> None:
    """核心场景：跨流程问题（target_workflow_id 留空）应该遍历所有已接入效能数据的流程
    各自作答，再汇总成一句话——不是直接说"暂未接入"或泄漏占位符。"""
    leave_analytics = _StubAnalyticsService("请假流程：进行中 7 单，共 600 单。")
    expense_analytics = _StubAnalyticsService("报销流程：进行中 3 单，共 400 单。")
    model = _StubSynthesisModel()
    svc, agent = _service(
        routes=[ManageRoute(route="analytics", target_workflow_id=None)],
        defs=[
            _def_row("LEAVE-001", "员工请假申请流程"),
            _def_row("EXPENSE-001", "费用报销流程"),
            _def_row("SEAL-001", "用印申请流程"),  # 未接入效能数据
        ],
        analytics_by_workflow_id={"LEAVE-001": leave_analytics, "EXPENSE-001": expense_analytics},
        model=model,
    )
    result = svc.ask("在跑的流程单子有多少")

    assert leave_analytics.calls and leave_analytics.calls[0]["message"] == "在跑的流程单子有多少"
    assert expense_analytics.calls and expense_analytics.calls[0]["message"] == "在跑的流程单子有多少"
    synthesis_input = model.invocations[0][-1]["content"]
    assert "请假流程：进行中 7 单，共 600 单。" in synthesis_input
    assert "报销流程：进行中 3 单，共 400 单。" in synthesis_input
    assert "用印申请流程" in synthesis_input  # 未接入的流程也要如实告知模型，不能让它假装全量
    assert result["reply"].startswith("已汇总：")
    assert result["target_workflow_id"] is None


def test_analytics_answer_treats_model_placeholder_id_as_unspecified() -> None:
    """回归：模型结构化输出偶尔不留空，吐一个目录里不存在的占位符字符串（如 "<UNKNOWN>"）
    ——不该把这个无意义字符串直接展示给用户，应该当成"没指定具体流程"，走全局聚合。"""
    leave_analytics = _StubAnalyticsService("请假流程：进行中 7 单。")
    model = _StubSynthesisModel()
    svc, _agent = _service(
        routes=[ManageRoute(route="analytics", target_workflow_id="<UNKNOWN>")],
        defs=[_def_row("LEAVE-001", "员工请假申请流程")],
        analytics_by_workflow_id={"LEAVE-001": leave_analytics},
        model=model,
    )
    result = svc.ask("在跑的流程单子有多少")

    assert "<UNKNOWN>" not in result["reply"]
    assert leave_analytics.calls  # 确实走了全局聚合，问到了已接入的流程
    assert result["target_workflow_id"] is None


def test_global_analytics_question_reports_honestly_when_nothing_wired() -> None:
    svc, _agent = _service(
        routes=[ManageRoute(route="analytics", target_workflow_id=None)],
        defs=[_def_row("SEAL-001", "用印申请流程"), _def_row("PROC-001", "采购申请流程")],
        analytics_by_workflow_id={},
    )
    result = svc.ask("在跑的流程单子有多少")
    assert "没有任何流程接入效能分析数据" in result["reply"]
