"""检索命中率评测（模块3 · Phase 5）。

一组标注 query → 期望应召回的规则 id；跑向量检索，量期望规则有没有进 top-k
（recall@k）。检索用真实索引（Titan 嵌入），所以由脚本产出 scorecard；本模块的
evaluate 逻辑可注入假索引单测。
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

# 标注集：自然语言问法 → 应命中的规则 id（锚案例 + 广度域）
LABELED_QUERIES: list[tuple[str, str]] = [
    ("病假请假需要上传什么材料", "leave.sick_leave_certificate"),
    ("请假超过七天要经过哪一级领导审批", "leave.line_leader_for_long_or_special"),
    ("请假类型开始结束日期天数事由必填吗", "leave.core_fields_required_at_draft"),
    ("费用报销五万以上要不要财务负责人审批", "auth.finance_head_for_large_expense"),
    ("采购五十万以上要不要采购委员会会签", "auth.procurement_committee_for_large"),
    ("公司公章合同章用印要不要合规法律部审核", "auth.legal_for_high_risk_seal"),
    ("子公司重大事项要不要公司领导批示", "eoa140.company_leader_instruction"),
    ("对口部门会签要不要多人并行处理", "eoa140.countersign_parallel"),
    ("流程必须有起草环节和通向结束的路径吗", "design.has_draft_and_end"),
    ("申请人所属部门这种自动带出字段要设成只读吗", "design.autofill_fields_readonly"),
]


class RetrievalCaseResult(BaseModel):
    query: str
    expected_rule: str
    retrieved_rules: list[str]
    hit: bool
    rank: int | None = Field(default=None, description="期望规则在返回里的名次（1起），未命中为 None")


class RetrievalScorecard(BaseModel):
    case_count: int
    k: int
    hit_count: int
    recall_at_k: float | None
    results: list[RetrievalCaseResult] = Field(default_factory=list)


def evaluate_retrieval(index: Any, *, k: int = 5, queries: list[tuple[str, str]] | None = None) -> RetrievalScorecard:
    queries = queries or LABELED_QUERIES
    results: list[RetrievalCaseResult] = []
    hits = 0
    for query, expected in queries:
        found = index.semantic_search(query, k=k, kind="rule")
        rule_ids = [h.metadata.get("rule_id") for h in found]
        hit = expected in rule_ids
        rank = rule_ids.index(expected) + 1 if hit else None
        if hit:
            hits += 1
        results.append(RetrievalCaseResult(query=query, expected_rule=expected, retrieved_rules=rule_ids, hit=hit, rank=rank))
    return RetrievalScorecard(
        case_count=len(queries), k=k, hit_count=hits,
        recall_at_k=round(hits / len(queries), 4) if queries else None,
        results=results,
    )


def render_retrieval_scorecard_markdown(card: RetrievalScorecard) -> str:
    lines = [
        f"# 检索命中率评测（recall@{card.k}）",
        "",
        f"- **recall@{card.k}**：{_pct(card.recall_at_k)}（{card.hit_count}/{card.case_count}）",
        "",
        "| query | 期望规则 | 命中 | 名次 |",
        "|---|---|---|---|",
    ]
    for r in card.results:
        lines.append(f"| {r.query} | {r.expected_rule} | {'✓' if r.hit else '✗'} | {r.rank if r.rank else '—'} |")
    return "\n".join(lines)


def _pct(value: float | None) -> str:
    return f"{value * 100:.1f}%" if value is not None else "—"
