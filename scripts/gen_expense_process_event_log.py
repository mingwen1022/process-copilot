"""一次性生成费用报销流程（带注入病灶）的合成事件日志（P3 报销全套 · 分析轴）。

镜像 scripts/gen_leave_process_event_log.py 的做法，但案例属性用通用数值采样
（报销总额）而非请假专属的 leave_type_weights/leave_days_range，驱动金额分级
路径（<50000 直接结束 / >=50000 送财务初审+财务负责人）。

100% 合成数据，不使用/不提交任何真实数据。

用法：uv run python scripts/gen_expense_process_event_log.py
"""

from __future__ import annotations

import json
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.generation.event_log_generator import generate_event_log
from data.schema import ArrivalConfig, DefectSpec, DefectType, DwellParam, ScenarioConfig

OUT_DIR = Path("data/analytics/expense_reimbursement")


def build_scenario_v1() -> ScenarioConfig:
    return ScenarioConfig(
        scenario_id="expense_v1",
        process_target_path="data/cases/expense_reimbursement/standard/target.json",
        case_count=400,
        start_date="2026-01-01",
        end_date="2026-06-30",
        arrival=ArrivalConfig(rate_per_day=2.6, peak_multiplier={"month_end": 1.5}),
        node_dwell_params={
            "dept_manager_approve": DwellParam(mu=1.609, sigma=0.55),  # 中位数约 5 小时
            "finance_review": DwellParam(mu=1.792, sigma=0.55),  # 中位数约 6 小时
            "finance_lead_approve": DwellParam(mu=2.303, sigma=0.5),  # 中位数约 10 小时（基线，未受慢环节病灶影响）
        },
        decision_probabilities={
            "dept_manager_approve": 0.88,
            "finance_review": 0.93,
            "finance_lead_approve": 0.94,
        },
        assumed_sla_days={
            "dept_manager_approve": 1.0,
            "finance_review": 1.0,
            "finance_lead_approve": 2.0,
        },
        numeric_case_attributes={"报销总额": (300.0, 65000.0)},  # 约 23% 落在大额门槛(>=50000)之上
        injected_defects=[
            DefectSpec(
                defect_id="slow_finance_lead_approve",
                type=DefectType.SLOW_NODE,
                target_node_id="finance_lead_approve",
                params={"dwell_multiplier": 8.0},
                affected_case_ratio=0.7,
            ),
            DefectSpec(
                defect_id="return_dept_manager_approve",
                type=DefectType.HIGH_RETURN_RATE,
                target_node_id="dept_manager_approve",
                params={"return_probability": 0.15},
                affected_case_ratio=1.0,
            ),
            DefectSpec(
                defect_id="manual_intervention_case",
                type=DefectType.MANUAL_INTERVENTION,
                affected_case_ratio=0.05,
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
    (OUT_DIR / "scenario_v1.json").write_text(scenario.model_dump_json(indent=2), encoding="utf-8")

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


if __name__ == "__main__":
    main()
