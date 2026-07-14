from __future__ import annotations

from datetime import datetime

import pytest
from fastapi.testclient import TestClient

from app.api.analytics_service import AnalyticsService
from app.api.server import create_app
from data.schema import ActionCategory, ArrivalConfig, CaseRecord, CaseStatus, DwellParam, EventRecord, ScenarioConfig

PROCESS_PATH = "data/cases/leave_request/standard/target.json"


def _case() -> CaseRecord:
    event = EventRecord(
        event_id="e1",
        case_id="c1",
        task_order=1,
        node_id="dept_supervisor",
        node_name="部门主管审批",
        resource_user_id="u1",
        resource_name="张三",
        enter_time=datetime(2026, 1, 1, 9, 0, 0),
        leave_time=datetime(2026, 1, 1, 12, 0, 0),
        dwell_seconds=3 * 3600,
        action="流程结束",
        action_category=ActionCategory.END,
        target_node_id="END",
    )
    return CaseRecord(
        case_id="c1",
        flow_code="LEAVE-001",
        flow_name="员工请假申请流程",
        process_version="V1.0.0",
        case_status=CaseStatus.COMPLETED,
        initiator_user_id="u0",
        initiator_name="李四",
        initiator_dept_name="研发部",
        case_attributes={"请假类型": "事假", "请假天数": 2},
        created_at=datetime(2026, 1, 1, 8, 0, 0),
        closed_at=datetime(2026, 1, 1, 12, 0, 0),
        events=[event],
    )


def _write_fixture(tmp_path) -> AnalyticsService:
    scenario = ScenarioConfig(
        scenario_id="test_fixture",
        process_target_path=PROCESS_PATH,
        case_count=1,
        start_date="2026-01-01",
        end_date="2026-01-31",
        arrival=ArrivalConfig(rate_per_day=1.0),
        node_dwell_params={"dept_supervisor": DwellParam(mu=0.5, sigma=0.4)},
        decision_probabilities={"dept_supervisor": 0.9},
        assumed_sla_days={"dept_supervisor": 1.0},
    )
    (tmp_path / "scenario_v1.json").write_text(scenario.model_dump_json(indent=2), encoding="utf-8")
    (tmp_path / "event_log_v1.jsonl").write_text(_case().model_dump_json() + "\n", encoding="utf-8")
    return AnalyticsService(analytics_dir=tmp_path)


def _write_v2_fixture(tmp_path) -> None:
    scenario = ScenarioConfig(
        scenario_id="test_fixture_v2",
        process_target_path=PROCESS_PATH,
        case_count=1,
        start_date="2026-01-01",
        end_date="2026-01-31",
        arrival=ArrivalConfig(rate_per_day=1.0),
        node_dwell_params={"dept_supervisor": DwellParam(mu=0.5, sigma=0.4)},
        decision_probabilities={"dept_supervisor": 0.9},
        assumed_sla_days={"dept_supervisor": 1.0},
    )
    (tmp_path / "scenario_v2.json").write_text(scenario.model_dump_json(indent=2), encoding="utf-8")
    (tmp_path / "event_log_v2.jsonl").write_text(_case().model_dump_json() + "\n", encoding="utf-8")


def _bottleneck_case() -> CaseRecord:
    """一条案例走完请假流程全部 4 个环节，line_leader 环节耗时远超其它环节
    （100h vs 1h/2h/0.2h），确定性触发 detect_candidates() 的 slow_node 候选——
    不依赖真实合成数据集，构造到刚好越阈，供反哺闭环测试用。"""

    def ev(order, node_id, node_name, hours, target):
        start = datetime(2026, 1, 1, 8, 0, 0)
        return EventRecord(
            event_id=f"be{order}", case_id="bc1", task_order=order, node_id=node_id, node_name=node_name,
            resource_user_id="u1", resource_name="张三", enter_time=start, leave_time=start,
            dwell_seconds=hours * 3600, action="流转", action_category=ActionCategory.ROUTE_FORWARD,
            target_node_id=target,
        )

    events = [
        ev(1, "draft", "起草", 0.2, "dept_supervisor"),
        ev(2, "dept_supervisor", "部门主管审批", 1.0, "dept_gm"),
        ev(3, "dept_gm", "部门总经理审批", 2.0, "line_leader"),
        ev(4, "line_leader", "条线分管领导审批", 100.0, "END"),
    ]
    return CaseRecord(
        case_id="bc1", flow_code="LEAVE-001", flow_name="员工请假申请流程", process_version="V1.0.0",
        case_status=CaseStatus.COMPLETED, initiator_user_id="u0", initiator_name="李四",
        initiator_dept_name="研发部", case_attributes={"请假类型": "事假", "请假天数": 2},
        created_at=datetime(2026, 1, 1, 8, 0, 0), closed_at=datetime(2026, 1, 6, 0, 0, 0), events=events,
    )


def _write_bottleneck_fixture(tmp_path) -> AnalyticsService:
    scenario = ScenarioConfig(
        scenario_id="bottleneck_fixture", process_target_path=PROCESS_PATH, case_count=1,
        start_date="2026-01-01", end_date="2026-01-31", arrival=ArrivalConfig(rate_per_day=1.0),
        node_dwell_params={"dept_supervisor": DwellParam(mu=0.5, sigma=0.4)},
        decision_probabilities={"dept_supervisor": 0.9}, assumed_sla_days={"dept_supervisor": 1.0},
    )
    (tmp_path / "scenario_v1.json").write_text(scenario.model_dump_json(indent=2), encoding="utf-8")
    (tmp_path / "event_log_v1.jsonl").write_text(_bottleneck_case().model_dump_json() + "\n", encoding="utf-8")
    from app.insights import InsightStore

    return AnalyticsService(analytics_dir=tmp_path, insight_store=InsightStore())


def test_dashboard_metrics_reads_scenario_and_event_log(tmp_path) -> None:
    service = _write_fixture(tmp_path)
    result = service.dashboard_metrics()
    assert result["overview"]["case_count"] == 1
    assert result["overview"]["case_status_counts"] == {"正常结束": 1}
    assert "dept_supervisor" in result["node_metrics"]
    assert result["node_names"]["dept_supervisor"] == "部门主管审批"


def test_dashboard_metrics_raises_runtime_error_when_files_missing(tmp_path) -> None:
    service = AnalyticsService(analytics_dir=tmp_path / "does-not-exist")
    with pytest.raises(RuntimeError):
        service.dashboard_metrics()


def test_analytics_endpoint_returns_metrics_json(tmp_path) -> None:
    service = _write_fixture(tmp_path)
    client = TestClient(create_app(analytics_service=service))
    response = client.get("/api/v1/analytics/leave-request/metrics")
    assert response.status_code == 200
    body = response.json()
    assert body["overview"]["case_count"] == 1
    assert "node_metrics" in body and "path_metrics" in body


def test_analytics_endpoint_returns_400_when_data_missing(tmp_path) -> None:
    service = AnalyticsService(analytics_dir=tmp_path / "missing")
    client = TestClient(create_app(analytics_service=service))
    response = client.get("/api/v1/analytics/leave-request/metrics")
    assert response.status_code == 400


def test_diagnosis_endpoint_uses_injected_agent(tmp_path) -> None:
    from app.agents.process_analysis_agent import DiagnosisReport, ProcessAnalysisAgent

    class StubAgent(ProcessAnalysisAgent):
        def __init__(self) -> None:  # 不触发 Bedrock client
            pass

        def diagnose(self, metrics):
            assert metrics["process_name"] == "员工请假申请流程"
            return DiagnosisReport(process_name=metrics["process_name"], candidate_count=0, summary="stub")

    service = _write_fixture(tmp_path)
    service._analysis_agent = StubAgent()
    client = TestClient(create_app(analytics_service=service))
    response = client.post("/api/v1/analytics/leave-request/diagnosis")
    assert response.status_code == 200
    body = response.json()
    assert body["summary"] == "stub"
    assert body["process_name"] == "员工请假申请流程"


def _stub_diagnosis_service(tmp_path):
    from app.agents.process_analysis_agent import DiagnosisReport, ProcessAnalysisAgent

    class StubAgent(ProcessAnalysisAgent):
        def __init__(self) -> None:
            self.calls = 0

        def diagnose(self, metrics):
            self.calls += 1
            return DiagnosisReport(process_name=metrics["process_name"], candidate_count=1, summary=f"stub-{self.calls}")

    service = _write_fixture(tmp_path)
    service._analysis_agent = StubAgent()
    return service


def test_each_generation_is_archived_with_id_and_timestamp(tmp_path) -> None:
    service = _stub_diagnosis_service(tmp_path)
    first = service.diagnosis_report()
    second = service.diagnosis_report()
    assert first["generated_at"] and first["report_id"]
    assert first["report_id"] != second["report_id"]  # 每份独立存档，不覆盖
    assert (tmp_path / "diagnosis_history.json").exists()

    # 用一个"不带 agent"的新服务实例读回——证明来自磁盘、不依赖内存/LLM
    reread = AnalyticsService(analytics_dir=tmp_path)
    overview = reread.diagnosis_overview()
    assert [r["report_id"] for r in overview["reports"]] == [second["report_id"], first["report_id"]]  # 最新在前
    assert overview["latest"]["summary"] == "stub-2"


def test_get_specific_historical_report_by_id(tmp_path) -> None:
    service = _stub_diagnosis_service(tmp_path)
    first = service.diagnosis_report()
    service.diagnosis_report()  # 更新的一份
    # 仍能按 id 打开早先那份
    assert service.get_diagnosis(first["report_id"])["summary"] == "stub-1"
    assert service.get_diagnosis("nonexistent") is None


def test_diagnosis_overview_is_empty_before_any_generation(tmp_path) -> None:
    service = _write_fixture(tmp_path)
    overview = service.diagnosis_overview()
    assert overview == {"reports": [], "latest": None}


def test_diagnosis_endpoints_list_generate_and_fetch(tmp_path) -> None:
    service = _stub_diagnosis_service(tmp_path)
    client = TestClient(create_app(analytics_service=service))

    # 生成前：overview 空，且不触发生成
    before = client.get("/api/v1/analytics/leave-request/diagnosis")
    assert before.status_code == 200
    assert before.json() == {"reports": [], "latest": None}

    posted = client.post("/api/v1/analytics/leave-request/diagnosis").json()
    report_id = posted["report_id"]

    overview = client.get("/api/v1/analytics/leave-request/diagnosis").json()
    assert len(overview["reports"]) == 1
    assert overview["latest"]["report_id"] == report_id

    detail = client.get(f"/api/v1/analytics/leave-request/diagnosis/{report_id}").json()
    assert detail["report"]["summary"] == "stub-1"

    missing = client.get("/api/v1/analytics/leave-request/diagnosis/nope").json()
    assert missing["report"] is None


class _StubQueryAgent:
    """替身：不跑真的 ReAct 循环，但真的调用 answer_question 传进来的 tools
    （跟真实 create_agent 调用工具的方式一致），这样 typed 查询执行/常态化指标
    落库这些副作用仍然走真实代码，不是伪造结果。"""

    def __init__(self, reply: str, calls: list[tuple[str, dict]] | None = None) -> None:
        self.reply = reply
        self.calls = calls or []
        self.seen_snapshot = None
        self.seen_custom_names = None

    def run_with_tools(
        self, *, message, metrics_snapshot, schema_ddl, tools, conversation_context="", custom_metric_names=None,
        design_candidates=None, max_tool_calls=4
    ):
        import json

        from app.agents.analytics_query_agent import AnalyticsToolRunResult, ToolCallTrace

        self.seen_snapshot = metrics_snapshot
        self.seen_custom_names = custom_metric_names
        by_name = {t.name: t for t in tools}
        trace: list[ToolCallTrace] = []
        for tool_name, args in self.calls:
            raw = by_name[tool_name].invoke(args)
            result = json.loads(raw) if isinstance(raw, str) else raw
            trace.append(ToolCallTrace(tool=tool_name, args=args, result=result))
        return AnalyticsToolRunResult(reply=self.reply, tool_calls=trace)


def test_answer_question_executes_query_and_returns_result(tmp_path) -> None:
    from app.analytics.query import AdHocMetricQuery, MetricFilter

    query = AdHocMetricQuery(
        name="事假案例数", scope="case",
        filters=[MetricFilter(field="请假类型", operator="=", value="事假")],
        aggregate="count",
    )
    service = _write_fixture(tmp_path)  # fixture 里那条案例请假类型=事假
    service._query_agent = _StubQueryAgent(
        "这就为你统计事假案例数。",
        calls=[("run_metric_query", {"query_json": query.model_dump_json()})],
    )
    result = service.answer_question(message="事假有几个")
    assert result["reply"].startswith("这就")
    assert len(result["query_results"]) == 1
    assert result["query_results"][0]["value"] == 1
    assert result["persisted_metric_names"] == []  # persist=False


def test_answer_question_persists_metric_and_shows_in_dashboard(tmp_path) -> None:
    from app.analytics.query import AdHocMetricQuery, MetricFilter

    query = AdHocMetricQuery(
        name="事假案例数", scope="case",
        filters=[MetricFilter(field="请假类型", operator="=", value="事假")],
        aggregate="count", persist=True,
    )
    service = _write_fixture(tmp_path)
    service._query_agent = _StubQueryAgent(
        "已把它设为常态化跟踪。",
        calls=[("run_metric_query", {"query_json": query.model_dump_json()})],
    )
    result = service.answer_question(message="以后都帮我盯着事假案例数")
    assert result["persisted_metric_names"] == ["事假案例数"]

    # 转正后，看板的 custom_metrics 里应出现这个指标
    dashboard = service.dashboard_metrics()
    custom = {m["name"]: m for m in dashboard["custom_metrics"]}
    assert "事假案例数" in custom
    assert custom["事假案例数"]["value"] == 1


def test_answer_question_sql_tool_call_is_reported_as_sql_run(tmp_path) -> None:
    service = _write_fixture(tmp_path)
    service._query_agent = _StubQueryAgent(
        "共有 1 个案例。",
        calls=[("run_sql", {"sql": "SELECT COUNT(*) AS n FROM cases"})],
    )
    result = service.answer_question(message="一共有多少个案例（用 SQL 演示）")
    assert result["sql_runs"] == [
        {"sql": "SELECT COUNT(*) AS n FROM cases", "columns": ["n"], "rows": [[1]], "truncated": False, "error": None}
    ]


def test_answer_question_sql_guard_rejection_is_surfaced_with_error(tmp_path) -> None:
    service = _write_fixture(tmp_path)
    service._query_agent = _StubQueryAgent(
        "这条查询超出了允许范围，我拦下了它。",
        calls=[("run_sql", {"sql": "DROP TABLE cases"})],
    )
    result = service.answer_question(message="帮我删掉 cases 表（应被拦截）")
    assert len(result["sql_runs"]) == 1
    assert result["sql_runs"][0]["error"]
    assert "SELECT" in result["sql_runs"][0]["error"]


def test_ask_endpoint_uses_injected_query_agent(tmp_path) -> None:
    service = _write_fixture(tmp_path)
    service._query_agent = _StubQueryAgent("条线分管领导审批平均耗时较长。")
    client = TestClient(create_app(analytics_service=service))
    response = client.post("/api/v1/analytics/leave-request/ask", json={"message": "哪个环节最慢", "history": []})
    assert response.status_code == 200
    body = response.json()
    assert body["reply"].startswith("条线")
    assert body["query_results"] == []


def test_version_comparison_unavailable_when_v2_missing(tmp_path) -> None:
    service = _write_fixture(tmp_path)  # 只有 v1
    assert service.version_comparison() == {"available": False}


def test_version_comparison_available_with_both_versions(tmp_path) -> None:
    service = _write_fixture(tmp_path)
    _write_v2_fixture(tmp_path)
    result = service.version_comparison()
    assert result["available"] is True
    assert "overview" in result and "headline" in result
    assert result["v1_case_count"] == 1 and result["v2_case_count"] == 1


# ——— 反哺闭环 场景一：诊断报告生成时，确定性候选无条件全量反哺 ———

def _stub_bottleneck_service(tmp_path) -> AnalyticsService:
    """跟 _stub_diagnosis_service 同一个套路（替身 agent，不真打 Bedrock），
    只是数据换成会触发 slow_node 候选的 _write_bottleneck_fixture。"""
    from app.agents.process_analysis_agent import DiagnosisReport, ProcessAnalysisAgent

    class StubAgent(ProcessAnalysisAgent):
        def __init__(self) -> None:
            pass

        def diagnose(self, metrics):
            return DiagnosisReport(process_name=metrics["process_name"], candidate_count=0, summary="stub")

    service = _write_bottleneck_fixture(tmp_path)
    service._analysis_agent = StubAgent()
    return service


def test_diagnosis_report_records_slow_node_candidate_to_insight_store(tmp_path) -> None:
    service = _stub_bottleneck_service(tmp_path)
    service.diagnosis_report()

    # 这份 fixture 同时会触发 slow_node@line_leader 和一条流程级 conformance_violation，
    # 两者现在都反哺——按 kind 取具体这一条，不假设它是列表里唯一一条。
    opened = service._insight_store.open_for("LEAVE-001")
    insight = next(i for i in opened if i.kind.value == "slow_node")
    assert insight.node_id == "line_leader"
    assert "条线分管领导审批" in insight.headline
    assert insight.source.value == "analytics"


def test_seed_insights_records_candidates_without_diagnosis_report(tmp_path) -> None:
    """起始态预置：seed_insights 把确定性候选沉淀进 InsightStore，但不生成 AI 诊断报告、
    不写 diagnosis_history——洞察层有内容（设计侧待处理丰富），诊断报告页仍空（现场生成保留）。"""
    service = _write_bottleneck_fixture(tmp_path)  # 会触发 slow_node@line_leader 候选
    service.seed_insights()

    opened = service._insight_store.open_for("LEAVE-001")
    assert any(i.kind.value == "slow_node" and i.node_id == "line_leader" for i in opened)
    assert service.diagnosis_overview() == {"reports": [], "latest": None}  # 没生成诊断报告


def test_seed_insights_is_idempotent(tmp_path) -> None:
    """重启/切回起始态会重跑 seed——幂等聚合，不重复堆条目（occurrences 累加）。"""
    service = _write_bottleneck_fixture(tmp_path)
    service.seed_insights()
    n_first = len(service._insight_store.open_for("LEAVE-001"))
    service.seed_insights()
    assert len(service._insight_store.open_for("LEAVE-001")) == n_first


def test_seed_insights_is_noop_without_store(tmp_path) -> None:
    service = _write_bottleneck_fixture(tmp_path)
    service._insight_store = None
    service.seed_insights()  # 不应抛异常


def test_diagnosis_report_regeneration_aggregates_not_duplicates(tmp_path) -> None:
    """重复点"重新生成"不应该在反哺侧堆出多条同一问题的洞察——幂等聚合，occurrences 累加。"""
    service = _stub_bottleneck_service(tmp_path)
    service.diagnosis_report()
    service.diagnosis_report()

    # fixture 会触发 slow_node@line_leader + 流程级 conformance_violation 两条候选，
    # 两条各自都该聚合成 occurrences=2，而不是各自堆成两条。
    opened = service._insight_store.open_for("LEAVE-001")
    assert len(opened) == 2
    assert {i.occurrences for i in opened} == {2}


def test_diagnosis_report_is_noop_on_insights_without_store_injected(tmp_path) -> None:
    service = _stub_bottleneck_service(tmp_path)
    service._insight_store = None
    service.diagnosis_report()  # 不应该抛异常


# ——— 反哺闭环 场景二：对话中 flag_design_issue 只认服务端真实候选 ———

def test_flag_design_issue_records_real_candidate_when_agent_calls_it(tmp_path) -> None:
    service = _write_bottleneck_fixture(tmp_path)
    service._query_agent = _StubQueryAgent(
        "好的，已经帮你记下这个效能问题，设计侧会看到提醒。",
        calls=[("flag_design_issue", {"candidate_id": "slow_node:line_leader"})],
    )
    result = service.answer_question(message="条线分管领导审批这么慢，记一下给设计侧吧")

    assert result["design_flags"] == [
        {"recorded": True, "headline": result["design_flags"][0]["headline"], "node_name": "条线分管领导审批"}
    ]
    assert "条线分管领导审批" in result["design_flags"][0]["headline"]
    opened = service._insight_store.open_for("LEAVE-001")
    assert len(opened) == 1 and opened[0].kind.value == "slow_node"


def test_flag_design_issue_rejects_candidate_id_not_in_current_list(tmp_path) -> None:
    """回归：agent 不能凭空编一个 candidate_id 反哺——必须是服务端此刻真实算出的候选，
    不认识的 id 直接拒绝，不记录任何东西（对称于运维侧 reassign_user_id 的校验思路）。"""
    service = _write_bottleneck_fixture(tmp_path)
    service._query_agent = _StubQueryAgent(
        "我编了一个不存在的候选试试。",
        calls=[("flag_design_issue", {"candidate_id": "slow_node:not_a_real_node"})],
    )
    result = service.answer_question(message="随便记点什么")

    assert "error" in result["design_flags"][0]
    assert service._insight_store.open_for("LEAVE-001") == []


def test_flag_design_issue_records_process_level_candidate_without_node_id(tmp_path) -> None:
    """conformance_violation 是流程级候选（不落在具体环节上，node_id=None）——现在有对应
    InsightKind 了，agent 精确报出这个真实存在的 bottleneck_id 应该能正常反哺成功，
    记录下来的洞察 node_id 为 None（"流程级"），不需要绑定到某个环节。"""
    service = _write_bottleneck_fixture(tmp_path)
    service._query_agent = _StubQueryAgent(
        "好的，帮你记一下合规违规。", calls=[("flag_design_issue", {"candidate_id": "conformance_violation:process"})],
    )
    result = service.answer_question(message="记一下合规违规")
    assert result["design_flags"][0].get("recorded") is True
    opened = service._insight_store.open_for("LEAVE-001")
    conformance = next(i for i in opened if i.kind.value == "conformance_violation")
    assert conformance.node_id is None


def test_version_comparison_endpoint(tmp_path) -> None:
    service = _write_fixture(tmp_path)
    _write_v2_fixture(tmp_path)
    client = TestClient(create_app(analytics_service=service))
    response = client.get("/api/v1/analytics/leave-request/version-comparison")
    assert response.status_code == 200
    assert response.json()["available"] is True
