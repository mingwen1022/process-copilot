from __future__ import annotations

from datetime import datetime

from app.analytics.metrics import compute_metrics, load_case_records
from app.generation.event_log_generator import OPS_NODE_ID, generate_event_log, load_process_definition
from data.schema import (
    ActionCategory,
    ArrivalConfig,
    CaseRecord,
    CaseStatus,
    DwellParam,
    EventRecord,
    ScenarioConfig,
)

PROCESS_PATH = "data/cases/leave_request/standard/target.json"


def _event(**overrides) -> EventRecord:
    base = dict(
        event_id="ev1",
        case_id="c1",
        task_order=1,
        node_id="dept_supervisor",
        node_name="部门主管审批",
        resource_user_id="u1",
        resource_name="张三",
        resource_dept_id="d1",
        resource_dept_name="研发部",
        enter_time=datetime(2026, 1, 1, 9, 0, 0),
        leave_time=datetime(2026, 1, 1, 12, 0, 0),
        dwell_seconds=3 * 3600,
        action="送部门总经理审批",
        action_category=ActionCategory.ROUTE_FORWARD,
        target_node_id="dept_gm",
    )
    base.update(overrides)
    return EventRecord.model_validate(base)


def _case(**overrides) -> CaseRecord:
    base = dict(
        case_id="c1",
        flow_code="LEAVE-001",
        flow_name="员工请假申请流程",
        process_version="V1.0.0",
        case_status=CaseStatus.COMPLETED,
        initiator_user_id="u0",
        initiator_name="李四",
        initiator_dept_id="d1",
        initiator_dept_name="研发部",
        case_attributes={"请假类型": "事假", "请假天数": 2},
        created_at=datetime(2026, 1, 1, 8, 0, 0),
        closed_at=datetime(2026, 1, 1, 12, 0, 0),
        events=[_event()],
        injected_defect_ids=[],
    )
    base.update(overrides)
    return CaseRecord.model_validate(base)


def test_overview_metrics_counts_status_and_averages_only_completed() -> None:
    completed = _case(
        case_id="c1",
        case_status=CaseStatus.COMPLETED,
        created_at=datetime(2026, 1, 1, 8, 0, 0),
        closed_at=datetime(2026, 1, 1, 12, 0, 0),  # 4 小时
        events=[_event(dwell_seconds=4 * 3600, leave_time=datetime(2026, 1, 1, 12, 0, 0))],
    )
    in_progress = _case(case_id="c2", case_status=CaseStatus.IN_PROGRESS, closed_at=None)
    terminated = _case(case_id="c3", case_status=CaseStatus.TERMINATED)

    result = compute_metrics([completed, in_progress, terminated])
    overview = result["overview"]
    assert overview["case_count"] == 3
    assert overview["case_status_counts"] == {"正常结束": 1, "进行中": 1, "终止": 1}
    assert overview["completion_rate"] == round(1 / 3, 4)
    assert overview["avg_case_duration_hours"] == 4.0
    assert overview["avg_node_visits"] == 1.0
    assert overview["manual_intervention_case_count"] == 0


def test_node_metrics_dwell_return_rate_and_repeat_visits() -> None:
    # case1: dept_supervisor 被访问两次（退回重提），第二次才转发
    case1 = _case(
        case_id="c1",
        events=[
            _event(event_id="e1", task_order=1, action_category=ActionCategory.RETURN, target_node_id="DRAFT"),
            _event(event_id="e2", task_order=3, action_category=ActionCategory.ROUTE_FORWARD, target_node_id="dept_gm"),
        ],
    )
    # case2: dept_supervisor 只访问一次，直接转发
    case2 = _case(
        case_id="c2",
        events=[_event(event_id="e3", case_id="c2", action_category=ActionCategory.ROUTE_FORWARD, target_node_id="dept_gm")],
    )

    result = compute_metrics([case1, case2])
    node = result["node_metrics"]["dept_supervisor"]
    assert node["visits"] == 3
    assert node["avg_visits_per_case"] == round(3 / 2, 4)  # 2 个案例都经过该环节，共 3 次
    assert node["return_rate"] == round(1 / 3, 4)


def test_node_metrics_sla_achievement_rate() -> None:
    case = _case(
        events=[
            _event(event_id="e1", dwell_seconds=12 * 3600),  # 12h <= 24h SLA
            _event(event_id="e2", dwell_seconds=48 * 3600),  # 48h > 24h SLA
        ]
    )
    result = compute_metrics([case], assumed_sla_days={"dept_supervisor": 1.0})
    node = result["node_metrics"]["dept_supervisor"]
    assert node["sla_achievement_rate"] == 0.5


def test_node_metrics_no_sla_key_when_not_configured() -> None:
    case = _case()
    result = compute_metrics([case])
    assert "sla_achievement_rate" not in result["node_metrics"]["dept_supervisor"]


def test_path_metrics_rework_rate_and_variants() -> None:
    with_return = _case(
        case_id="c1",
        events=[
            _event(event_id="e1", node_id="dept_supervisor", action_category=ActionCategory.RETURN, target_node_id="DRAFT"),
            _event(event_id="e2", node_id="draft", node_name="起草", action_category=ActionCategory.ROUTE_FORWARD, target_node_id="dept_supervisor"),
            _event(event_id="e3", node_id="dept_supervisor", action_category=ActionCategory.END, target_node_id="END"),
        ],
    )
    without_return = _case(
        case_id="c2",
        events=[_event(event_id="e4", case_id="c2", node_id="dept_supervisor", action_category=ActionCategory.END, target_node_id="END")],
    )
    result = compute_metrics([with_return, without_return])
    path = result["path_metrics"]
    assert path["rework_case_count"] == 1
    assert path["rework_rate"] == 0.5
    assert path["distinct_variant_count"] == 2


def test_conformance_violation_detected_against_real_process_definition() -> None:
    process = load_process_definition(PROCESS_PATH)
    # 事假 2 天，结论性意见=同意（由 END 类别反推）应当走"流程结束"，
    # 这里故意记录成走向 dept_gm——应被判定为一次结构性违规。
    violating_case = _case(
        case_id="c1",
        case_attributes={"请假类型": "事假", "请假天数": 2},
        events=[_event(action_category=ActionCategory.ROUTE_FORWARD, target_node_id="dept_gm")],
    )
    conforming_case = _case(
        case_id="c2",
        case_attributes={"请假类型": "事假", "请假天数": 2},
        events=[_event(event_id="e2", case_id="c2", action_category=ActionCategory.END, target_node_id="END")],
    )
    result = compute_metrics([violating_case, conforming_case], process=process)
    violations = result["path_metrics"]["conformance_violations"]
    assert len(violations) == 1
    assert violations[0]["case_id"] == "c1"
    assert violations[0]["expected_target_node_id"] == "END"
    assert violations[0]["actual_target_node_id"] == "dept_gm"


def test_manual_intervention_metrics_prior_node_and_down_path() -> None:
    case = _case(
        events=[
            _event(
                event_id="e1",
                node_id="dept_supervisor",
                node_name="部门主管审批",
                action_category=ActionCategory.ROUTE_FORWARD,
                target_node_id="dept_gm",
                leave_time=datetime(2026, 1, 1, 12, 0, 0),
            ),
            _event(
                event_id="e2",
                node_id=OPS_NODE_ID,
                node_name="后台运维操作",
                resource_user_id="ops_yggl_001",
                resource_name="行政办公室秘书",
                enter_time=datetime(2026, 1, 1, 12, 0, 0),
                leave_time=datetime(2026, 1, 2, 12, 0, 0),
                dwell_seconds=24 * 3600,
                action="送部门总经理审批",
                action_category=ActionCategory.MANUAL_INTERVENTION,
                target_node_id="dept_gm",
            ),
        ]
    )
    result = compute_metrics([case])
    manual = result["manual_intervention"]
    assert manual["total_ops_events"] == 1
    assert manual["by_prior_node"] == {"部门主管审批": 1}
    assert manual["down_path_distribution"] == {"送部门总经理审批": 1}


def test_resource_metrics_excludes_ops_pseudo_node() -> None:
    case = _case(
        events=[
            _event(event_id="e1", resource_user_id="u1", resource_name="张三"),
            _event(
                event_id="e2",
                node_id=OPS_NODE_ID,
                resource_user_id="ops_yggl_001",
                resource_name="行政办公室秘书",
                action_category=ActionCategory.MANUAL_INTERVENTION,
            ),
        ]
    )
    result = compute_metrics([case])
    assert "ops_yggl_001" not in result["resource_metrics"]
    assert "u1" in result["resource_metrics"]
    assert result["resource_metrics"]["u1"]["visits"] == 1


def test_origination_metrics_by_department_attribute_and_month() -> None:
    case1 = _case(case_id="c1", initiator_dept_name="研发部", case_attributes={"请假类型": "年假", "请假天数": 3}, created_at=datetime(2026, 1, 5))
    case2 = _case(case_id="c2", initiator_dept_name="财务部", case_attributes={"请假类型": "事假", "请假天数": 1}, created_at=datetime(2026, 2, 3))
    result = compute_metrics([case1, case2])
    origination = result["origination_metrics"]
    assert origination["by_department"] == {"研发部": 1, "财务部": 1}
    assert origination["by_case_attribute"]["请假类型"] == {"年假": 1, "事假": 1}
    assert origination["monthly_trend"] == {"202601": 1, "202602": 1}


def test_load_case_records_round_trips_jsonl(tmp_path) -> None:
    case = _case()
    path = tmp_path / "log.jsonl"
    path.write_text(case.model_dump_json() + "\n", encoding="utf-8")
    loaded = load_case_records(path)
    assert len(loaded) == 1
    assert loaded[0] == case


def test_compute_metrics_end_to_end_on_generated_log() -> None:
    """跑一遍真实生成器产出的日志，只做结构性/合理性检查（非精确值），
    确认 Phase 1 → Phase 2 的接口是通的。"""
    process = load_process_definition(PROCESS_PATH)
    scenario = ScenarioConfig(
        scenario_id="metrics_smoke",
        process_target_path=PROCESS_PATH,
        case_count=80,
        start_date="2026-01-01",
        end_date="2026-02-28",
        arrival=ArrivalConfig(rate_per_day=3.0),
        node_dwell_params={
            "dept_supervisor": DwellParam(mu=0.5, sigma=0.4),
            "dept_gm": DwellParam(mu=0.8, sigma=0.4),
            "line_leader": DwellParam(mu=0.8, sigma=0.4),
        },
        decision_probabilities={"dept_supervisor": 0.9, "dept_gm": 0.9, "line_leader": 0.9},
        assumed_sla_days={"dept_supervisor": 1.0, "dept_gm": 2.0, "line_leader": 3.0},
        leave_type_weights={"年假": 0.5, "事假": 0.5},
        leave_days_range={"年假": (1, 10), "事假": (1, 5)},
        random_seed=3,
    )
    cases = generate_event_log(scenario, process=process)
    result = compute_metrics(cases, process=process, assumed_sla_days=scenario.assumed_sla_days)

    assert result["overview"]["case_count"] == 80
    assert result["overview"]["avg_case_duration_hours"] > 0
    assert "dept_supervisor" in result["node_metrics"]
    assert result["node_metrics"]["dept_supervisor"]["sla_achievement_rate"] is not None
    assert result["path_metrics"]["distinct_variant_count"] >= 1
    assert isinstance(result["path_metrics"]["conformance_violations"], list)
    assert isinstance(result["resource_metrics"], dict)
    assert result["origination_metrics"]["monthly_trend"]
