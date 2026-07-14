from __future__ import annotations

from datetime import datetime

from data.schema import (
    ActionCategory,
    ArrivalConfig,
    CaseRecord,
    CaseStatus,
    DefectSpec,
    DefectType,
    DwellParam,
    EventRecord,
    ScenarioConfig,
)


def _sample_event(**overrides) -> EventRecord:
    base = dict(
        event_id="c1-ev1",
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


def test_event_record_round_trip() -> None:
    event = _sample_event()
    payload = event.model_dump_json()
    restored = EventRecord.model_validate_json(payload)
    assert restored == event


def test_case_record_to_flat_rows_merges_case_and_event_fields() -> None:
    event1 = _sample_event()
    event2 = _sample_event(event_id="c1-ev2", task_order=2, node_id="dept_gm", target_node_id="END")
    case = CaseRecord(
        case_id="c1",
        flow_code="LEAVE-001",
        flow_name="员工请假申请流程",
        process_version="V1.0.0",
        case_status=CaseStatus.COMPLETED,
        initiator_user_id="u0",
        initiator_name="李四",
        initiator_dept_id="d1",
        initiator_dept_name="研发部",
        case_attributes={"请假类型": "年假", "请假天数": 3},
        created_at=datetime(2026, 1, 1, 8, 0, 0),
        closed_at=datetime(2026, 1, 1, 12, 0, 0),
        events=[event1, event2],
        injected_defect_ids=[],
    )
    rows = case.to_flat_rows()
    assert len(rows) == 2
    assert rows[0]["case_id"] == "c1"
    assert rows[0]["attr_请假类型"] == "年假"
    assert rows[0]["node_id"] == "dept_supervisor"
    assert rows[1]["node_id"] == "dept_gm"


def test_scenario_config_round_trip_with_defects() -> None:
    scenario = ScenarioConfig(
        scenario_id="leave_v1",
        process_target_path="data/cases/leave_request/standard/target.json",
        case_count=5,
        start_date="2026-01-01",
        end_date="2026-01-31",
        arrival=ArrivalConfig(rate_per_day=2.0, peak_multiplier={"month_end": 1.5}),
        node_dwell_params={"dept_supervisor": DwellParam(mu=1.0, sigma=0.5)},
        decision_probabilities={"dept_supervisor": 0.9},
        assumed_sla_days={"dept_supervisor": 1.0},
        injected_defects=[
            DefectSpec(
                defect_id="slow_dept_supervisor",
                type=DefectType.SLOW_NODE,
                target_node_id="dept_supervisor",
                params={"dwell_multiplier": 5.0},
                affected_case_ratio=0.5,
            )
        ],
    )
    restored = ScenarioConfig.model_validate_json(scenario.model_dump_json())
    assert restored.injected_defects[0].type == DefectType.SLOW_NODE
    assert restored.node_dwell_params["dept_supervisor"].mu == 1.0


def test_defect_spec_affected_case_ratio_bounds() -> None:
    import pytest

    with pytest.raises(Exception):
        DefectSpec(defect_id="x", type=DefectType.SLOW_NODE, affected_case_ratio=1.5)
