from __future__ import annotations

from app.eval.compliance_eval import (
    ComplianceEvalCase,
    build_leave_eval_cases,
    evaluate_compliance,
    render_compliance_scorecard_markdown,
)
from app.eval.retrieval_eval import evaluate_retrieval, render_retrieval_scorecard_markdown


def test_compliance_eval_full_recall_no_false_positive_on_injected_cases() -> None:
    """确定性合规评测（不打 Bedrock）：每个注入违规都被检出、合规基线不误报。"""
    card = evaluate_compliance(build_leave_eval_cases())
    assert card.recall == 1.0
    assert card.false_positive_count == 0
    baseline = next(r for r in card.results if r.name == "合规基线")
    assert baseline.detected_rules == []  # 合规基线 0 违规


def test_compliance_eval_counts_miss_and_false_positive() -> None:
    from app.eval.compliance_eval import _compliant_leave

    base = _compliant_leave()
    cases = [
        # 期望触发一条不可能触发的规则 → 漏检
        ComplianceEvalCase(name="不可能命中", process=base, expected_rules=["eoa140.company_leader_instruction"]),
    ]
    card = evaluate_compliance(cases)
    assert card.recall == 0.0
    result = card.results[0]
    assert "eoa140.company_leader_instruction" in result.missed


def test_compliance_scorecard_markdown_has_headline() -> None:
    card = evaluate_compliance(build_leave_eval_cases())
    md = render_compliance_scorecard_markdown(card)
    assert "违规检出率" in md and "100.0%" in md


class _FakeHit:
    def __init__(self, rule_id):
        self.metadata = {"rule_id": rule_id, "kind": "rule"}
        self.text = rule_id
        self.kind = "rule"


class _FakeIndex:
    """按 query 返回预设 rule 命中，测检索评测逻辑（不打 Bedrock）。"""

    def __init__(self, by_query):
        self.by_query = by_query

    def semantic_search(self, query, *, k=5, kind=None, **kwargs):
        return [_FakeHit(rid) for rid in self.by_query.get(query, [])][:k]


def test_retrieval_eval_recall_hit_and_rank() -> None:
    queries = [("Q1", "rule.a"), ("Q2", "rule.b")]
    index = _FakeIndex({
        "Q1": ["rule.x", "rule.a", "rule.y"],  # 命中，名次2
        "Q2": ["rule.z"],                        # 未命中
    })
    card = evaluate_retrieval(index, k=5, queries=queries)
    assert card.hit_count == 1
    assert card.recall_at_k == 0.5
    r1 = next(r for r in card.results if r.query == "Q1")
    assert r1.hit is True and r1.rank == 2
    r2 = next(r for r in card.results if r.query == "Q2")
    assert r2.hit is False and r2.rank is None


def test_retrieval_eval_respects_k_cutoff() -> None:
    queries = [("Q", "rule.deep")]
    # 期望规则排在第6，k=5 时应算未命中
    index = _FakeIndex({"Q": ["r1", "r2", "r3", "r4", "r5", "rule.deep"]})
    card = evaluate_retrieval(index, k=5, queries=queries)
    assert card.hit_count == 0


def test_retrieval_scorecard_markdown() -> None:
    index = _FakeIndex({"Q1": ["rule.a"]})
    card = evaluate_retrieval(index, k=5, queries=[("Q1", "rule.a")])
    md = render_retrieval_scorecard_markdown(card)
    assert "recall@5" in md and "rule.a" in md
