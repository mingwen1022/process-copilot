"""模块3 规则RAG 评测：规则合规率 + 检索命中率（Phase 5）。

合规率是确定性的（不打 Bedrock）；检索命中率用真实向量索引（Titan 嵌入）。
前置：先跑 scripts/build_knowledge_index.py 建索引。
用法：uv run python scripts/run_rule_rag_eval.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.eval.compliance_eval import build_leave_eval_cases, evaluate_compliance, render_compliance_scorecard_markdown
from app.eval.retrieval_eval import evaluate_retrieval, render_retrieval_scorecard_markdown

OUT_DIR = Path("data/rag_eval")


def main() -> None:
    comp_card = evaluate_compliance(build_leave_eval_cases())
    comp_md = render_compliance_scorecard_markdown(comp_card)
    (OUT_DIR / "compliance_scorecard.md").write_text(comp_md + "\n", encoding="utf-8")
    print(comp_md)
    print()

    from app.rag.index import KnowledgeIndex

    index = KnowledgeIndex()
    retr_card = evaluate_retrieval(index)
    retr_md = render_retrieval_scorecard_markdown(retr_card)
    (OUT_DIR / "retrieval_scorecard.md").write_text(retr_md + "\n", encoding="utf-8")
    print(retr_md)
    print(f"\n输出：{OUT_DIR}/compliance_scorecard.md, retrieval_scorecard.md")


if __name__ == "__main__":
    main()
