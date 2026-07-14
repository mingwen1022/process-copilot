"""对费用报销流程 v1 合成事件日志跑一遍确定性指标层（P3 报销全套 · 分析轴）。

前置：先跑 scripts/gen_expense_process_event_log.py 生成 event_log_v1.jsonl。
用法：uv run python scripts/run_expense_process_metrics.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.analytics.metrics import compute_metrics, load_case_records
from app.generation.event_log_generator import load_process_definition
from data.schema import ScenarioConfig

OUT_DIR = Path("data/analytics/expense_reimbursement")


def main() -> None:
    scenario = ScenarioConfig.model_validate_json((OUT_DIR / "scenario_v1.json").read_text(encoding="utf-8"))
    process = load_process_definition(scenario.process_target_path)
    cases = load_case_records(OUT_DIR / "event_log_v1.jsonl")

    metrics = compute_metrics(cases, process=process, assumed_sla_days=scenario.assumed_sla_days)
    (OUT_DIR / "metrics_v1.json").write_text(
        json.dumps(metrics, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    overview = metrics["overview"]
    print(f"案例数: {overview['case_count']}  状态分布: {overview['case_status_counts']}")
    print(f"平均办结时长(小时): {overview['avg_case_duration_hours']}")
    print(f"平均流转环节数: {overview['avg_node_visits']}")
    print(f"人工介入比例: {overview['manual_intervention_case_ratio']}")
    print()
    print("各环节指标：")
    for node_id, node in metrics["node_metrics"].items():
        print(f"  {node_id:22} 访问{node['visits']:4} 均耗时{node['avg_dwell_hours']:>8}h "
              f"退回率{node['return_rate']} SLA达成率{node.get('sla_achievement_rate')}")
    print()
    print(f"输出: {OUT_DIR / 'metrics_v1.json'}")


if __name__ == "__main__":
    main()
