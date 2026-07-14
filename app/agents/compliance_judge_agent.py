"""定性规则合规判断 agent（模块3 · Phase 3 · LLM 部分）。

结构化能判的规则走确定性检查（app.rag.compliance）；剩下定性的（回避、命名规范
等）交这个 agent 用 LLM-judge：给它规则表述 + 流程定义，让它判是否合规、给理由。
一次调用判所有定性规则，输出 typed 结果。LLM 只判定性项、不碰确定性规则。
"""

from __future__ import annotations

import json
from typing import Any

from pydantic import BaseModel, Field

from app.models.bedrock import create_bedrock_chat_model
from app.models.text import is_legacy_text_generator
from data.schema import AtomicRule, ProcessDefinition


class RuleViolation(BaseModel):
    node_id: str = Field(description="违规涉及的环节 node_id（必须是输入流程环节里真实存在的 node_id，不要编造）；"
                          "涉及两个环节的规则（如相邻回避）用其中主要一个，另一个在 detail 里写清楚")
    detail: str = Field(description="这个环节具体哪里违反了规则")


class RuleVerdict(BaseModel):
    rule_id: str = Field(description="对应规则 id，必须与输入中某条一致")
    violations: list[RuleViolation] = Field(
        default_factory=list,
        description="这条规则在当前流程里的所有违规实例，按环节逐条列举；完全合规时留空列表，不要笼统给一句话。",
    )

    @property
    def compliant(self) -> bool:
        return not self.violations


class ComplianceJudgeOutput(BaseModel):
    verdicts: list[RuleVerdict] = Field(default_factory=list)


def _system_prompt() -> str:
    return """你是企业 OA 流程的合规审查助手，只负责判断一批**定性规则**在给定流程定义上是否
满足。每条规则给了 id 和表述，你要逐条判断该规则在这个流程里的所有违规实例。

严格约束：
- 只依据每条规则的 statement 字面要求判断，不要额外脑补 statement 没提到的标准——每条规则的
  判断范围以它的表述文字为准，不要自己加码。例如"审批环节应以处理角色加动作方式命名"这条，
  只判断环节名称本身是不是"角色类别+动作"的格式（如"部门主管审批"："部门主管"=角色类别，
  "审批"=动作）；**不要**额外要求名称里的角色词与该环节 handler.role 字段的具体取值逐字一致
  ——角色词可以是泛称、简称（如用"公司领导"指代更长的正式职位头衔），这不是这条规则管的事，
  节点名称和 handler.role 字段本来就是两个独立的东西，没有强制一致的要求。
- 只依据给定的流程定义判断，不臆造流程里没有的环节/字段。
- 违规必须**按环节逐个列举**（violations 里每一项对应一个具体环节及其违规原因），不要把多个
  环节的问题合并成一句笼统描述——下游要靠"哪个具体环节违规"来判断"这轮编辑是不是让某个
  环节从合规变成了不合规"，笼统合并会让这个判断做不了。
- violations 里每个 node_id 必须是输入流程环节列表里真实存在的 node_id。
- 完全合规的规则，violations 给空列表，不要编造违规凑数。
- verdicts 里每条的 rule_id 必须与输入中某条规则一致，且每条规则都要覆盖（哪怕 violations 是空的）。
- 拿不准时倾向判为不合规并说明存疑点，不要放水；但"拿不准"指规则本身要求的事项存疑，不包括
  规则没要求的事项。
- 用简洁中文；不要用英文双引号。"""


def _user_prompt(process: ProcessDefinition, rules: list[AtomicRule]) -> str:
    node_lines = "\n".join(
        f"  - {n.node_id} | {n.node_name} | 角色={(n.handler.role if n.handler else '起草人')} | "
        f"处理方式={(n.handler.mode.value if n.handler and n.handler.mode else '-')}"
        for n in process.flow_nodes
    )
    rule_lines = "\n".join(f"  - id={r.rule_id}：{r.statement}" for r in rules)
    return f"""流程名称：{process.meta.process_name}

流程环节（角色/处理方式是这个环节实际配置的处理人信息，只给需要用到处理人信息的规则
——如判断相邻环节是否解析成同一人——参考；跟环节名称是否规范是两件独立的事，不代表
环节名称必须跟这里的角色字面一致）：
{node_lines}

待判断的定性规则：
{rule_lines}

请逐条判断每个规则被该流程违反的所有具体环节（每个环节一条 violation），完全合规的规则给空列表。"""


class ComplianceJudgeAgent:
    def __init__(self, model: Any | None = None) -> None:
        self.model = model or create_bedrock_chat_model()
        self.structured_model = (
            None
            if is_legacy_text_generator(self.model)
            else self.model.with_structured_output(ComplianceJudgeOutput, method="function_calling", include_raw=True)
        )

    def judge(self, process: ProcessDefinition, rules: list[AtomicRule]) -> list[RuleVerdict]:
        qualitative = [r for r in rules if r.check_type == "llm_judge"]
        if not qualitative or self.structured_model is None:
            return []
        response = self.structured_model.invoke(
            [
                {"role": "system", "content": _system_prompt()},
                {"role": "user", "content": _user_prompt(process, qualitative)},
            ]
        )
        output = _coerce(response)
        if output is None:
            # 真实踩过的失败模式：模型把 verdicts 这个数组字段整体编码成了一段 JSON 字符串
            # （tool_calls[0]['args']['verdicts'] 是 str 不是 list），Pydantic 校验直接判整体
            # 失败、判断结果全部丢弃——即便模型的判断内容其实是对的（这不是偶发抽样噪声，
            # 连续 3 次同样输入都复现了同一种畸形）。且这段"JSON 字符串"本身有时还带着转义
            # 错误、不是简单 json.loads 就能修好的合法 JSON。所以分两步：先试便宜的确定性
            # 修复（parse 那段字符串），解决不了再退一步让模型自己修（跟 process_extraction
            # agent 的 format-repair retry 同一个套路，只是这里换成 ComplianceJudgeOutput）。
            output = _repair_stringified_verdicts(response)
        if output is None:
            output = self._repair_via_retry(response)
        if output is None:
            return []
        valid_ids = {r.rule_id for r in qualitative}
        return [v for v in output.verdicts if v.rule_id in valid_ids]

    def _repair_via_retry(self, response: Any) -> ComplianceJudgeOutput | None:
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
        return _coerce(repaired)


def _coerce(response: Any) -> ComplianceJudgeOutput | None:
    parsed: Any = None
    parsing_error: Any = None
    if isinstance(response, dict):
        parsed = response.get("parsed")
        parsing_error = response.get("parsing_error")
    elif isinstance(response, ComplianceJudgeOutput):
        parsed = response
    else:
        parsed = getattr(response, "parsed", None)
        parsing_error = getattr(response, "parsing_error", None)
    if parsing_error or parsed is None:
        return None
    if not isinstance(parsed, ComplianceJudgeOutput):
        try:
            parsed = ComplianceJudgeOutput.model_validate(parsed)
        except Exception:  # noqa: BLE001
            return None
    return parsed


def _repair_stringified_verdicts(response: Any) -> ComplianceJudgeOutput | None:
    """便宜的第一次尝试：verdicts 字符串本身恰好是合法 JSON 时，纯确定性解析出来，
    不用再多一次网络调用。字符串本身就是畸形 JSON（常见：内部双引号没转义）时解析
    失败，交给 _repair_via_retry 那个更贵但更稳的兜底。"""
    raw = response.get("raw") if isinstance(response, dict) else None
    tool_calls = getattr(raw, "tool_calls", None) or []
    for call in tool_calls:
        args = call.get("args") if isinstance(call, dict) else None
        if not isinstance(args, dict):
            continue
        verdicts = args.get("verdicts")
        if not isinstance(verdicts, str):
            continue
        try:
            fixed_verdicts = json.loads(verdicts)
        except (TypeError, ValueError):
            continue
        try:
            return ComplianceJudgeOutput.model_validate({**args, "verdicts": fixed_verdicts})
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
    return """你是 JSON 格式修复器，只负责把上一轮模型输出修复为符合 ComplianceJudgeOutput schema 的合法 JSON。

要求：
1. 不重新判断合规结论，不改变任何 rule_id/node_id/detail 的实际内容。
2. 只修复 JSON/Schema 格式问题：verdicts 必须是原生 JSON 数组（不能整体编码成一段字符串），
   每条 verdict 的 violations 也必须是原生 JSON 数组，内部双引号转义、尾逗号、缺失括号等
   语法问题都要修。
3. 输出必须是一个完整 JSON object（形如 {"verdicts": [{"rule_id": "...", "violations": [...]}]})，
   不要输出 Markdown、解释、代码块或额外文字。
4. detail 文本里如果原本包含未转义的英文双引号，改成中文引号或直接去掉，避免再次生成非法 JSON。
"""


def _format_repair_user_prompt(*, raw_output: str, parsing_error: Any) -> str:
    return f"""上一轮输出没有通过 ComplianceJudgeOutput structured output 校验。

错误信息：
{parsing_error}

请只修复下面这段输出的 JSON/Schema 格式，返回修复后的完整 JSON object：

{raw_output}
"""
