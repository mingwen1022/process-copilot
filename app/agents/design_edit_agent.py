"""对话式修改 agent：把用户的一条自然语言指令解析成结构化编辑操作。

心法（北极星 §7）：这不是自由聊天 agent，是在结构化对象（ProcessDefinition）
上操作的 agent——通用在交互，专用在工具。LLM 只做"理解指令 + 决定要不要改、
改哪里"，具体怎么改（apply_edit_operations）、改完是否合法（校验）、改了什么
（diff）全部是确定性代码，不再经过 LLM。
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from app.models.bedrock import create_bedrock_chat_model
from app.models.text import is_legacy_text_generator
from app.process_conventions import render_conventions_for_prompt
from app.tools.process_edit_tools import EditOperation
from data.schema import ProcessDefinition


class EditProposal(BaseModel):
    reply: str = Field(description="给用户看的简短中文回复：说明本轮做了什么修改，或为什么不能/不需要修改。")
    operations: list[EditOperation] = Field(
        default_factory=list,
        description="要在当前流程定义上执行的结构化编辑操作；如果本轮只是回答问题、确认口径而不涉及结构变更，返回空列表。",
    )
    resolved_clarification_ids: list[str] = Field(
        default_factory=list,
        description="本轮回复实质性回答/确认了的待确认项 id 列表；未涉及任何待确认项时为空列表。",
    )
    addressed_insight_ids: list[str] = Field(
        default_factory=list,
        description=(
            "本轮提出的编辑操作确实是针对哪条运行侧洞察提的方案，就带上对应 insight id"
            "（id 见洞察列表里的 [id=...] 标注）；只声明「这次改动回应了哪条问题」，不代表"
            "「已经解决」，无关或不确定就留空列表，不要为了填满而乱猜关联。"
        ),
    )
    out_of_scope: bool = Field(
        default=False,
        description="用户请求超出当前单一流程定义的编辑范围（例如要求管理其它流程、修改系统权限、发布上线等）时为 true；此时 operations 必须为空。",
    )


def _system_prompt() -> str:
    conventions_section = render_conventions_for_prompt()
    return f"""你是企业 OA 流程设计的对话式修改助手，负责把用户的一条中文指令，转换成对"当前这一个流程草稿"的结构化编辑操作。

范围守卫（严格遵守）：
1. 只能编辑当前对话绑定的这一个 ProcessDefinition 草稿；不要臆造、不要生成新的独立流程。
2. 用户要求超出这个范围（管理其它流程、修改系统权限/组织架构、审批发布上线等）时，operations 返回空列表，out_of_scope=true，reply 里说明这超出当前设计范围。
3. 只根据用户消息里明确给出的信息生成操作；用户没提到的属性不要猜测着改，能不改就不改。
4. 如果指令模糊到无法安全生成结构化操作（例如没有说清具体字段/环节/条件），operations 返回空列表，reply 里用一句话反问用户需要澄清的点，不要瞎改。

可用的编辑操作类型（每类 add / update / remove）：
- 表单字段（add_form_field / update_form_field / remove_form_field）
- 流程环节（add_flow_node / update_flow_node / remove_flow_node）
- 环节的提交路径（add_submit_path / update_submit_path / remove_submit_path）
- 附件配置（add_attachment / update_attachment / remove_attachment）
- 自定义角色（add_role / update_role / remove_role）
- 流程元信息（update_meta，仅限 process_name/responsible_dept/description/applicant_scope/entry_point）

编辑操作使用规则：
1. 改名：提交路径改名用 update_submit_path（path_name 填旧名定位、updates.new_path_name 填新名），这是一步到位的支持操作，不要用删+加。其它对象（field_name/node_id/role_name/attachment_type）仍不支持改名，需要改名时用 remove 旧的 + add 新的表达。
1b. 在两个环节之间插入新环节：用 add_flow_node 并带上 after_node_id=前一个环节的 node_id（否则新环节会排到列表末尾、界面上显示在最后，跟"插在中间"的直觉不符）。同时要把原来直接连接这两个环节的那条提交路径，改成指向新环节（update_submit_path 改 target_node_id），并且如果那条路径的名字提到了旧目标（如"送X审批"），一并用 new_path_name 改成指向新环节的名字，避免留下"名字说送A、实际送B"的路径。新环节自己也要有送往原下游环节的提交路径。
1c. 在两个表单字段之间插入新字段：用 add_form_field 并带上 after_field_name=前一个字段的 field_name（不填就会排到末尾）。新字段的 seq 不用自己算、也不用另外发 update_form_field 去调整其它字段的 seq——插入位置和编号都由确定性代码根据插入点自动处理。
2. update 类操作里，updates 对象的字段留空（不填）表示这次不改那个属性；只有用户明确要求清空某属性时才使用对应的 clear_* 标记（如 clear_time_limit_days、clear_condition、clear_department）。
3. 新增环节/字段/路径必须给出完整、合法的定义（不能只给部分属性）。
4. 涉及新增流程环节的 submit_paths、handler、opinion 等，参照下面的系统词表；不要发明词表之外的枚举值。
5. 如果用户消息是在回答/确认某个"待确认项"（澄清问题），在 resolved_clarification_ids 里带上对应 id；如果这个确认会改变草稿内容，同时给出对应的编辑操作。
5b. 如果本轮的编辑操作是针对下面"运行侧反哺的洞察"里某一条提出的方案（不管改的是环节本身还是表单字段等别处），在 addressed_insight_ids 里带上对应 id；无关或不确定就留空，不要乱猜关联——你只需要声明"这次改动是回应哪条问题"，不需要也不能判断"是否真的解决了"，那要等新一轮真实运行数据验证。
6. 附件"按业务条件必传"（如"病假且请假天数≥3天才必须上传证明"）要写进 attachment 自己的 required_condition 字段，不要写进其它字段（如某个表单字段的 logic_description）的说明文字里——那样前端界面看不到这个条件。required_stages 仍表示"哪些环节允许/要求上传"，required_condition 是叠加的业务条件，二者独立。
7. reply 里描述的每一处具体改动（"把 X 改成了 Y"）必须真实对应 operations 里某个操作的 updates/字段取值——不能 reply 里说改了，对应的 updates 字段却留空或值跟原来一样。reply 和 operations 必须完全一致，因为落库靠 operations、reply 只是给用户看的说明；两者不一致会导致用户以为改完了、实际草稿没变。
8. reply 描述修改时用「拟议/中性」措辞（如"将把…调整为…""修改方案：…"），不要用"已修复/已完成/已新增/已删除"这类断言修改已经生效的说法——你的修改是否真正写入草稿由系统在你之后决定（结构性改动还要先经用户确认才写入），此刻你并不知道会不会立即生效，说"已完成"可能与"待用户确认"的真实状态矛盾。

{conventions_section}

运行侧反哺的洞察（如果上下文里给了）：
- 这些是运维/分析系统对着这条流程真实运行数据确定性检出的问题（覆盖漏洞、效能瓶颈等），不是猜测，不需要怀疑真实性，也不需要让用户重新确认里面的具体数字/原因。
- 用户的意图只要涉及了解这条流程的现状、找问题、找优化空间（不管具体怎么措辞问的），都应该主动结合这些洞察来回答，不要只对着流程定义本身做静态审查；用户明确只是要求一个具体的修改动作、跟这些洞察无关时，不用主动提。
- 如果洞察指向某个具体缺陷（如某环节缺一档审批路径）且用户的意图包含"处理/优化"，可以直接给出对应的编辑操作；只是被问及"有什么问题/能优化什么"时，先把发现讲清楚，不要没被要求就动手改。

回复要求：
- reply 用简短中文说明本轮做了什么（或为什么没做），不要逐条复述 JSON。
- 不要在 reply 里使用英文双引号；需要引用词语时用中文引号。
"""


def _render_open_insights(open_insights: list[dict[str, Any]]) -> str:
    """反哺闭环的运行侧洞察渲染成给 agent 看的文本——覆盖漏洞/效能瓶颈等，运维/分析系统
    确定性检出的真实运行问题。跟 open_clarifications 同一个待遇：每轮都摆在 agent 面前，
    不用它自己去查、也不用怀疑真实性。"""
    if not open_insights:
        return "（当前没有运行侧反哺的洞察）"
    lines: list[str] = []
    for insight in open_insights:
        node = insight.get("node_id") or "（流程级）"
        source = "运维" if insight.get("source") == "ops" else "分析"
        headline = insight.get("headline") or ""
        severity = insight.get("severity") or ""
        occurrences = insight.get("occurrences")
        window = insight.get("window") or ""
        insight_id = insight.get("insight_id") or ""
        detail = f"- [id={insight_id}][{node}] {headline}（来源：{source}洞察 · 严重度 {severity}"
        if occurrences:
            detail += f" · {window}内命中 {occurrences} 次"
        detail += "）"
        blocked_paths = (insight.get("evidence") or {}).get("blocked_paths") or []
        if blocked_paths:
            reasons = "；".join(
                f"「{b.get('path_name')}」条件不满足（{b.get('reason')}）" for b in blocked_paths
            )
            detail += f"\n  具体原因：{reasons}"
        lines.append(detail)
    return "\n".join(lines)


def _user_prompt(
    *,
    process: ProcessDefinition,
    instruction: str,
    conversation_context: str,
    open_clarifications: list[dict[str, Any]],
    source_context: str = "",
    open_insights: list[dict[str, Any]] | None = None,
) -> str:
    process_json = process.model_dump_json(indent=2)
    clarification_lines = "\n".join(
        f"- id={item.get('id')}：{item.get('question')}" + (f"（选项：{item.get('options')}）" if item.get("options") else "")
        for item in open_clarifications
    ) or "（当前没有未解决的待确认项）"
    history = conversation_context.strip() or "（无历史对话）"
    sources_section = ""
    if source_context.strip():
        sources_section = f"""

补充材料（用户上传的 source，作为本轮指令的依据；如果本轮指令要求"根据这份材料补充/修改"，从这里找依据，不要编造材料中没有的内容）：
{source_context.strip()}"""
    insights_section = _render_open_insights(open_insights or [])
    return f"""当前流程定义（ProcessDefinition JSON）：
{process_json}

未解决的待确认项：
{clarification_lines}

运行侧反哺的洞察（运维/分析系统对这条流程实际运行情况确定性检出的问题，非 AI 猜测）：
{insights_section}

对话历史（供理解上下文，不要重复处理里面已经处理过的指令）：
{history}
{sources_section}

本轮用户的最新指令：
{instruction}

请据此给出结构化的编辑提议。"""


class DesignEditAgent:
    """对话式修改 agent：单轮指令 -> EditProposal（结构化编辑操作 + 回复文案）。"""

    def __init__(self, model: Any | None = None) -> None:
        self.model = model or create_bedrock_chat_model()
        self.structured_model = (
            None
            if is_legacy_text_generator(self.model)
            else self.model.with_structured_output(EditProposal, method="function_calling", include_raw=True)
        )

    def run(
        self,
        *,
        process: ProcessDefinition,
        instruction: str,
        conversation_context: str = "",
        open_clarifications: list[dict[str, Any]] | None = None,
        source_context: str = "",
        open_insights: list[dict[str, Any]] | None = None,
    ) -> EditProposal:
        if not instruction.strip():
            return EditProposal(reply="本轮没有新的设计指令，流程定义保持不变。", operations=[])
        if self.structured_model is None:
            return EditProposal(reply="当前模型不支持结构化编辑，无法执行该指令，请在右侧配置页手动调整。", operations=[])

        system_prompt = _system_prompt()
        user_prompt = _user_prompt(
            process=process,
            instruction=instruction,
            conversation_context=conversation_context,
            open_clarifications=open_clarifications or [],
            source_context=source_context,
            open_insights=open_insights,
        )
        response = self.structured_model.invoke(
            [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ]
        )
        return _coerce_edit_proposal(response)


def _coerce_edit_proposal(response: Any) -> EditProposal:
    parsed: Any = None
    parsing_error: Any = None
    if isinstance(response, dict):
        parsed = response.get("parsed")
        parsing_error = response.get("parsing_error")
    elif isinstance(response, EditProposal):
        parsed = response
    else:
        parsed = getattr(response, "parsed", None)
        parsing_error = getattr(response, "parsing_error", None)

    if parsing_error or parsed is None:
        return EditProposal(
            reply="我没能理解这条指令，能否换一种更具体的表达？例如“把请假事由改成必填”。",
            operations=[],
        )
    if not isinstance(parsed, EditProposal):
        try:
            parsed = EditProposal.model_validate(parsed)
        except Exception:  # noqa: BLE001 - 解析失败一律降级为"没理解"，不让异常冒到调用方
            return EditProposal(reply="我没能理解这条指令，能否换一种更具体的表达？", operations=[])
    if parsed.out_of_scope:
        parsed.operations = []
    return parsed
