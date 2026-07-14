"""生成请假流程 v2 的合成事件日志（分析侧闭环 Phase 5）。

**v2 定义不在这里造**——它由 scripts/run_design_feedback.py 通过"分析 agent 诊断
→ 设计 agent 把建议翻成编辑"的真实回灌产出（process_v2.json），人只做业务决策与
采纳确认，不手写编辑。本脚本只负责：读那份 agent 产出的 v2 定义 + 构造 v2 场景
（削弱病灶=建模改进）+ 生成 v2 事件日志。

两类改进分清楚：
- **结构性改进**（v2 定义里的真实修改，靠 path sampler 机械生效、非调参）：由回灌
  产出，如升级门槛 >7天→>14天，让更多短假在总经理层结束、不涌向最慢环节。
- **建模改进**（下面 _V2_DEFECT_OVERRIDES 对注入病灶的削弱，代表改进措施的预期
  效果；幅度是假设，但每项对应一条采纳的建议）：SLA/催办→慢环节提速、超时减少；
  表单校验→退回下降；强制校验→违规跳级归零；异常告警→人工介入减少。

控变量：v2 与 v1 同到达分布/假期类型/天数分布/实例量/随机种子；路由改动会改变
后续消耗的随机数，故两版为同分布聚合对比（对比脚本给出两版 case-mix 证明可比）。

前置：先跑 gen_leave_process_event_log.py（v1）与 run_design_feedback.py（v2 定义）。
用法：uv run python scripts/gen_leave_process_v2.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.generation.event_log_generator import generate_event_log
from data.schema import DefectSpec, ScenarioConfig
from scripts.gen_leave_process_event_log import build_scenario_v1, summarize

OUT_DIR = Path("data/analytics/leave_request")

# v2 建模改进：注入病灶 → 削弱后的参数（每项对应一条采纳的诊断建议）
_V2_DEFECT_OVERRIDES: dict[str, dict] = {
    "slow_line_leader": {"params": {"dwell_multiplier": 2.0}, "affected_case_ratio": 0.85},  # SLA+催办：慢环节提速
    "high_return_dept_supervisor": {"params": {"return_probability": 0.10}, "affected_case_ratio": 1.0},  # 表单强校验
    "sla_breach_dept_gm": {"affected_case_ratio": 0.08},  # 处理时限+催办
    "illegal_skip_line_leader": {"affected_case_ratio": 0.0},  # 强制路由校验：违规跳级归零
    "manual_intervention_case": {"affected_case_ratio": 0.03},  # 异常自动告警
    "zombie_case": {"affected_case_ratio": 0.01},  # 监控
}


def build_scenario_v2() -> ScenarioConfig:
    """从 v1 场景出发，保持所有控变量，只削弱病灶、指向 agent 产出的 v2 定义。"""
    v2 = build_scenario_v1().model_copy(deep=True)
    v2.scenario_id = "leave_v2"
    v2.process_target_path = str(OUT_DIR / "process_v2.json")
    new_defects: list[DefectSpec] = []
    for defect in v2.injected_defects:
        override = _V2_DEFECT_OVERRIDES.get(defect.defect_id, {})
        data = defect.model_dump()
        if "params" in override:
            data["params"] = override["params"]
        if "affected_case_ratio" in override:
            data["affected_case_ratio"] = override["affected_case_ratio"]
        new_defects.append(DefectSpec.model_validate(data))
    v2.injected_defects = new_defects
    return v2


def main() -> None:
    process_v2 = OUT_DIR / "process_v2.json"
    if not process_v2.exists():
        raise SystemExit(f"未找到 {process_v2}，请先跑 scripts/run_design_feedback.py 生成 agent 回灌的 v2 定义。")

    scenario = build_scenario_v2()
    (OUT_DIR / "scenario_v2.json").write_text(scenario.model_dump_json(indent=2), encoding="utf-8")

    cases = generate_event_log(scenario)

    with (OUT_DIR / "event_log_v2.jsonl").open("w", encoding="utf-8") as f:
        for case in cases:
            f.write(case.model_dump_json() + "\n")

    summary = summarize(cases)
    (OUT_DIR / "injected_defects_v2.json").write_text(
        json.dumps(
            {"scenario_id": scenario.scenario_id, "defect_catalog": [d.model_dump() for d in scenario.injected_defects], "summary": summary},
            ensure_ascii=False, indent=2, default=str,
        ),
        encoding="utf-8",
    )

    print(f"v2 生成实例数: {summary['case_count']}  事件数: {summary['event_count']}")
    print(f"v2 实例状态分布: {summary['case_status_counts']}")
    print(f"v2 病灶命中实例数: {summary['defect_case_counts']}")
    print(f"输出目录: {OUT_DIR}")


if __name__ == "__main__":
    main()
