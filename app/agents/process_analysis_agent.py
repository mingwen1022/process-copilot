"""运营分析 agent（分析侧闭环 Phase 3.1 · LLM 部分）。

心法（北极星 §7，与设计侧 design_edit_agent 同一套）：确定性代码负责"是什么、
有多严重"（app.analytics.thresholds 已筛出候选堵点，含确定性 severity），LLM
只负责"为什么、怎么改"——即针对每个候选堵点写归因和改进建议。LLM 不碰原始
日志、不重算指标、不改 severity。

产出的 suggested_process_edit_instruction 是一条自然语言的设计修改指令，为
Phase 5"把建议回灌设计 agent 产 v2 定义"铺路——本阶段只生成，不自动执行。
"""

from __future__ import annotations

import json
from typing import Any

from pydantic import BaseModel, Field

from app.analytics.thresholds import CandidateBottleneck, ThresholdConfig, detect_candidates
from app.models.bedrock import create_bedrock_chat_model
from app.models.text import is_legacy_text_generator


class BottleneckAttribution(BaseModel):
    """LLM 针对单个候选堵点产出的文案（只含归因和建议，不含任何数字/判定）。"""

    bottleneck_id: str = Field(description="对应候选堵点的 id，必须与输入中的某个 bottleneck_id 完全一致")
    root_cause: str = Field(description="用一两句话解释这个堵点最可能的成因，只基于给定指标，不臆造数据")
    suggestion: str = Field(description="针对性的流程优化建议，具体可落地")
    suggested_process_edit_instruction: str | None = Field(
        default=None,
        description="若该建议可转成对流程定义的一条修改指令（如加处理时限、改路由条件），"
        "用一句自然语言写出来；不适用则为 null",
    )


class DiagnosisAttributionOutput(BaseModel):
    """LLM 单次调用的结构化输出。"""

    summary: str = Field(description="用两三句话概括本流程当前最突出的效能问题")
    attributions: list[BottleneckAttribution] = Field(default_factory=list)


class DiagnosisItem(BaseModel):
    """一个候选堵点 + 其归因建议的合并结果（确定性字段来自 thresholds，文案来自 LLM）。"""

    bottleneck_id: str
    category: str
    node_id: str | None = None
    node_name: str | None = None
    severity: str
    description: str
    metric_reference: dict[str, Any] = Field(default_factory=dict)
    root_cause: str = ""
    suggestion: str = ""
    suggested_process_edit_instruction: str | None = None


class KnowledgeSnippet(BaseModel):
    source: str = Field(description="知识出处，如 rule_id 或 doc_id/子句")
    text: str


class DiagnosisReport(BaseModel):
    process_name: str
    candidate_count: int
    summary: str = ""
    items: list[DiagnosisItem] = Field(default_factory=list)
    llm_available: bool = True
    # 本次诊断检索并提供给 LLM 的知识库依据（运营基准/相关制度）；可能为空（诚实空）。
    knowledge_context: list[KnowledgeSnippet] = Field(default_factory=list)
    # 生成时间戳与 id 由服务层在持久化时写入（agent 保持无副作用/确定性，不碰时间/随机）。
    generated_at: str | None = None
    report_id: str | None = None


def _system_prompt() -> str:
    return """你是企业 OA 流程的运营分析助手。系统已经用确定性规则从流程运行数据里筛出了一批
“候选堵点”，每个都带好了类别、严重度和支撑指标数字。你的职责只有两件：
1. 针对每个候选堵点，用一两句话解释最可能的成因（root_cause）；
2. 给出具体、可落地的流程优化建议（suggestion）；如果这条建议可以转成对流程定义的
   一条修改（比如给某环节加处理时限、调整某条路由的跳转条件、增加提醒/催办），
   就在 suggested_process_edit_instruction 里用一句自然语言写出来。

严格约束：
- 只能基于给定的候选堵点和指标数字来分析，不要臆造没有给出的数据或指标。
- 不要修改、不要复述严重度判定，也不要自己重新计算指标——那些是系统算好的。
- attributions 里每一项的 bottleneck_id 必须与输入中的某个候选堵点 id 完全一致，
  且每个候选堵点都要覆盖到。
- 若给了“参考知识（运营基准/相关制度）”，归因和建议可引用它、并注明出处（如
  “依据处理时限标准，部门总经理审批时限应为2天”）；**未提供参考知识、或其与该堵点
  无关时，绝不要编造制度/基准引用**——没有依据就只依据指标本身分析。
- 回复用简洁中文；不要使用英文双引号，需要引用词语时用中文引号。"""


def _user_prompt(process_name: str, candidates: list[CandidateBottleneck], knowledge: list[KnowledgeSnippet]) -> str:
    lines = [f"流程名称：{process_name}", "", "候选堵点清单（已按严重度排序）："]
    for candidate in candidates:
        lines.append(
            f"- id={candidate.bottleneck_id} | 类别={candidate.category} | 严重度={candidate.severity}\n"
            f"  描述：{candidate.description}\n"
            f"  关键指标：{candidate.metric_reference}"
        )
    lines.append("")
    lines.append("参考知识（运营基准/相关制度，可引用并注明出处；下方为空则表示未检索到相关依据，请勿编造引用）：")
    if knowledge:
        for snippet in knowledge:
            lines.append(f"- 【{snippet.source}】{snippet.text}")
    else:
        lines.append("（未检索到与本流程堵点相关的知识依据）")
    lines.append("")
    lines.append("请针对上面每一个候选堵点给出成因与建议，并总体概括本流程的效能问题。")
    return "\n".join(lines)


class ProcessAnalysisAgent:
    def __init__(
        self,
        model: Any | None = None,
        threshold_config: ThresholdConfig | None = None,
        knowledge_index: Any | None = None,
    ) -> None:
        self.model = model or create_bedrock_chat_model()
        self.threshold_config = threshold_config or ThresholdConfig()
        # 知识库索引可注入；缺省惰性构造（真嵌入）。检索走 retrieve-then-read + 阈值：
        # 无相关知识就返回空，不硬凑——LLM 被要求"无依据不编造引用"。
        self._knowledge_index = knowledge_index
        self._knowledge_index_ready = knowledge_index is not None
        self.structured_model = (
            None
            if is_legacy_text_generator(self.model)
            else self.model.with_structured_output(DiagnosisAttributionOutput, method="function_calling", include_raw=True)
        )

    def diagnose(self, metrics: dict[str, Any]) -> DiagnosisReport:
        process_name = metrics.get("process_name") or "流程"
        candidates = detect_candidates(metrics, self.threshold_config)

        if not candidates:
            return DiagnosisReport(
                process_name=process_name,
                candidate_count=0,
                summary="未检出超过告警阈值的候选堵点，当前流程运行指标处于正常区间。",
                items=[],
                llm_available=self.structured_model is not None,
            )

        if self.structured_model is None:
            # 没有可用模型时不编造归因，如实降级：只给确定性候选，文案留空。
            return DiagnosisReport(
                process_name=process_name,
                candidate_count=len(candidates),
                summary="当前模型不可用，仅展示确定性候选堵点，未生成 AI 归因与建议。",
                items=[_item_from_candidate(c) for c in candidates],
                llm_available=False,
            )

        knowledge = self._retrieve_knowledge(process_name, candidates)
        response = self.structured_model.invoke(
            [
                {"role": "system", "content": _system_prompt()},
                {"role": "user", "content": _user_prompt(process_name, candidates, knowledge)},
            ]
        )
        output = _coerce_attribution_output(response)
        if output is None:
            # 同一类真实踩过的失败模式（compliance_judge_agent 也有）：模型把 attributions
            # 这个数组字段整体编码成了一段 JSON 字符串，Pydantic 校验直接判整体失败、归因
            # 全部丢弃。分两步修：先试便宜的确定性修复，解决不了再退一步让模型自己修。
            output = _repair_stringified_attributions(response)
        if output is None:
            output = self._repair_via_retry(response)
        report = _merge_report(process_name, candidates, output)
        report.knowledge_context = knowledge
        return report

    def _repair_via_retry(self, response: Any) -> DiagnosisAttributionOutput | None:
        raw_text = _extract_tool_call_args_text(response)
        parsing_error = response.get("parsing_error") if isinstance(response, dict) else None
        if not raw_text or self.structured_model is None:
            return None
        repaired = self.structured_model.invoke(
            [
                {"role": "system", "content": _format_repair_system_prompt()},
                {"role": "user", "content": _format_repair_user_prompt(raw_output=raw_text, parsing_error=parsing_error)},
            ]
        )
        return _coerce_attribution_output(repaired)

    def _retrieve_knowledge(self, process_name: str, candidates: list[CandidateBottleneck]) -> list[KnowledgeSnippet]:
        """逐个堵点检索相关知识（每类堵点召回自己相关的基准/制度），按出处去重、
        限量。用 per-candidate query 而非一个宽泛合并 query，让每个堵点都拿到自己
        对口的依据（如超时→SLA基准、退回→退回基准）。检索带阈值，无关的自动丢弃。"""
        index = self._get_knowledge_index()
        if index is None:
            return []
        seen: set[str] = set()
        snippets: list[KnowledgeSnippet] = []
        for candidate in candidates[:6]:
            try:
                hits = index.semantic_search(f"{process_name} {candidate.description}", k=2)
            except Exception:  # noqa: BLE001 - 检索失败不拖垮诊断，降级为无依据
                return snippets
            for hit in hits:
                meta = hit.metadata
                source = meta.get("rule_id") or f"{meta.get('doc_id', '')}/{meta.get('clause', '')}".strip("/")
                source = source or "知识库"
                if source in seen:
                    continue
                seen.add(source)
                snippets.append(KnowledgeSnippet(source=source, text=hit.text))
            if len(snippets) >= 6:
                break
        return snippets[:6]

    def _get_knowledge_index(self) -> Any | None:
        if not self._knowledge_index_ready:
            try:
                from app.rag.index import KnowledgeIndex

                self._knowledge_index = KnowledgeIndex()
            except Exception:  # noqa: BLE001 - 无索引/嵌入不可用时降级为无知识依据
                self._knowledge_index = None
            self._knowledge_index_ready = True
        return self._knowledge_index


def _item_from_candidate(candidate: CandidateBottleneck) -> DiagnosisItem:
    return DiagnosisItem(
        bottleneck_id=candidate.bottleneck_id,
        category=candidate.category,
        node_id=candidate.node_id,
        node_name=candidate.node_name,
        severity=candidate.severity,
        description=candidate.description,
        metric_reference=candidate.metric_reference,
    )


def _merge_report(
    process_name: str,
    candidates: list[CandidateBottleneck],
    output: DiagnosisAttributionOutput | None,
) -> DiagnosisReport:
    attributions = {a.bottleneck_id: a for a in output.attributions} if output else {}
    items: list[DiagnosisItem] = []
    for candidate in candidates:
        item = _item_from_candidate(candidate)
        attribution = attributions.get(candidate.bottleneck_id)
        if attribution is not None:
            item.root_cause = attribution.root_cause
            item.suggestion = attribution.suggestion
            item.suggested_process_edit_instruction = attribution.suggested_process_edit_instruction
        items.append(item)
    return DiagnosisReport(
        process_name=process_name,
        candidate_count=len(candidates),
        summary=output.summary if output else "",
        items=items,
        llm_available=True,
    )


def _coerce_attribution_output(response: Any) -> DiagnosisAttributionOutput | None:
    parsed: Any = None
    parsing_error: Any = None
    if isinstance(response, dict):
        parsed = response.get("parsed")
        parsing_error = response.get("parsing_error")
    elif isinstance(response, DiagnosisAttributionOutput):
        parsed = response
    else:
        parsed = getattr(response, "parsed", None)
        parsing_error = getattr(response, "parsing_error", None)

    if parsing_error or parsed is None:
        return None
    if not isinstance(parsed, DiagnosisAttributionOutput):
        try:
            parsed = DiagnosisAttributionOutput.model_validate(parsed)
        except Exception:  # noqa: BLE001 - 解析失败降级为"无归因"，不让异常冒到调用方
            return None
    return parsed


def _repair_stringified_attributions(response: Any) -> DiagnosisAttributionOutput | None:
    """便宜的第一次尝试：attributions 字符串本身恰好是合法 JSON 时，纯确定性解析出来，
    不用再多一次网络调用。字符串本身就是畸形 JSON 时解析失败，交给 _repair_via_retry
    那个更贵但更稳的兜底。"""
    raw = response.get("raw") if isinstance(response, dict) else None
    tool_calls = getattr(raw, "tool_calls", None) or []
    for call in tool_calls:
        args = call.get("args") if isinstance(call, dict) else None
        if not isinstance(args, dict):
            continue
        attributions = args.get("attributions")
        if not isinstance(attributions, str):
            continue
        try:
            fixed_attributions = json.loads(attributions)
        except (TypeError, ValueError):
            continue
        try:
            return DiagnosisAttributionOutput.model_validate({**args, "attributions": fixed_attributions})
        except Exception:  # noqa: BLE001
            continue
    return None


def _extract_tool_call_args_text(response: Any) -> str | None:
    """把模型上一轮实际输出的 tool call 参数原样转成文本，给修复轮当"你刚才说了什么"看。"""
    raw = response.get("raw") if isinstance(response, dict) else None
    tool_calls = getattr(raw, "tool_calls", None) or []
    for call in tool_calls:
        args = call.get("args") if isinstance(call, dict) else None
        if isinstance(args, dict):
            return json.dumps(args, ensure_ascii=False, indent=2)
    return None


def _format_repair_system_prompt() -> str:
    return """你是 JSON 格式修复器，只负责把上一轮模型输出修复为符合 DiagnosisAttributionOutput schema 的合法 JSON。

要求：
1. 不重新判断归因结论，不改变 summary/root_cause/suggestion 的实际内容。
2. 只修复 JSON/Schema 格式问题：attributions 必须是原生 JSON 数组（不能整体编码成一段
   字符串），内部双引号转义、尾逗号、缺失括号等语法问题都要修。
3. 输出必须是一个完整 JSON object（形如 {"summary": "...", "attributions": [{"bottleneck_id": "...", ...}]}），
   不要输出 Markdown、解释、代码块或额外文字。
4. 文本字段里如果原本包含未转义的英文双引号，改成中文引号或直接去掉，避免再次生成非法 JSON。
"""


def _format_repair_user_prompt(*, raw_output: str, parsing_error: Any) -> str:
    return f"""上一轮输出没有通过 DiagnosisAttributionOutput structured output 校验。

错误信息：
{parsing_error}

请只修复下面这段输出的 JSON/Schema 格式，返回修复后的完整 JSON object：

{raw_output}
"""
