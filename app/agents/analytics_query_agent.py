"""对话式指标问答 agent（分析侧闭环 Phase 3.2）。

镜像设计侧 design_edit_agent 的架构：LLM 只做"理解这句话是纯提问、还是要算个
新指标"，把要算的东西解析成结构化 AdHocMetricQuery；具体怎么算由确定性代码
（app.analytics.query.run_ad_hoc_query）执行，数字由前端渲染成结果卡片——LLM
不在 reply 里报数字。

三种情况：
1. 纯提问（答案已在给定的指标快照里）→ queries 为空，reply 直接照数据回答。
2. 要算新指标 → 生成一个或多个 AdHocMetricQuery，reply 说明将要算什么。
3. 要求"以后都跟踪" → 在对应 query 上把 persist 置 true。
超出当前单个流程分析范围（管别的流程、改流程、发布上线等）→ out_of_scope=true。
"""

from __future__ import annotations

import json
from typing import Any

from pydantic import BaseModel, Field

from app.analytics.query import AdHocMetricQuery
from app.models.bedrock import create_bedrock_chat_model
from app.models.text import is_legacy_text_generator


class AnalyticsQueryProposal(BaseModel):
    reply: str = Field(description="给用户看的简短中文回复：解释这轮回答了什么，或要算什么指标。不要在这里报具体数字。")
    queries: list[AdHocMetricQuery] = Field(
        default_factory=list,
        description="需要新计算的临时指标；如果只是照现有快照回答问题，返回空列表。",
    )
    out_of_scope: bool = Field(
        default=False,
        description="用户请求超出当前单个流程的指标分析范围（管别的流程/改流程/发布上线等）时为 true，此时 queries 必须为空。",
    )


def _system_prompt() -> str:
    return """你是企业 OA 流程效能分析的对话助手，帮流程 owner 看懂并追问一个流程的运行指标。
你会拿到该流程当前的结构化指标快照（JSON）和已登记的自定义指标列表。

判断用户这句话属于哪种情况：
1. 纯提问，且答案已经在指标快照里（比如问某环节耗时、退回率、办结时长）——直接用
   快照里的数字在 reply 里回答，queries 返回空列表。
2. 需要算快照里没有的临时指标（比如“请假天数超过10天但1天内办完的有几个”“各部门
   的平均办结时长”）——把它解析成 AdHocMetricQuery，reply 里说明将要算什么，不要
   自己编造数字。
3. 用户要求把某个指标“以后都跟踪/常态化/固定盯着”——在对应 query 上把 persist 置为
   true（如果这个指标是上一轮刚算过的，也重新给出对应的 query 并 persist=true）。

AdHocMetricQuery 的字段：
- scope: case（按流程实例）或 event（按环节任务）
- filters: 过滤条件列表，每条是 {field, operator, value}，多条之间是“与”关系
  - 可用的 case 字段：case_status、请假类型、请假天数、initiator_dept_name、
    duration_hours（办结时长小时）、node_visits、is_completed、has_return、
    has_manual_intervention、**node_id/node_name（这个实例经过的环节集合，列表值；
    问"哪些/多少实例经过了某环节"要用 scope=case 配这两个字段，= 表示"经过/包含"，
    不要下钻到 event 粒度去数环节任务数——那数的是访问次数不是实例数）**
  - 可用的 event 字段：node_id、node_name、action、action_category、resource_name、
    resource_dept_name、dwell_hours、task_order、请假类型、请假天数
  - operator: = / != / > / >= / < / <= / in（in 时 value 用列表；对列表值字段
    （如 case 的 node_id/node_name）= 和 in 都表示"成员关系/包含"）
- aggregate: count（计数）/ rate（占同 scope 全部的比例）/ avg_dwell_hours（平均时长）
- group_by: 可选，按某字段分组
- persist: 是否转正为常态化指标

严格约束：
- 只基于给定快照和数据字段，不要臆造快照里没有的字段或数字。
- 只有明确要新计算时才给 queries；能用快照直接回答就别生成查询。
- 超出单个流程指标分析范围（管别的流程、修改流程定义、发布上线等）→ out_of_scope=true、
  queries 为空。
- reply 用简洁中文，不要在里面报将要计算的具体数值（数值由系统算出后单独展示）；
  不要使用英文双引号。"""


def _user_prompt(*, message: str, metrics_snapshot: dict[str, Any], conversation_context: str, custom_metric_names: list[str]) -> str:
    snapshot = json.dumps(_trim_snapshot(metrics_snapshot), ensure_ascii=False, indent=2)
    history = conversation_context.strip() or "（无历史对话）"
    custom = "、".join(custom_metric_names) if custom_metric_names else "（暂无）"
    return f"""当前流程指标快照（JSON）：
{snapshot}

已登记的自定义指标：{custom}

对话历史：
{history}

本轮用户的问题：
{message}

请判断这是纯提问还是要算新指标，并据此给出结构化回复。"""


class ToolCallTrace(BaseModel):
    """一次工具调用的完整记录（工具名+入参+结果），供前端渲染成卡片/拦截提示。"""

    tool: str
    args: dict[str, Any] = Field(default_factory=dict)
    result: dict[str, Any] = Field(default_factory=dict)


class AnalyticsToolRunResult(BaseModel):
    """run_with_tools 的输出：reply 是解读，tool_calls 是过程中的每一次取数
    （agent 可能 0 次、1 次或多次调用，也可能先调 run_metric_query 不够再升级
    到 run_sql）。数字的可信来源是 tool_calls 里的结果，不是 reply 的转述。"""

    reply: str
    tool_calls: list[ToolCallTrace] = Field(default_factory=list)


def _render_design_candidates(candidates: list[Any]) -> str:
    if not candidates:
        return "（当前无确定性检出的效能问题候选）"
    lines = []
    for c in candidates:
        lines.append(f"- id={c.bottleneck_id} | 环节={c.node_name or c.node_id} | 严重度={c.severity}\n  {c.description}")
    return "\n".join(lines)


def _tool_system_prompt(
    *,
    metrics_snapshot: dict[str, Any],
    schema_ddl: str,
    custom_metric_names: list[str],
    conversation_context: str,
    design_candidates: list[Any] | None = None,
) -> str:
    snapshot = json.dumps(_trim_snapshot(metrics_snapshot), ensure_ascii=False, indent=2)
    custom = "、".join(custom_metric_names) if custom_metric_names else "（暂无）"
    history = conversation_context.strip() or "（无历史对话）"
    candidates_text = _render_design_candidates(design_candidates or [])
    return f"""你是企业 OA 流程效能分析的对话助手，帮流程 owner 看懂并追问一个流程的运行指标。

当前流程指标快照（JSON，多数常见问题的答案已经在这里）：
{snapshot}

已登记的自定义指标：{custom}

当前确定性候选（系统按阈值算出的真实效能问题，供你判断要不要反哺给设计侧）：
{candidates_text}

对话历史：
{history}

你有三个工具可用：
1. 快照里已经能直接回答的问题——不要调用任何工具，直接在最终回复里回答。
2. 快照答不了、但能用"过滤+聚合+可选分组"表达的新指标（如"病假超过3天的有几个"
   "各部门平均办结时长"）——调用 run_metric_query。这是类型化查询，安全、
   可穷举测试，优先于 run_sql。
3. run_metric_query 也表达不了的需求（多表 JOIN、复杂条件组合、跨环节下钻、
   大范围聚合）——才调用 run_sql，对下面的表结构写一条只读 SELECT：

{schema_ddl}

4. flag_design_issue：对话明确指向上面"确定性候选"里的一条、且用户像是在陈述一个
   真实观察到的问题（不是随口假设或纯提问）——可以直接调用，参数 candidate_id 用
   候选列表里的 id，不要编造。反哺不等于自动改流程，设计侧还要人工确认才会真的
   修改，所以这一步风险低，判断明确就可以直接调，不用每次都先问。如果拿不准
   用户是不是真要反哺、或者看不出对应哪条候选，就先用自然语言问一句（如"这个
   要不要我记进反哺提醒给设计侧？"），等用户确认后下一轮再调用，不要自己直接调。

严格约束：
- 只基于快照和工具返回的真实数据回答，不要编造数字。
- 工具返回 {{"error": ...}} 时，根据错误信息改写重试，或如实告诉用户查不了，
  不要假装查到了、也不要编一个数字搪塞。
- 最终回复用简洁中文，不要在文字里逐条罗列原始数字/表格（结果会由系统单独
  渲染成卡片），只做解读和结论；不要使用英文双引号。
- 超出单个流程指标分析范围（管别的流程、修改流程定义、发布上线等）——不要
  调用任何工具，直接在回复里说明这超出分析范围。"""


def _extract_tool_run_result(response: Any) -> AnalyticsToolRunResult:
    messages = response.get("messages", []) if isinstance(response, dict) else getattr(response, "messages", [])
    call_args_by_id: dict[str, dict[str, Any]] = {}
    tool_calls: list[ToolCallTrace] = []
    final_reply = ""

    for msg in messages:
        tool_call_entries = getattr(msg, "tool_calls", None) or []
        for entry in tool_call_entries:
            call_id = entry.get("id")
            if call_id:
                call_args_by_id[call_id] = {"name": entry.get("name", ""), "args": entry.get("args", {})}

        if type(msg).__name__ == "ToolMessage":
            call_id = getattr(msg, "tool_call_id", None)
            info = call_args_by_id.get(call_id, {})
            content = getattr(msg, "content", "")
            try:
                result = json.loads(content) if isinstance(content, str) else content
            except (TypeError, ValueError):
                result = {"raw": content}
            if not isinstance(result, dict):
                result = {"value": result}
            tool_calls.append(
                ToolCallTrace(
                    tool=info.get("name") or getattr(msg, "name", "") or "unknown_tool",
                    args=info.get("args", {}),
                    result=result,
                )
            )
        elif type(msg).__name__ == "AIMessage":
            content = getattr(msg, "content", "")
            if isinstance(content, str) and content.strip():
                final_reply = content

    if not final_reply:
        final_reply = "没能生成有效回复，请换个问法再试一次。"
    return AnalyticsToolRunResult(reply=final_reply, tool_calls=tool_calls)


def _trim_snapshot(metrics: dict[str, Any]) -> dict[str, Any]:
    """给 LLM 的快照去掉体量大、逐条明细类的字段（如 conformance_violations 列表），
    只保留聚合结论，避免 prompt 过长又无助于回答。"""
    trimmed = dict(metrics)
    path = trimmed.get("path_metrics")
    if isinstance(path, dict):
        path = dict(path)
        path.pop("conformance_violations", None)
        trimmed["path_metrics"] = path
    return trimmed


class AnalyticsQueryAgent:
    def __init__(self, model: Any | None = None) -> None:
        self.model = model or create_bedrock_chat_model()
        self.structured_model = (
            None
            if is_legacy_text_generator(self.model)
            else self.model.with_structured_output(AnalyticsQueryProposal, method="function_calling", include_raw=True)
        )

    def run(
        self,
        *,
        message: str,
        metrics_snapshot: dict[str, Any],
        conversation_context: str = "",
        custom_metric_names: list[str] | None = None,
    ) -> AnalyticsQueryProposal:
        if not message.strip():
            return AnalyticsQueryProposal(reply="请输入一个关于该流程指标的问题。", queries=[])
        if self.structured_model is None:
            return AnalyticsQueryProposal(reply="当前模型不可用，无法解析指标问题。", queries=[])

        response = self.structured_model.invoke(
            [
                {"role": "system", "content": _system_prompt()},
                {
                    "role": "user",
                    "content": _user_prompt(
                        message=message,
                        metrics_snapshot=metrics_snapshot,
                        conversation_context=conversation_context,
                        custom_metric_names=custom_metric_names or [],
                    ),
                },
            ]
        )
        return _coerce_proposal(response)

    def run_with_tools(
        self,
        *,
        message: str,
        metrics_snapshot: dict[str, Any],
        schema_ddl: str,
        tools: list[Any],
        conversation_context: str = "",
        custom_metric_names: list[str] | None = None,
        design_candidates: list[Any] | None = None,
        max_tool_calls: int = 4,
    ) -> AnalyticsToolRunResult:
        """带工具的对话循环（L2 typed / L3 SQL 两级取数梯度 + 反哺闭环场景二的
        flag_design_issue）。快照仍进 system prompt——大多数问题走 L1（快照直接答），
        0 次工具调用；快照答不了再由 agent 自己决定升级到哪一级工具。见
        app/analytics/sql_store.py 和 app/tools/{metric_query_tool,sql_query_tool,
        design_feedback_tool}.py 的模块 docstring。"""
        if not message.strip():
            return AnalyticsToolRunResult(reply="请输入一个关于该流程指标的问题。")
        if is_legacy_text_generator(self.model):
            return AnalyticsToolRunResult(reply="当前模型不可用，无法解析指标问题。")

        from langchain.agents import create_agent

        agent = create_agent(
            model=self.model,
            tools=tools,
            system_prompt=_tool_system_prompt(
                metrics_snapshot=metrics_snapshot,
                schema_ddl=schema_ddl,
                custom_metric_names=custom_metric_names or [],
                conversation_context=conversation_context,
                design_candidates=design_candidates,
            ),
        )
        try:
            response = agent.invoke(
                {"messages": [{"role": "user", "content": message}]},
                config={"recursion_limit": max_tool_calls * 2 + 4},
            )
        except Exception as exc:  # noqa: BLE001 - 出错时给用户一个可读的降级答复
            return AnalyticsToolRunResult(reply=f"分析过程中出错：{exc}")

        return _extract_tool_run_result(response)


def _coerce_proposal(response: Any) -> AnalyticsQueryProposal:
    parsed: Any = None
    parsing_error: Any = None
    if isinstance(response, dict):
        parsed = response.get("parsed")
        parsing_error = response.get("parsing_error")
    elif isinstance(response, AnalyticsQueryProposal):
        parsed = response
    else:
        parsed = getattr(response, "parsed", None)
        parsing_error = getattr(response, "parsing_error", None)

    if parsing_error or parsed is None:
        return AnalyticsQueryProposal(reply="我没能理解这个问题，能否换一种更具体的表达？例如“病假超过3天的案例有多少个”。", queries=[])
    if not isinstance(parsed, AnalyticsQueryProposal):
        try:
            parsed = AnalyticsQueryProposal.model_validate(parsed)
        except Exception:  # noqa: BLE001 - 解析失败一律降级为"没理解"
            return AnalyticsQueryProposal(reply="我没能理解这个问题，能否换一种更具体的表达？", queries=[])
    if parsed.out_of_scope:
        parsed.queries = []
    return parsed
