"""一次性生成请假流程 v1（带注入病灶）的合成事件日志（分析侧闭环 Phase 0+1）。

100% 合成数据，不使用/不提交任何真实数据；字段设计参照真实 EOA 事件日志校准
（见 doc/产品功能全景.md 模块7/8）。scenario 配置 + 生成器见：
- data/schema/event_log_schema.py（ScenarioConfig / CaseRecord / EventRecord）
- app/generation/event_log_generator.py（path sampler）

用法：uv run python scripts/gen_leave_process_event_log.py
"""

from __future__ import annotations

import json
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.generation.event_log_generator import generate_event_log
from data.schema import ArrivalConfig, DefectSpec, DefectType, DwellParam, ScenarioConfig

OUT_DIR = Path("data/analytics/leave_request")


def build_scenario_v1() -> ScenarioConfig:
    return ScenarioConfig(
        scenario_id="leave_v1",
        process_target_path="data/cases/leave_request/standard/target.json",
        case_count=600,
        start_date="2026-01-01",
        end_date="2026-06-30",
        arrival=ArrivalConfig(rate_per_day=4.0, peak_multiplier={"month_end": 1.6}),
        node_dwell_params={
            "dept_supervisor": DwellParam(mu=1.386, sigma=0.6),  # 中位数约 4 小时
            "dept_gm": DwellParam(mu=2.079, sigma=0.6),  # 中位数约 8 小时
            "line_leader": DwellParam(mu=2.079, sigma=0.5),  # 中位数约 8 小时（未受慢环节病灶影响的基线）
        },
        decision_probabilities={
            "dept_supervisor": 0.90,
            "dept_gm": 0.92,
            "line_leader": 0.95,
        },
        assumed_sla_days={
            "dept_supervisor": 1.0,
            "dept_gm": 2.0,
            "line_leader": 3.0,
        },
        leave_type_weights={
            "年假": 0.30,
            "事假": 0.25,
            "病假": 0.20,
            "婚假": 0.05,
            "产假": 0.05,
            "陪产假": 0.05,
            "丧假": 0.10,
        },
        leave_days_range={
            "年假": (1, 10),
            "事假": (1, 5),
            "病假": (1, 7),
            "婚假": (3, 10),
            "产假": (90, 158),
            "陪产假": (7, 15),
            "丧假": (1, 15),
        },
        injected_defects=[
            DefectSpec(
                defect_id="slow_line_leader",
                type=DefectType.SLOW_NODE,
                target_node_id="line_leader",
                params={"dwell_multiplier": 15.0},
                affected_case_ratio=0.85,
            ),
            DefectSpec(
                defect_id="high_return_dept_supervisor",
                type=DefectType.HIGH_RETURN_RATE,
                target_node_id="dept_supervisor",
                params={"return_probability": 0.35},
                affected_case_ratio=1.0,
            ),
            DefectSpec(
                defect_id="sla_breach_dept_gm",
                type=DefectType.SLA_BREACH,
                target_node_id="dept_gm",
                # 0.40：让 dept_gm 的 SLA 达成率明显跌破 80% 阈值，成为一个
                # 确实值得检出的超时问题（0.15 时达成率仍有 85%，够不成堵点）。
                affected_case_ratio=0.40,
            ),
            DefectSpec(
                defect_id="illegal_skip_line_leader",
                type=DefectType.ILLEGAL_SKIP,
                target_node_id="dept_gm",
                params={"force_target_node_id": "END"},
                affected_case_ratio=0.05,
            ),
            DefectSpec(
                defect_id="manual_intervention_case",
                type=DefectType.MANUAL_INTERVENTION,
                # 0.12：使实际人工介入比例落在 ~10%，明显高于健康水位（真实全局
                # 看板约 1.7%）和 5% 告警阈值，成为一个可检出的运维介入问题。
                affected_case_ratio=0.12,
            ),
            DefectSpec(
                defect_id="zombie_case",
                type=DefectType.ZOMBIE_INSTANCE,
                affected_case_ratio=0.02,
            ),
        ],
        random_seed=42,
    )


def summarize(cases: list) -> dict:
    status_counts = Counter(case.case_status.value for case in cases)
    defect_case_counts: Counter = Counter()
    defect_event_counts: Counter = Counter()
    total_events = 0
    for case in cases:
        for defect_id in case.injected_defect_ids:
            defect_case_counts[defect_id] += 1
        for event in case.events:
            total_events += 1
            for defect_id in event.injected_defect_labels:
                defect_event_counts[defect_id] += 1
    return {
        "case_count": len(cases),
        "event_count": total_events,
        "case_status_counts": dict(status_counts),
        "defect_case_counts": dict(defect_case_counts),
        "defect_event_counts": dict(defect_event_counts),
    }


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    scenario = build_scenario_v1()
    (OUT_DIR / "scenario_v1.json").write_text(
        scenario.model_dump_json(indent=2), encoding="utf-8"
    )

    cases = generate_event_log(scenario)

    log_path = OUT_DIR / "event_log_v1.jsonl"
    with log_path.open("w", encoding="utf-8") as f:
        for case in cases:
            f.write(case.model_dump_json() + "\n")

    summary = summarize(cases)
    gold = {
        "scenario_id": scenario.scenario_id,
        "defect_catalog": [d.model_dump() for d in scenario.injected_defects],
        "summary": summary,
    }
    (OUT_DIR / "injected_defects_v1.json").write_text(
        json.dumps(gold, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
    )

    print(f"生成实例数: {summary['case_count']}  事件数: {summary['event_count']}")
    print(f"实例状态分布: {summary['case_status_counts']}")
    print(f"病灶命中实例数: {summary['defect_case_counts']}")
    print(f"病灶命中事件数: {summary['defect_event_counts']}")
    print(f"输出目录: {OUT_DIR}")


if __name__ == "__main__":
    main()
