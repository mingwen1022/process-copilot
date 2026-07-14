from __future__ import annotations

from app.analytics.thresholds import (
    CATEGORY_CONFORMANCE_VIOLATION,
    CATEGORY_HIGH_RETURN_RATE,
    CATEGORY_HIGH_REWORK,
    CATEGORY_MANUAL_INTERVENTION,
    CATEGORY_SLA_BREACH,
    CATEGORY_SLOW_NODE,
    ThresholdConfig,
    detect_candidates,
)


def _metrics(**overrides):
    base = {
        "overview": {
            "case_count": 100,
            "manual_intervention_case_count": 0,
            "manual_intervention_case_ratio": 0.0,
        },
        "node_metrics": {
            "draft": {"avg_dwell_hours": 0.2, "return_rate": 0.0, "avg_visits_per_case": 1.0},
            "dept_supervisor": {"avg_dwell_hours": 5.0, "return_rate": 0.05, "sla_achievement_rate": 1.0, "avg_visits_per_case": 1.0},
            "dept_gm": {"avg_dwell_hours": 10.0, "return_rate": 0.05, "sla_achievement_rate": 0.95, "avg_visits_per_case": 1.0},
            "line_leader": {"avg_dwell_hours": 12.0, "return_rate": 0.05, "sla_achievement_rate": 0.9, "avg_visits_per_case": 1.0},
        },
        "path_metrics": {
            "rework_rate": 0.05,
            "rework_case_count": 5,
            "conformance_violation_count": 0,
        },
        "manual_intervention": {"by_prior_node": {}},
        "node_names": {
            "draft": "起草",
            "dept_supervisor": "部门主管审批",
            "dept_gm": "部门总经理审批",
            "line_leader": "条线分管领导审批",
        },
    }
    for key, value in overrides.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            base[key] = {**base[key], **value}
        else:
            base[key] = value
    return base


def test_healthy_metrics_yield_no_candidates() -> None:
    assert detect_candidates(_metrics()) == []


def test_slow_node_detected_by_median_multiple_and_absolute_floor() -> None:
    metrics = _metrics(node_metrics={"line_leader": {"avg_dwell_hours": 120.0, "return_rate": 0.05, "sla_achievement_rate": 0.9, "avg_visits_per_case": 1.0}})
    candidates = detect_candidates(metrics)
    slow = [c for c in candidates if c.category == CATEGORY_SLOW_NODE]
    assert len(slow) == 1
    assert slow[0].node_id == "line_leader"
    assert slow[0].metric_reference["avg_dwell_hours"] == 120.0


def test_slow_node_not_flagged_when_below_absolute_floor_even_if_relatively_high() -> None:
    # 所有环节都很快：某个环节是中位数的很多倍，但绝对值仍低于 24h 下限 → 不算慢
    metrics = _metrics(node_metrics={
        "draft": {"avg_dwell_hours": 0.1, "return_rate": 0.0, "avg_visits_per_case": 1.0},
        "a": {"avg_dwell_hours": 0.5, "return_rate": 0.0, "avg_visits_per_case": 1.0},
        "b": {"avg_dwell_hours": 8.0, "return_rate": 0.0, "avg_visits_per_case": 1.0},
    })
    assert [c for c in detect_candidates(metrics) if c.category == CATEGORY_SLOW_NODE] == []


def test_high_return_rate_detected_with_severity() -> None:
    metrics = _metrics(node_metrics={"dept_supervisor": {"avg_dwell_hours": 5.0, "return_rate": 0.4, "sla_achievement_rate": 1.0, "avg_visits_per_case": 1.6}})
    high = [c for c in detect_candidates(metrics) if c.category == CATEGORY_HIGH_RETURN_RATE]
    assert len(high) == 1
    assert high[0].severity == "high"  # 0.4 >= 0.35


def test_sla_breach_detected_below_target() -> None:
    metrics = _metrics(node_metrics={"dept_gm": {"avg_dwell_hours": 10.0, "return_rate": 0.05, "sla_achievement_rate": 0.6, "avg_visits_per_case": 1.0}})
    breach = [c for c in detect_candidates(metrics) if c.category == CATEGORY_SLA_BREACH]
    assert len(breach) == 1
    assert breach[0].node_id == "dept_gm"
    assert breach[0].severity == "medium"  # 0.6 >= 0.5 高严重度线


def test_sla_breach_none_rate_is_ignored() -> None:
    # draft 没有 sla_achievement_rate（未配置 SLA），不应误报
    metrics = _metrics()
    assert [c for c in detect_candidates(metrics) if c.node_id == "draft" and c.category == CATEGORY_SLA_BREACH] == []


def test_conformance_violation_detected_when_count_positive() -> None:
    metrics = _metrics(path_metrics={"conformance_violation_count": 9})
    conf = [c for c in detect_candidates(metrics) if c.category == CATEGORY_CONFORMANCE_VIOLATION]
    assert len(conf) == 1
    assert conf[0].severity == "high"
    assert conf[0].metric_reference["conformance_violation_count"] == 9


def test_manual_intervention_detected_above_threshold() -> None:
    metrics = _metrics(overview={"manual_intervention_case_ratio": 0.1, "manual_intervention_case_count": 10, "case_count": 100})
    manual = [c for c in detect_candidates(metrics) if c.category == CATEGORY_MANUAL_INTERVENTION]
    assert len(manual) == 1


def test_high_rework_detected_above_threshold() -> None:
    metrics = _metrics(path_metrics={"rework_rate": 0.42, "rework_case_count": 252, "conformance_violation_count": 0})
    rework = [c for c in detect_candidates(metrics) if c.category == CATEGORY_HIGH_REWORK]
    assert len(rework) == 1


def test_candidates_sorted_high_severity_first() -> None:
    metrics = _metrics(
        node_metrics={
            "line_leader": {"avg_dwell_hours": 120.0, "return_rate": 0.4, "sla_achievement_rate": 0.3, "avg_visits_per_case": 1.6},
        },
        path_metrics={"rework_rate": 0.42, "rework_case_count": 252, "conformance_violation_count": 5},
    )
    candidates = detect_candidates(metrics)
    severities = [c.severity for c in candidates]
    assert severities == sorted(severities, key=lambda s: {"high": 0, "medium": 1}[s])
    assert candidates[0].severity == "high"


def test_threshold_config_is_respected() -> None:
    metrics = _metrics(node_metrics={"dept_supervisor": {"avg_dwell_hours": 5.0, "return_rate": 0.25, "sla_achievement_rate": 1.0, "avg_visits_per_case": 1.0}})
    # 默认阈值 0.2 会命中；把阈值调到 0.3 就不该命中
    assert [c for c in detect_candidates(metrics) if c.category == CATEGORY_HIGH_RETURN_RATE]
    strict = ThresholdConfig(high_return_rate=0.3)
    assert [c for c in detect_candidates(metrics, strict) if c.category == CATEGORY_HIGH_RETURN_RATE] == []
