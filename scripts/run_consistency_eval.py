"""跑一遍横切一致性 / 护栏断言评测（评测方案 · 第 5 类）。

不需要 gold、不需要业务数据、不打 Bedrock——夹具走真实确定性管线构造，
断言"输出内部自不自洽"。可直接进 CI。

用法：uv run python scripts/run_consistency_eval.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.eval.consistency_eval import build_ops_consistency_cases, evaluate_consistency

OUT_DIR = Path("data/eval/consistency")


def render_markdown(scorecard) -> str:  # noqa: ANN001
    lines = [
        "# 横切一致性 / 护栏断言评测",
        "",
        f"- case 数：{scorecard.case_count}",
        f"- 断言检出率（正控）：{scorecard.recall if scorecard.recall is not None else '—'}",
        f"- 误报数：{scorecard.false_positive_count}",
        f"- 干净基线数（期望自洽且实际自洽）：{scorecard.clean_case_count}",
        "",
        "| case | 期望断言 | 实际检出 | 漏检 | 误报 |",
        "|---|---|---|---|---|",
    ]
    for r in scorecard.results:
        lines.append(
            f"| {r.case} | {'、'.join(r.expected_kinds) or '—'} | {'、'.join(r.detected_kinds) or '—'} "
            f"| {'、'.join(r.missed) or '—'} | {'、'.join(r.unexpected) or '—'} |"
        )
    detail = [v for r in scorecard.results for v in r.violations]
    if detail:
        lines += ["", "## 命中的断言明细", ""]
        for v in detail:
            lines.append(f"- **[{v.kind}]** `{v.case}` @{v.field}：{v.detail}")
            if v.evidence:
                lines.append(f"  - 证据：`{v.evidence}`")
    return "\n".join(lines)


def main() -> None:
    scorecard = evaluate_consistency(build_ops_consistency_cases())
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    (OUT_DIR / "consistency_scorecard.json").write_text(scorecard.model_dump_json(indent=2), encoding="utf-8")
    markdown = render_markdown(scorecard)
    (OUT_DIR / "consistency_scorecard.md").write_text(markdown + "\n", encoding="utf-8")
    print(markdown)
    print(f"\n输出：{OUT_DIR / 'consistency_scorecard.json'} / .md")


if __name__ == "__main__":
    main()
