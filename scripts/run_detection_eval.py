"""对请假流程 v1 跑一遍诊断 agent 的检出率评测（分析侧闭环 Phase 4）。

前置：先跑 gen_leave_process_event_log.py 生成事件日志 + 注入病灶清单（gold）。
用法：uv run python scripts/run_detection_eval.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.analytics.metrics import compute_metrics, load_case_records
from app.eval.detection_eval import evaluate_detection, render_scorecard_markdown
from app.generation.event_log_generator import load_process_definition
from data.schema import ScenarioConfig

ANALYTICS_DIR = Path("data/analytics/leave_request")


def main() -> None:
    scenario = ScenarioConfig.model_validate_json((ANALYTICS_DIR / "scenario_v1.json").read_text(encoding="utf-8"))
    gold = json.loads((ANALYTICS_DIR / "injected_defects_v1.json").read_text(encoding="utf-8"))
    process = load_process_definition(scenario.process_target_path)
    cases = load_case_records(ANALYTICS_DIR / "event_log_v1.jsonl")

    metrics = compute_metrics(cases, process=process, assumed_sla_days=scenario.assumed_sla_days)
    metrics["node_names"] = {node.node_id: node.node_name for node in process.flow_nodes}

    scorecard = evaluate_detection(metrics, gold["defect_catalog"], scenario_id=scenario.scenario_id)

    (ANALYTICS_DIR / "detection_scorecard_v1.json").write_text(
        scorecard.model_dump_json(indent=2), encoding="utf-8"
    )
    markdown = render_scorecard_markdown(scorecard)
    (ANALYTICS_DIR / "detection_scorecard_v1.md").write_text(markdown + "\n", encoding="utf-8")
    print(markdown)
    print(f"\n输出：{ANALYTICS_DIR / 'detection_scorecard_v1.json'} / .md")


if __name__ == "__main__":
    main()
