from __future__ import annotations

from datetime import datetime, timedelta

from app.analytics.query import (
    AdHocMetricQuery,
    MetricFilter,
    load_custom_metrics,
    run_ad_hoc_query,
    upsert_custom_metric,
)
from data.schema import ActionCategory, CaseRecord, CaseStatus, EventRecord


def _event(**overrides) -> EventRecord:
    base = dict(
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
        action="送部门总经理审批",
        action_category=ActionCategory.ROUTE_FORWARD,
        target_node_id="dept_gm",
    )
    base.update(overrides)
    return EventRecord.model_validate(base)


def _case(case_id, leave_type, leave_days, status=CaseStatus.COMPLETED, duration_h=10, events=None) -> CaseRecord:
    created = datetime(2026, 1, 1, 8, 0, 0)
    closed = None if status != CaseStatus.COMPLETED else created + timedelta(hours=duration_h)
    return CaseRecord(
        case_id=case_id,
        flow_code="LEAVE-001",
        flow_name="员工请假申请流程",
        process_version="V1.0.0",
        case_status=status,
        initiator_user_id="u0",
        initiator_name="李四",
        initiator_dept_name="研发部",
        case_attributes={"请假类型": leave_type, "请假天数": leave_days},
        created_at=created,
        closed_at=closed,
        events=events if events is not None else [_event(case_id=case_id)],
    )


def _sample_cases():
    return [
        _case("c1", "年假", 12, duration_h=20),
        _case("c2", "事假", 2, duration_h=5),
        _case("c3", "病假", 8, duration_h=30),
        _case("c4", "年假", 3, status=CaseStatus.IN_PROGRESS),
    ]


def test_count_with_numeric_filter() -> None:
    query = AdHocMetricQuery(
        name="超长请假数", scope="case",
        filters=[MetricFilter(field="请假天数", operator=">", value=7)],
        aggregate="count",
    )
    result = run_ad_hoc_query(query, _sample_cases())
    assert result.matched == 2  # c1(12), c3(8)
    assert result.value == 2
    assert result.total == 4


def test_rate_aggregate() -> None:
    query = AdHocMetricQuery(
        name="年假占比", scope="case",
        filters=[MetricFilter(field="请假类型", operator="=", value="年假")],
        aggregate="rate",
    )
    result = run_ad_hoc_query(query, _sample_cases())
    assert result.matched == 2
    assert result.value == round(2 / 4, 4)


def test_avg_dwell_hours_case_scope_skips_unclosed() -> None:
    query = AdHocMetricQuery(
        name="年假平均办结时长", scope="case",
        filters=[MetricFilter(field="请假类型", operator="=", value="年假")],
        aggregate="avg_dwell_hours",
    )
    result = run_ad_hoc_query(query, _sample_cases())
    # c1=20h 已办结，c4 进行中(duration None) 被跳过 → 平均=20
    assert result.value == 20.0


def test_in_operator() -> None:
    query = AdHocMetricQuery(
        name="特殊假类型数", scope="case",
        filters=[MetricFilter(field="请假类型", operator="in", value=["病假", "事假"])],
        aggregate="count",
    )
    result = run_ad_hoc_query(query, _sample_cases())
    assert result.matched == 2


def test_group_by_count() -> None:
    query = AdHocMetricQuery(
        name="各类型案例数", scope="case", filters=[], aggregate="count", group_by="请假类型",
    )
    result = run_ad_hoc_query(query, _sample_cases())
    assert result.value == {"事假": 1, "年假": 2, "病假": 1}


def test_case_scope_can_filter_by_visited_node_name() -> None:
    """回归：真实使用中 LLM 自然地想用 scope=case + node_name 过滤"哪些实例经过
    了某环节"，但案例行原来没有 node_name 字段（只有 event 行才有），导致恒为 0
    命中。这里用真实事件序列（含条线分管领导审批）复现并验证修复。"""
    reached = _case(
        "c1", "年假", 12,
        events=[
            _event(case_id="c1", node_id="dept_supervisor", node_name="部门主管审批"),
            _event(case_id="c1", node_id="dept_gm", node_name="部门总经理审批"),
            _event(case_id="c1", node_id="line_leader", node_name="条线分管领导审批"),
        ],
    )
    not_reached = _case(
        "c2", "事假", 2,
        events=[_event(case_id="c2", node_id="dept_supervisor", node_name="部门主管审批")],
    )
    cases = [reached, not_reached]

    query = AdHocMetricQuery(
        name="经过条线分管领导审批的流程数", scope="case",
        filters=[MetricFilter(field="node_name", operator="=", value="条线分管领导审批")],
        aggregate="count",
    )
    result = run_ad_hoc_query(query, cases)
    assert result.matched == 1  # 只有 c1 经过；修复前恒为 0
    assert result.total == 2


def test_case_scope_node_id_membership_with_in_operator() -> None:
    reached = _case("c1", "年假", 12, events=[_event(case_id="c1", node_id="line_leader", node_name="条线分管领导审批")])
    not_reached = _case("c2", "事假", 2, events=[_event(case_id="c2", node_id="dept_supervisor", node_name="部门主管审批")])
    query = AdHocMetricQuery(
        name="经过条线分管领导或总经理", scope="case",
        filters=[MetricFilter(field="node_id", operator="in", value=["line_leader", "dept_gm"])],
        aggregate="count",
    )
    result = run_ad_hoc_query(query, [reached, not_reached])
    assert result.matched == 1


def test_case_scope_node_name_not_equal_excludes_visited() -> None:
    reached = _case("c1", "年假", 12, events=[_event(case_id="c1", node_id="line_leader", node_name="条线分管领导审批")])
    not_reached = _case("c2", "事假", 2, events=[_event(case_id="c2", node_id="dept_supervisor", node_name="部门主管审批")])
    query = AdHocMetricQuery(
        name="未经过条线分管领导审批的流程数", scope="case",
        filters=[MetricFilter(field="node_name", operator="!=", value="条线分管领导审批")],
        aggregate="count",
    )
    result = run_ad_hoc_query(query, [reached, not_reached])
    assert result.matched == 1  # 只有 c2 没经过


def test_event_scope_filter_and_avg_dwell() -> None:
    events = [
        _event(case_id="c1", node_id="dept_gm", node_name="部门总经理审批", dwell_seconds=4 * 3600),
        _event(case_id="c1", node_id="dept_supervisor", node_name="部门主管审批", dwell_seconds=2 * 3600),
    ]
    cases = [_case("c1", "年假", 12, events=events)]
    query = AdHocMetricQuery(
        name="总经理环节平均时长", scope="event",
        filters=[MetricFilter(field="node_id", operator="=", value="dept_gm")],
        aggregate="avg_dwell_hours",
    )
    result = run_ad_hoc_query(query, cases)
    assert result.matched == 1
    assert result.value == 4.0


def test_multiple_filters_are_conjunctive() -> None:
    query = AdHocMetricQuery(
        name="超长年假数", scope="case",
        filters=[
            MetricFilter(field="请假类型", operator="=", value="年假"),
            MetricFilter(field="请假天数", operator=">", value=7),
        ],
        aggregate="count",
    )
    result = run_ad_hoc_query(query, _sample_cases())
    assert result.matched == 1  # 仅 c1(年假,12)


def test_not_equal_operator() -> None:
    query = AdHocMetricQuery(
        name="非年假数", scope="case",
        filters=[MetricFilter(field="请假类型", operator="!=", value="年假")],
        aggregate="count",
    )
    result = run_ad_hoc_query(query, _sample_cases())
    assert result.matched == 2  # c2 事假, c3 病假


def test_custom_metrics_upsert_and_load_round_trip(tmp_path) -> None:
    path = tmp_path / "custom_metrics.json"
    q1 = AdHocMetricQuery(name="超长请假数", scope="case", filters=[MetricFilter(field="请假天数", operator=">", value=7)], aggregate="count")
    upsert_custom_metric(path, q1)
    loaded = load_custom_metrics(path)
    assert len(loaded) == 1
    assert loaded[0].name == "超长请假数"
    assert loaded[0].persist is True  # 转正后强制 persist

    # 同名覆盖，不重复
    q1b = AdHocMetricQuery(name="超长请假数", scope="case", filters=[MetricFilter(field="请假天数", operator=">", value=10)], aggregate="count")
    upsert_custom_metric(path, q1b)
    loaded2 = load_custom_metrics(path)
    assert len(loaded2) == 1
    assert loaded2[0].filters[0].value == 10


def test_load_custom_metrics_missing_file_returns_empty(tmp_path) -> None:
    assert load_custom_metrics(tmp_path / "nope.json") == []
