from __future__ import annotations

from app.insights import InsightStore
from app.insights.producers import OpsInsightProducer
from app.copilots.ops_service import OpsCopilotService
from data.schema import InsightChannel, InsightKind, InsightSource


class _StubAgent:
    def decide(self, *a, **k):  # noqa: ANN001, ANN002, ANN003
        return None


# ——— 闸门1：kind gate（只放行设计相关根因）———

def test_producer_records_design_gap_as_coverage_gap() -> None:
    s = InsightStore()
    ins = OpsInsightProducer.record_from_diagnosis(
        s, workflow_definition_id="LEAVE-001", node_id="gm",
        stuck_kind="design_gap", blocked_paths=[{"path_name": "送总经理", "reason": "无匹配"}],
    )
    assert ins is not None
    assert ins.kind == InsightKind.COVERAGE_GAP and ins.channel == InsightChannel.DESIGN
    assert ins.severity == "high"


def test_producer_skips_data_error() -> None:
    """用户填错(data_error)不反哺——那是用户的事，不是设计缺陷。"""
    s = InsightStore()
    assert OpsInsightProducer.record_from_diagnosis(
        s, workflow_definition_id="LEAVE-001", node_id="gm", stuck_kind="data_error") is None
    assert s.open_for("LEAVE-001") == []


def test_producer_maps_org_gap() -> None:
    s = InsightStore()
    ins = OpsInsightProducer.record_from_diagnosis(
        s, workflow_definition_id="LEAVE-001", node_id="ll", stuck_kind="org_gap")
    assert ins.kind == InsightKind.ORG_GAP and ins.channel == InsightChannel.ORG


# ——— ops_service 挂钩：终局结算写入 + 聚合 ———

def _svc() -> OpsCopilotService:
    return OpsCopilotService(agent=_StubAgent(), insight_store=InsightStore())


def test_record_incident_writes_coverage_gap_for_stuck_design_gap() -> None:
    svc = _svc()
    ins = svc.record_incident("inst_gap")  # 事假5天卡总经理·design_gap
    assert ins is not None and ins.kind == InsightKind.COVERAGE_GAP
    assert ins.workflow_definition_id == "LEAVE-001"  # 用 process_id 作流程键
    got = svc.insights_for("LEAVE-001")
    assert len(got) == 1 and got[0].node_id == ins.node_id


def test_record_incident_ignores_data_error_instance() -> None:
    svc = _svc()
    assert svc.record_incident("inst_data") is None  # data_error → 不反哺
    assert svc.insights_for("LEAVE-001") == []


def test_seed_history_aggregates_to_occurrences() -> None:
    """模拟近30天4单同因卡住 → 聚合成 1 条 occurrences=4（不是4条）。"""
    svc = _svc()
    svc.seed_reback_history("inst_gap", times=4)
    got = svc.insights_for("LEAVE-001")
    assert len(got) == 1
    assert got[0].occurrences == 4
    assert svc.insight_channel_counts("LEAVE-001") == {"design": 1}


def test_seed_org_gap_records_org_channel_insight() -> None:
    """运维·岗位空缺（inst_org 选不到审批人）→ org_gap 洞察，走「组织」渠道，不是设计渠道
    ——它是给岗位配人的组织问题，不是改流程设计能解的，跟 coverage_gap 区分开。"""
    svc = _svc()
    svc.seed_reback_history("inst_org", times=3)
    got = svc.insights_for("LEAVE-001")
    org = next(i for i in got if i.kind == InsightKind.ORG_GAP)
    assert org.channel == InsightChannel.ORG
    assert org.occurrences == 3
    assert org.source == InsightSource.OPS

    # 覆盖漏洞 + 岗位空缺可以并存（不同 kind → 独立两条）
    svc.seed_reback_history("inst_gap", times=4)
    kinds = {i.kind for i in svc.insights_for("LEAVE-001")}
    assert kinds == {InsightKind.ORG_GAP, InsightKind.COVERAGE_GAP}


def test_no_store_is_noop() -> None:
    """未注入 store 时静默不记，不报错。"""
    svc = OpsCopilotService(agent=_StubAgent())
    assert svc.record_incident("inst_gap") is None
    assert svc.insights_for("LEAVE-001") == []


# ——— 分析臂 producer（与运维臂对称）———

def test_analytics_producer_maps_candidates_to_insights() -> None:
    from app.analytics.thresholds import CandidateBottleneck
    from app.insights.producers import AnalyticsInsightProducer

    s = InsightStore()
    cands = [
        CandidateBottleneck(bottleneck_id="slow_node:gm", category="slow_node", node_id="gm",
                            severity="high", description="gm 慢", metric_reference={"avg_dwell_hours": 76.8}),
        CandidateBottleneck(bottleneck_id="high_return_rate:ll", category="high_return_rate", node_id="ll",
                            severity="medium", description="ll 退回高", metric_reference={"return_rate": 0.4}),
        CandidateBottleneck(bottleneck_id="conformance_violation:process", category="conformance_violation",
                            node_id=None, severity="high", description="合规违规"),  # 流程级，无 node_id 也要记
    ]
    rec = AnalyticsInsightProducer.record_from_candidates(s, workflow_definition_id="LEAVE-001", candidates=cands)
    assert len(rec) == 3
    opened = s.open_for("LEAVE-001")
    assert {i.kind for i in opened} == {InsightKind.SLOW_NODE, InsightKind.HIGH_RETURN, InsightKind.CONFORMANCE_VIOLATION}
    slow = next(i for i in opened if i.kind == InsightKind.SLOW_NODE)
    assert slow.channel == InsightChannel.TRIAGE  # slow 二义→triage
    assert slow.evidence == {"avg_dwell_hours": 76.8}
    assert slow.headline == "gm 慢"  # 回归：headline 之前一直是空字符串，设计工作台的提醒栏显示不出内容
    conformance = next(i for i in opened if i.kind == InsightKind.CONFORMANCE_VIOLATION)
    assert conformance.node_id is None  # 流程级洞察，不挂在任何一个环节上
    assert conformance.channel == InsightChannel.DESIGN  # 路由条件跟实际流转对不上，根因明确是设计


def test_supported_analytics_categories_matches_real_category_map() -> None:
    """回归：_CATEGORY_MAP 曾经挂着一个 detect_candidates() 从不会产出的幽灵类别
    "dead_node"，且真实会产出的六类都在——不能悄悄漂移。"""
    from app.insights.producers import SUPPORTED_ANALYTICS_CATEGORIES

    assert SUPPORTED_ANALYTICS_CATEGORIES == {
        "slow_node", "high_return_rate", "sla_breach",
        "conformance_violation", "high_rework", "manual_intervention",
    }


def test_unsupported_category_candidate_is_skipped_not_recorded() -> None:
    from app.analytics.thresholds import CandidateBottleneck
    from app.insights.producers import AnalyticsInsightProducer

    s = InsightStore()
    cand = CandidateBottleneck(bottleneck_id="dead_node:x", category="dead_node", node_id="x",
                                severity="medium", description="从不会真的产出的类别")
    assert AnalyticsInsightProducer.record_from_candidates(s, workflow_definition_id="LEAVE-001", candidates=[cand]) == []
    assert s.open_for("LEAVE-001") == []


def test_ops_and_analytics_coexist_on_same_node() -> None:
    """同环节 coverage_gap(运维) + slow_node(分析) 两条独立洞察，不互相覆盖。"""
    from app.analytics.thresholds import CandidateBottleneck
    from app.insights.producers import AnalyticsInsightProducer

    s = InsightStore()
    OpsInsightProducer.record_from_diagnosis(s, workflow_definition_id="LEAVE-001", node_id="gm",
                                             stuck_kind="design_gap")
    AnalyticsInsightProducer.record_from_candidates(
        s, workflow_definition_id="LEAVE-001",
        candidates=[CandidateBottleneck(bottleneck_id="slow_node:gm", category="slow_node",
                                        node_id="gm", severity="high", description="gm 慢")])
    kinds = {i.kind for i in s.open_for("LEAVE-001")}
    assert kinds == {InsightKind.COVERAGE_GAP, InsightKind.SLOW_NODE}  # 两条并存
