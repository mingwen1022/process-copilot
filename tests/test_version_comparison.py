from __future__ import annotations

from app.analytics.version_comparison import compare_versions, render_comparison_markdown


def _metrics(duration, rework, node_dwell, sla, reach_visits, case_count=600):
    return {
        "overview": {
            "case_count": case_count,
            "avg_case_duration_hours": duration,
            "avg_node_visits": 4.0,
            "avg_last_node_dwell_hours": 20.0,
            "manual_intervention_case_ratio": 0.05,
            "completion_rate": 0.98,
        },
        "path_metrics": {"rework_rate": rework, "conformance_violation_count": 5},
        "node_metrics": {
            "line_leader": {"avg_dwell_hours": node_dwell, "sla_achievement_rate": sla, "return_rate": 0.05, "visits": reach_visits, "avg_visits_per_case": 1.0},
        },
        "node_names": {"line_leader": "条线分管领导审批"},
        "origination_metrics": {"by_case_attribute": {"请假类型": {"年假": 300, "事假": 300}}},
    }


def test_improvement_deltas_and_directions() -> None:
    v1 = _metrics(duration=76.0, rework=0.42, node_dwell=116.0, sla=0.27, reach_visits=170)
    v2 = _metrics(duration=23.0, rework=0.16, node_dwell=15.0, sla=0.90, reach_visits=83)
    cmp = compare_versions(v1, v2)

    duration = next(d for d in cmp.overview if d.key == "avg_case_duration_hours")
    assert duration.delta == -53.0
    assert duration.improved is True  # down 方向，变小=改善
    assert round(duration.pct_change, 2) == round(-53.0 / 76.0, 2)

    rework = next(d for d in cmp.path if d.key == "rework_rate")
    assert rework.improved is True


def test_sla_is_up_direction() -> None:
    v1 = _metrics(76.0, 0.42, 116.0, 0.27, 170)
    v2 = _metrics(23.0, 0.16, 15.0, 0.90, 83)
    cmp = compare_versions(v1, v2)
    sla = next(m for n in cmp.nodes for m in n.metrics if m.key == "sla_achievement_rate")
    assert sla.delta == round(0.90 - 0.27, 4)
    assert sla.improved is True  # up 方向，变大=改善


def test_regression_is_flagged_not_hidden() -> None:
    # v2 办结时长反而变长 → 应标为未改善
    v1 = _metrics(50.0, 0.4, 100.0, 0.3, 170)
    v2 = _metrics(60.0, 0.4, 100.0, 0.3, 170)
    cmp = compare_versions(v1, v2)
    duration = next(d for d in cmp.overview if d.key == "avg_case_duration_hours")
    assert duration.improved is False


def test_no_change_is_neutral() -> None:
    v1 = _metrics(50.0, 0.4, 100.0, 0.3, 170)
    v2 = _metrics(50.0, 0.4, 100.0, 0.3, 170)
    cmp = compare_versions(v1, v2)
    duration = next(d for d in cmp.overview if d.key == "avg_case_duration_hours")
    assert duration.delta == 0.0
    assert duration.improved is None


def test_line_leader_reach_ratio_computed() -> None:
    v1 = _metrics(76.0, 0.42, 116.0, 0.27, reach_visits=170, case_count=600)
    v2 = _metrics(23.0, 0.16, 15.0, 0.90, reach_visits=83, case_count=600)
    cmp = compare_versions(v1, v2)
    assert cmp.line_leader_reach_ratio_v1 == round(170 / 600, 4)
    assert cmp.line_leader_reach_ratio_v2 == round(83 / 600, 4)


def test_headline_and_markdown() -> None:
    v1 = _metrics(76.0, 0.42, 116.0, 0.27, 170)
    v2 = _metrics(23.0, 0.16, 15.0, 0.90, 83)
    cmp = compare_versions(v1, v2)
    assert "76h → 23h" in cmp.headline
    md = render_comparison_markdown(cmp)
    assert "v1 vs v2 效能对比" in md
    assert "平均办结时长" in md
    assert "条线分管领导审批" in md
