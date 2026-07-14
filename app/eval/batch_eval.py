from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path
from typing import Any

from app.eval.process_target_eval import evaluate_run_against_target, write_evaluation_report
from app.io_utils import write_json


def run_batch_eval(config_path: str | Path, out_dir: str | Path) -> dict[str, Any]:
    config_file = Path(config_path)
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    config = json.loads(config_file.read_text(encoding="utf-8"))
    rows: list[dict[str, Any]] = []
    reports: list[dict[str, Any]] = []

    for item in config.get("cases", []):
        case_id = item["case_id"]
        case_out = out / case_id
        report = evaluate_run_against_target(
            item["run"],
            item["target"],
            allow_draft_target=item.get("allow_draft_target", False),
        )
        write_evaluation_report(report, case_out)
        reports.append(report)
        rows.append(_summary_row(case_id, report))

    summary = {
        "config": str(config_file),
        "case_count": len(rows),
        "average_auto_total": _average(row["auto_total"] for row in rows),
        "average_human_adjusted_total": _average(row["human_adjusted_total"] for row in rows),
        "cases": rows,
    }
    write_json(summary, out / "eval_summary.json")
    _write_summary_csv(rows, out / "eval_summary.csv")
    (out / "eval_summary.md").write_text(_render_summary_markdown(summary), encoding="utf-8")
    return summary


def _summary_row(case_id: str, report: dict[str, Any]) -> dict[str, Any]:
    comparison = report.get("comparison_report", {})
    top_missing = []
    for key in ["missing_form_fields", "missing_flow_nodes", "missing_submit_paths", "missing_attachments", "missing_roles"]:
        for value in comparison.get(key, [])[:3]:
            top_missing.append(f"{key}:{value}")
    return {
        "case_id": case_id,
        "review_status": report.get("review_metadata", {}).get("review_status", "draft"),
        "auto_total": report.get("auto_score", {}).get("total", report.get("score", {}).get("total", 0)),
        "human_adjusted_total": report.get("human_adjusted_score", {}).get("total", report.get("score", {}).get("total", 0)),
        "process_definition_score": report.get("auto_score", {}).get("deterministic_process_definition", 0),
        "clarification_score": report.get("auto_score", {}).get("clarification", 0),
        "evidence_guardrail_score": report.get("auto_score", {}).get("evidence_and_guardrail", 0),
        "hard_failures": report.get("hard_failures", []),
        "top_missing_items": top_missing[:8],
    }


def _write_summary_csv(rows: list[dict[str, Any]], path: Path) -> None:
    fieldnames = [
        "case_id",
        "review_status",
        "auto_total",
        "human_adjusted_total",
        "process_definition_score",
        "clarification_score",
        "evidence_guardrail_score",
        "hard_failures",
        "top_missing_items",
    ]
    with path.open("w", encoding="utf-8", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow({key: _csv_value(row.get(key)) for key in fieldnames})


def _render_summary_markdown(summary: dict[str, Any]) -> str:
    lines = [
        "# Eval Summary",
        "",
        f"- Case 数：{summary.get('case_count')}",
        f"- 平均 auto total：{summary.get('average_auto_total')}",
        f"- 平均 human adjusted total：{summary.get('average_human_adjusted_total')}",
        "",
        "| case_id | review_status | 确定性流程(70) | 待确认项(20) | 证据护栏(10) | 总分 | hard_failures |",
        "| --- | --- | ---: | ---: | ---: | ---: | --- |",
    ]
    for row in summary.get("cases", []):
        lines.append(
            "| {case_id} | {review_status} | {det} | {clar} | {ev} | {auto_total} | {hard_failures} |".format(
                case_id=row["case_id"],
                review_status=row["review_status"],
                det=row.get("process_definition_score", 0),
                clar=row.get("clarification_score", 0),
                ev=row.get("evidence_guardrail_score", 0),
                auto_total=row["auto_total"],
                hard_failures="<br>".join(row.get("hard_failures", [])) or "—",
            )
        )
    lines.append("")
    return "\n".join(lines)


def _average(values: Any) -> float:
    numbers = [float(value) for value in values]
    return round(sum(numbers) / len(numbers), 2) if numbers else 0.0


def _csv_value(value: Any) -> str:
    if isinstance(value, (list, dict)):
        return json.dumps(value, ensure_ascii=False)
    return "" if value is None else str(value)


def main() -> None:
    parser = argparse.ArgumentParser(description="Run evaluation for multiple target/run pairs.")
    parser.add_argument("--config", required=True, help="Eval cases JSON config.")
    parser.add_argument("--out", required=True, help="Output summary directory.")
    args = parser.parse_args()

    summary = run_batch_eval(args.config, args.out)
    print(json.dumps({"case_count": summary["case_count"], "out": str(Path(args.out))}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
