"""一键重跑三个案例 + 评测 + 跨案例汇总表。

为每个案例跑 LangGraph pipeline（固定 run 目录），再用 batch_eval 对各自 gold
打分，产出跨案例三维汇总表，并导出到 doc/eval_summary.md（runs/ 已 gitignore）。

用法：uv run python scripts/run_eval_summary.py
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.eval.batch_eval import run_batch_eval
from app.tools.vision_ocr import build_bedrock_image_transcriber
from app.workflows.process_v1 import run_process_case

CASES = [
    ("data/cases/EOA140_subsidiary_major_matter", "runs/EOA140"),
    ("data/cases/leave_request", "runs/leave_request"),
    ("data/cases/expense_reimbursement", "runs/expense_reimbursement"),
]
CONFIG = "data/eval_cases.json"
SUMMARY_OUT = Path("runs/eval_summary")
EXPORT_MD = Path("doc/eval_summary.md")


def main() -> None:
    transcriber = build_bedrock_image_transcriber()
    for case_dir, run_dir in CASES:
        out = Path(run_dir)
        if out.exists():
            shutil.rmtree(out)
        print(f"[run] {case_dir} -> {run_dir}")
        run_process_case(case_dir, out, image_transcriber=transcriber)

    print(f"[eval] batch -> {SUMMARY_OUT}")
    summary = run_batch_eval(CONFIG, SUMMARY_OUT)

    EXPORT_MD.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(SUMMARY_OUT / "eval_summary.md", EXPORT_MD)
    print(f"[export] {EXPORT_MD}")
    for row in summary["cases"]:
        print(
            f"  {row['case_id']:<34} total={row['auto_total']:<6} "
            f"det={row['process_definition_score']} clar={row['clarification_score']} ev={row['evidence_guardrail_score']}"
        )


if __name__ == "__main__":
    main()
