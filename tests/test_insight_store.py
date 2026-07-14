from __future__ import annotations

from app.insights import InsightStore, channel_for
from data.schema import InsightChannel, InsightKind, InsightSource, InsightStatus


def _store() -> InsightStore:
    return InsightStore()


def test_channel_mapping_is_fixed_and_deterministic() -> None:
    assert channel_for(InsightKind.COVERAGE_GAP) == InsightChannel.DESIGN
    assert channel_for(InsightKind.ORG_GAP) == InsightChannel.ORG
    assert channel_for(InsightKind.SLOW_NODE) == InsightChannel.TRIAGE  # 二义→triage


def test_record_creates_new_insight() -> None:
    s = _store()
    ins = s.record(workflow_definition_id="LEAVE-001", node_id="gm", kind=InsightKind.COVERAGE_GAP,
                   severity="high", source=InsightSource.OPS, at="2026-07-01T09:00:00")
    assert ins.occurrences == 1
    assert ins.channel == InsightChannel.DESIGN
    assert ins.status == InsightStatus.OPEN
    assert ins.first_seen == ins.last_seen == "2026-07-01T09:00:00"


def test_record_same_key_aggregates_not_duplicates() -> None:
    """同 (流程,环节,类型) 反复写 → 一条 occurrences=N，不是 N 条。"""
    s = _store()
    for day in range(1, 5):  # 4 单同因卡住
        s.record(workflow_definition_id="LEAVE-001", node_id="gm", kind=InsightKind.COVERAGE_GAP,
                 severity="high", source=InsightSource.OPS, at=f"2026-07-0{day}T09:00:00")
    opened = s.open_for("LEAVE-001")
    assert len(opened) == 1
    assert opened[0].occurrences == 4
    assert opened[0].last_seen == "2026-07-04T09:00:00"  # 刷新到最新


def test_different_keys_stay_separate() -> None:
    s = _store()
    s.record(workflow_definition_id="LEAVE-001", node_id="gm", kind=InsightKind.COVERAGE_GAP,
             severity="high", source=InsightSource.OPS)
    s.record(workflow_definition_id="LEAVE-001", node_id="gm", kind=InsightKind.SLOW_NODE,
             severity="medium", source=InsightSource.ANALYTICS)  # 同环节不同类型
    assert len(s.open_for("LEAVE-001")) == 2


def test_open_for_filters_by_workflow_and_status() -> None:
    s = _store()
    a = s.record(workflow_definition_id="LEAVE-001", node_id="gm", kind=InsightKind.COVERAGE_GAP,
                 severity="high", source=InsightSource.OPS)
    s.record(workflow_definition_id="EXPENSE-001", node_id="fin", kind=InsightKind.SLOW_NODE,
             severity="high", source=InsightSource.ANALYTICS)
    assert {i.insight_id for i in s.open_for("LEAVE-001")} == {a.insight_id}
    s.resolve(a.insight_id)
    assert s.open_for("LEAVE-001") == []  # resolved 不在 open_for


def test_resolved_then_recur_opens_new_insight() -> None:
    """修复后消解；同问题再复发 → 新开一条（不是复活旧的）。"""
    s = _store()
    first = s.record(workflow_definition_id="LEAVE-001", node_id="gm", kind=InsightKind.COVERAGE_GAP,
                     severity="high", source=InsightSource.OPS)
    s.resolve(first.insight_id)
    second = s.record(workflow_definition_id="LEAVE-001", node_id="gm", kind=InsightKind.COVERAGE_GAP,
                      severity="high", source=InsightSource.OPS)
    assert second.insight_id != first.insight_id
    assert second.occurrences == 1
    assert len(s.open_for("LEAVE-001")) == 1  # 只有新的那条 open


def test_channel_counts_for_manage_card() -> None:
    """流程管理卡片「运行反馈」分渠道计数。"""
    s = _store()
    s.record(workflow_definition_id="LEAVE-001", node_id="gm", kind=InsightKind.COVERAGE_GAP,
             severity="high", source=InsightSource.OPS)          # design
    s.record(workflow_definition_id="LEAVE-001", node_id="ll", kind=InsightKind.HIGH_RETURN,
             severity="medium", source=InsightSource.ANALYTICS)  # design
    s.record(workflow_definition_id="LEAVE-001", node_id="ll", kind=InsightKind.ORG_GAP,
             severity="high", source=InsightSource.OPS)          # org
    s.record(workflow_definition_id="LEAVE-001", node_id="sm", kind=InsightKind.SLOW_NODE,
             severity="medium", source=InsightSource.ANALYTICS)  # triage
    counts = s.channel_counts("LEAVE-001")
    assert counts == {"design": 2, "org": 1, "triage": 1}


def test_acknowledge_keeps_in_open_for() -> None:
    s = _store()
    a = s.record(workflow_definition_id="LEAVE-001", node_id="gm", kind=InsightKind.COVERAGE_GAP,
                 severity="high", source=InsightSource.OPS)
    s.acknowledge(a.insight_id)
    assert a.status == InsightStatus.ACKNOWLEDGED
    assert len(s.open_for("LEAVE-001")) == 1  # acknowledged 仍在 open_for（未消解）
