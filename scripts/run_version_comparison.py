"""跑一遍请假流程 v1 vs v2 效能对比（分析侧闭环 Phase 5 · headline）。

前置：先跑 gen_leave_process_event_log.py（v1）与 gen_leave_process_v2.py（v2）。
用法：uv run python scripts/run_version_comparison.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.analytics.metrics import compute_metrics, load_case_records
from app.analytics.version_comparison import compare_versions, render_comparison_markdown
from app.generation.event_log_generator import load_process_definition
from data.schema import ScenarioConfig

OUT_DIR = Path("data/analytics/leave_request")


def _metrics_for(scenario_file: str, event_log_file: str) -> dict:
    scenario = ScenarioConfig.model_validate_json((OUT_DIR / scenario_file).read_text(encoding="utf-8"))
    process = load_process_definition(scenario.process_target_path)
    cases = load_case_records(OUT_DIR / event_log_file)
    metrics = compute_metrics(cases, process=process, assumed_sla_days=scenario.assumed_sla_days)
    metrics["node_names"] = {node.node_id: node.node_name for node in process.flow_nodes}
    return metrics


def main() -> None:
    metrics_v1 = _metrics_for("scenario_v1.json", "event_log_v1.jsonl")
    metrics_v2 = _metrics_for("scenario_v2.json", "event_log_v2.jsonl")

    comparison = compare_versions(metrics_v1, metrics_v2)
    (OUT_DIR / "comparison_v1_v2.json").write_text(comparison.model_dump_json(indent=2), encoding="utf-8")
    markdown = render_comparison_markdown(comparison)
    (OUT_DIR / "comparison_v1_v2.md").write_text(markdown + "\n", encoding="utf-8")
    print(markdown)
    print(f"\n输出：{OUT_DIR / 'comparison_v1_v2.json'} / .md")


if __name__ == "__main__":
    main()
