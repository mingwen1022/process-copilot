"""闭环反馈：分析 agent 诊断 → 设计 agent 自动改出 v2 定义（模块 8 · 真实 agent-to-agent）。

链路（全真实 Bedrock）：
  v1 事件日志 → 指标 → ProcessAnalysisAgent 诊断（含每条堵点的自然语言修改建议）
  → DesignEditAgent 逐条把建议解析成结构化编辑并应用 → v2 定义（process_v2.json）。

human-in-the-loop：本脚本默认采纳全部有可执行建议的堵点；实际产品里这个采纳
列表来自人的勾选（--adopt 传 bottleneck_id 子集）。翻译不由人做，由设计 agent 做。

用法：uv run python scripts/run_design_feedback.py [--adopt id1,id2]
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.agents.design_feedback import apply_diagnosis_feedback
from app.agents.process_analysis_agent import ProcessAnalysisAgent
from app.analytics.metrics import compute_metrics, load_case_records
from app.generation.event_log_generator import load_process_definition
from data.schema import ScenarioConfig

OUT_DIR = Path("data/analytics/leave_request")

# human-in-the-loop 的业务决策：设计 agent 对"短假豁免高层审批"这条建议反问过
# 门槛具体几天（M4 澄清守卫）；人给出业务参数=14天，仍由设计 agent 翻成编辑。
# 这是"人决策、agent 翻译"，不是人手写编辑。
_HUMAN_CLARIFICATIONS = {
    "slow_node:line_leader": "升级到条线分管领导审批的门槛设为请假天数超过14天；"
    "请假天数不超过14天且非婚假/产假/陪产假时，在部门总经理审批环节直接流程结束。",
}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--adopt", default=None, help="采纳的 bottleneck_id 逗号分隔；缺省=全部")
    args = parser.parse_args()
    adopted = args.adopt.split(",") if args.adopt else None

    scenario = ScenarioConfig.model_validate_json((OUT_DIR / "scenario_v1.json").read_text(encoding="utf-8"))
    process = load_process_definition(scenario.process_target_path)
    cases = load_case_records(OUT_DIR / "event_log_v1.jsonl")
    metrics = compute_metrics(cases, process=process, assumed_sla_days=scenario.assumed_sla_days)
    metrics["node_names"] = {n.node_id: n.node_name for n in process.flow_nodes}
    metrics["process_name"] = process.meta.process_name

    print("① 分析 agent 诊断 v1…")
    report = ProcessAnalysisAgent().diagnose(metrics)
    print(f"   候选堵点 {report.candidate_count} 个")

    print("② 设计 agent 逐条把建议解析成编辑并应用…")
    result = apply_diagnosis_feedback(
        process=process,
        diagnosis_items=[item.model_dump() for item in report.items],
        adopted_bottleneck_ids=adopted,
        clarifications=_HUMAN_CLARIFICATIONS,
    )

    definition = result.v2_process.model_dump(mode="json")
    definition["meta"]["version"] = "V2.0.0-draft"
    (OUT_DIR / "process_v2.json").write_text(json.dumps(definition, ensure_ascii=False, indent=2), encoding="utf-8")

    feedback_log = {
        "diagnosis_summary": report.summary,
        "total_operations": result.total_operations,
        "changed_suggestion_count": result.changed_suggestion_count,
        "applied": [a.model_dump() for a in result.applied],
    }
    (OUT_DIR / "feedback_log_v2.json").write_text(
        json.dumps(feedback_log, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    print(f"\n设计 agent 共产生 {result.total_operations} 个编辑操作，其中 {result.changed_suggestion_count} 条建议真的改动了定义：")
    for a in result.applied:
        flag = "✓改动" if a.has_changes else ("⚠️超范围" if a.out_of_scope else "—未转出编辑")
        print(f"  [{flag}] {a.bottleneck_id}：{a.instruction[:42]}… → {a.operation_count} 操作")
        if a.errors:
            print(f"        错误：{a.errors}")
    print(f"\n输出：{OUT_DIR / 'process_v2.json'} + feedback_log_v2.json")


if __name__ == "__main__":
    main()
