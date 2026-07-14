from __future__ import annotations

from app.eval.detection_eval import evaluate_detection, render_scorecard_markdown


def _metrics(node_metrics=None, path_metrics=None, overview=None):
    return {
        "overview": {"case_count": 600, "manual_intervention_case_ratio": 0.0, "manual_intervention_case_count": 0, **(overview or {})},
        "node_metrics": node_metrics or {},
        "path_metrics": {"rework_rate": 0.05, "rework_case_count": 30, "conformance_violation_count": 0, **(path_metrics or {})},
        "manual_intervention": {"by_prior_node": {}},
        "node_names": {"dept_supervisor": "部门主管审批", "dept_gm": "部门总经理审批", "line_leader": "条线分管领导审批"},
    }


def _full_scenario_metrics():
    # 复刻请假 v1 的检出画面：慢环节+SLA 低(line_leader)、高退回(dept_supervisor)、
    # SLA 违约(dept_gm)、违规跳级、人工介入、高返工
    return _metrics(
        node_metrics={
            "draft": {"avg_dwell_hours": 0.2, "return_rate": 0.0, "avg_visits_per_case": 1.0},
            "dept_supervisor": {"avg_dwell_hours": 5.0, "return_rate": 0.4, "sla_achievement_rate": 1.0, "avg_visits_per_case": 1.6},
            "dept_gm": {"avg_dwell_hours": 20.0, "return_rate": 0.05, "sla_achievement_rate": 0.6, "avg_visits_per_case": 1.1},
            "line_leader": {"avg_dwell_hours": 120.0, "return_rate": 0.05, "sla_achievement_rate": 0.27, "avg_visits_per_case": 1.0},
        },
        path_metrics={"rework_rate": 0.42, "rework_case_count": 252, "conformance_violation_count": 7},
        overview={"manual_intervention_case_ratio": 0.1, "manual_intervention_case_count": 60},
    )


_FULL_GOLD = [
    {"defect_id": "slow_line_leader", "type": "慢环节", "target_node_id": "line_leader"},
    {"defect_id": "high_return_dept_supervisor", "type": "高退回率", "target_node_id": "dept_supervisor"},
    {"defect_id": "sla_breach_dept_gm", "type": "超时违约", "target_node_id": "dept_gm"},
    {"defect_id": "illegal_skip_line_leader", "type": "违规跳级", "target_node_id": "dept_gm"},
    {"defect_id": "manual_intervention_case", "type": "线下人工介入", "target_node_id": None},
    {"defect_id": "zombie_case", "type": "僵尸实例", "target_node_id": None},
]


def test_full_scenario_detects_all_detectable_defects() -> None:
    card = evaluate_detection(_full_scenario_metrics(), _FULL_GOLD, scenario_id="leave_v1")
    assert card.total_injected == 6
    assert card.detectable_injected == 5
    assert card.detected_injected == 5
    assert card.recall_over_detectable == 1.0
    assert card.recall_over_all == round(5 / 6, 4)
    assert card.spurious_false_positive_count == 0


def test_zombie_is_marked_undetectable_and_missed() -> None:
    card = evaluate_detection(_full_scenario_metrics(), _FULL_GOLD)
    zombie = next(m for m in card.defect_matches if m.defect_id == "zombie_case")
    assert zombie.detectable is False
    assert zombie.detected is False
    assert zombie.expected_category is None


def test_secondary_consequences_are_not_false_positives() -> None:
    card = evaluate_detection(_full_scenario_metrics(), _FULL_GOLD)
    kinds = {e.bottleneck_id: e.classification for e in card.extra_detections}
    # line_leader 既是慢环节又 SLA 低 → sla_breach:line_leader 是二级连带
    assert kinds.get("sla_breach:line_leader") == "secondary_consequence"
    # high_return 触发 → high_rework 是二级连带
    assert kinds.get("high_rework:process") == "secondary_consequence"
    assert all(v == "secondary_consequence" for v in kinds.values())


def test_missed_defect_lowers_recall() -> None:
    # line_leader 变快 → 慢环节病灶漏检
    metrics = _full_scenario_metrics()
    metrics["node_metrics"]["line_leader"]["avg_dwell_hours"] = 6.0
    metrics["node_metrics"]["line_leader"]["sla_achievement_rate"] = 0.95
    card = evaluate_detection(metrics, _FULL_GOLD)
    slow = next(m for m in card.defect_matches if m.defect_id == "slow_line_leader")
    assert slow.detected is False
    assert card.detected_injected == 4
    assert card.recall_over_detectable == round(4 / 5, 4)


def test_node_specific_defect_requires_matching_node() -> None:
    # 注入的慢环节在 dept_gm，但指标里慢的是 line_leader → 不该匹配上
    gold = [{"defect_id": "slow_dept_gm", "type": "慢环节", "target_node_id": "dept_gm"}]
    card = evaluate_detection(_full_scenario_metrics(), gold)
    assert card.defect_matches[0].detected is False


def test_spurious_false_positive_is_counted() -> None:
    # 构造一个既非注入病灶、又非二级连带的检出：某环节 SLA 低但该环节不慢
    metrics = _metrics(
        node_metrics={
            "dept_gm": {"avg_dwell_hours": 10.0, "return_rate": 0.05, "sla_achievement_rate": 0.5, "avg_visits_per_case": 1.0},
        },
    )
    gold = []  # 没有注入任何病灶
    card = evaluate_detection(metrics, gold)
    spurious = [e for e in card.extra_detections if e.classification == "spurious"]
    assert any(e.bottleneck_id == "sla_breach:dept_gm" for e in spurious)
    assert card.spurious_false_positive_count >= 1


def test_render_markdown_contains_headline_numbers() -> None:
    card = evaluate_detection(_full_scenario_metrics(), _FULL_GOLD, scenario_id="leave_v1")
    md = render_scorecard_markdown(card)
    assert "可检病灶检出率" in md
    assert "100.0%（5/5）" in md
    assert "zombie_case" in md
    assert "二级连带" in md
