"""分析侧反哺闭环 · 场景二的对话工具（把 AnalyticsInsightProducer 包成 tool-calling 能调的形状）。

跟 metric_query_tool/sql_query_tool 同一个心法：LLM 只负责"这轮对话要不要调用"，
不负责"这算不算问题"——后者已经被 detect_candidates()（确定性阈值判定）判过了。
这个工具只认调用方传进来的 candidates 列表（服务端此刻自己刚算出来的真实候选，
已经按 SUPPORTED_ANALYTICS_CATEGORIES 筛过能反哺的类别），agent 传一个不在这份
列表里的 candidate_id 就会被拒绝——它编不出候选，只能确认一个真实存在的。

具体调用时机（写在 agent 的 system prompt 里，不是这个模块的职责）：对话明确指向
某条候选、且用户像是在陈述真实观察到的问题——可以直接调用；拿不准就先问用户，
用户确认后再调用。反哺进去的只是"设计工作台会看到一条提醒"，不代表流程被改了——
真正的修改仍然要走设计侧现成的"确认应用"人工关卡，所以这一步允许 agent 有更高的
自主度，不强制每次都先问。
"""

from __future__ import annotations

import json
from typing import Any

from langchain_core.tools import StructuredTool

from app.analytics.thresholds import CandidateBottleneck
from app.insights.producers import AnalyticsInsightProducer


def build_flag_design_issue_tool(
    candidates: list[CandidateBottleneck], *, insight_store: Any | None, workflow_definition_id: str
) -> StructuredTool:
    by_id = {c.bottleneck_id: c for c in candidates}

    def flag_design_issue(candidate_id: str) -> str:
        if insight_store is None:
            return json.dumps({"error": "反哺存储未接入，无法记录。"}, ensure_ascii=False)
        candidate = by_id.get(candidate_id)
        if candidate is None:
            return json.dumps(
                {
                    "error": f"candidate_id={candidate_id!r} 不在当前确定性候选列表里，"
                    "只能反哺服务端刚算出的真实候选，不能凭空指定或编造。",
                },
                ensure_ascii=False,
            )
        recorded = AnalyticsInsightProducer.record_from_candidates(
            insight_store, workflow_definition_id=workflow_definition_id, candidates=[candidate],
        )
        if not recorded:
            return json.dumps({"error": "该候选类别暂不支持反哺（未落地到 InsightKind）。"}, ensure_ascii=False)
        return json.dumps(
            {"recorded": True, "headline": recorded[0].headline, "node_name": candidate.node_name},
            ensure_ascii=False,
        )

    return StructuredTool.from_function(
        func=flag_design_issue,
        name="flag_design_issue",
        description=(
            "把一条确定性候选堵点反哺给设计侧（会出现在流程设计工作台的运行侧提醒里，"
            "供 owner 参考是否要调整流程设计；不代表流程会被自动修改，实际修改仍需人工"
            "在设计工作台确认）。参数 candidate_id 必须是当前对话上下文里给你的"
            "「确定性候选」列表中真实存在的 bottleneck_id，不能编造或用不在列表里的 id。"
        ),
    )
