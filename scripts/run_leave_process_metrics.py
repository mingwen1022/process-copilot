"""对请假流程 v1 合成事件日志跑一遍确定性指标层（分析侧闭环 Phase 2）。

前置：先跑 scripts/gen_leave_process_event_log.py 生成 event_log_v1.jsonl。
用法：uv run python scripts/run_leave_process_metrics.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.analytics.metrics import compute_metrics, load_case_records
from app.generation.event_log_generator import load_process_definition
from data.schema import ScenarioConfig

OUT_DIR = Path("data/analytics/leave_request")


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
    print(f"最后环节平均停滞(小时): {overview['avg_last_node_dwell_hours']}")
    print(f"人工介入比例: {overview['manual_intervention_case_ratio']}")
    print()
    print("各环节指标：")
    for node_id, node in metrics["node_metrics"].items():
        print(f"  {node_id:16} 访问{node['visits']:4} 均耗时{node['avg_dwell_hours']:>8}h "
              f"人均访问{node['avg_visits_per_case']} 退回率{node['return_rate']} "
              f"SLA达成率{node.get('sla_achievement_rate')}")
    print()
    path = metrics["path_metrics"]
    print(f"返工率: {path['rework_rate']}  变体种类: {path['distinct_variant_count']}  "
          f"违规跳级: {path['conformance_violation_count']}")
    print()
    manual = metrics["manual_intervention"]
    print(f"人工介入事件总数: {manual['total_ops_events']}")
    print(f"按运维前环节: {manual['by_prior_node']}")
    print(f"下送路径分布: {manual['down_path_distribution']}")
    print()
    print(f"输出: {OUT_DIR / 'metrics_v1.json'}")


if __name__ == "__main__":
    main()
