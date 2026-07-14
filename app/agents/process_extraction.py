from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from langgraph.config import get_stream_writer

from app.io_utils import extract_json_object
from app.models.bedrock import create_bedrock_chat_model
from app.models.text import generate_text, is_legacy_text_generator, message_content_to_text
from app.process_conventions import render_conventions_for_prompt
from app.run_layout import node_log_dir
from app.tracing import serialize_agent_response, serialize_llm_call, to_jsonable, write_trace
from app.workflows.state import WorkflowState
from data.schema import ProcessDefinition


class ProcessExtractionAgent:
    """Agent node that extracts a ProcessDefinition from raw source context."""

    def __init__(self, model: Any | None = None, agent: Any | None = None) -> None:
        self.model = model or create_bedrock_chat_model()
        self.structured_model = agent or (
            None if is_legacy_text_generator(self.model) else self._create_structured_model(self.model)
        )

    def run(self, state: WorkflowState) -> WorkflowState:
        node_dir = node_log_dir(state["out_dir"], "process_extraction_agent")
        node_dir.mkdir(parents=True, exist_ok=True)
        attempt = state.get("validation_retry_count", 0)
        _emit_agent_progress("正在构建 ProcessDefinition 抽取 prompt", attempt=attempt)
        system_prompt = _system_prompt(include_schema=self.structured_model is None)
        user_prompt = _build_user_prompt(state)
        raw_output = ""
        trace_payload: dict[str, Any] | None = None
        try:
            if self.structured_model is None:
                _emit_agent_progress("正在调用文本模型生成 JSON", attempt=attempt)
                raw_output = generate_text(self.model, system_prompt, user_prompt)
                _emit_agent_progress("正在解析文本模型返回的 JSON", attempt=attempt)
                trace_payload = serialize_llm_call(
                    agent="process_extraction_agent",
                    step=f"extract_attempt_{attempt}",
                    system_prompt=system_prompt,
                    user_prompt=user_prompt,
                    assistant_output=raw_output,
                )
                (node_dir / f"llm_raw_output_attempt_{attempt}.txt").write_text(raw_output, encoding="utf-8")
                candidate = ProcessDefinition.model_validate(extract_json_object(raw_output))
            else:
                messages = [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ]
                _emit_agent_progress("正在调用 Bedrock structured output", attempt=attempt)
                extraction = _coerce_structured_response(
                    self.structured_model.invoke(messages),
                    input_messages=messages,
                    step=f"extract_attempt_{attempt}",
                )
                raw_output = extraction.raw_output
                trace_payload = extraction.trace_payload
                (node_dir / f"llm_raw_output_attempt_{attempt}.txt").write_text(raw_output, encoding="utf-8")
                write_trace(trace_payload, node_dir / f"messages_attempt_{attempt}.json")
                if extraction.candidate is None:
                    _emit_agent_progress("structured output 未通过解析，正在执行格式修复", attempt=attempt)
                    extraction = self._repair_structured_output(
                        raw_output=raw_output,
                        parsing_error=extraction.parsing_error or "structured output parser returned no parsed value",
                        node_dir=node_dir,
                        attempt=attempt,
                    )
                    raw_output = extraction.raw_output
                candidate = extraction.candidate
                if candidate is None:
                    raise ValueError(
                        "structured output parsing failed"
                        + (f": {extraction.parsing_error}" if extraction.parsing_error else "")
                    )
            if trace_payload is not None and self.structured_model is None:
                write_trace(trace_payload, node_dir / f"messages_attempt_{attempt}.json")
            if candidate is not None and not raw_output.strip():
                raw_output = candidate.model_dump_json(indent=2)
            if self.structured_model is not None:
                (node_dir / f"llm_raw_output_final_attempt_{attempt}.txt").write_text(raw_output, encoding="utf-8")
            if state.get("verbose"):
                _print_verbose_extraction(raw_output, retry_count=state.get("validation_retry_count", 0))
            _emit_agent_progress("ProcessDefinition 草稿抽取完成", attempt=attempt)
            return {**state, "candidate_process": candidate, "llm_raw_output": raw_output, "schema_errors": []}
        except Exception as exc:  # provider, parser, or schema validation may fail before a candidate exists
            _emit_agent_progress(f"ProcessDefinition 抽取失败：{exc}", status="failed", attempt=attempt)
            if raw_output:
                (node_dir / f"llm_raw_output_attempt_{attempt}.txt").write_text(raw_output, encoding="utf-8")
            error_payload = trace_payload or {
                "input_messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
            }
            error_payload.update(
                {
                    "agent": "process_extraction_agent",
                    "step": f"extract_attempt_{attempt}",
                    "error_type": exc.__class__.__name__,
                    "message": str(exc),
                    "raw_output_path": str(node_dir / f"llm_raw_output_attempt_{attempt}.txt") if raw_output else None,
                }
            )
            write_trace(error_payload, node_dir / f"messages_attempt_{attempt}.json")
            if state.get("verbose"):
                print(f"[process_extraction_agent] error={exc.__class__.__name__}: {exc}")
            return {
                **state,
                "candidate_process": None,
                "llm_raw_output": raw_output,
                "schema_errors": [
                    {
                        "loc": ["process_extraction_agent"],
                        "msg": str(exc),
                        "type": exc.__class__.__name__,
                    }
                ],
            }

    def _repair_structured_output(
        self,
        *,
        raw_output: str,
        parsing_error: Any,
        node_dir: Path,
        attempt: int,
    ) -> "_StructuredExtraction":
        messages = [
            {"role": "system", "content": _format_repair_system_prompt()},
            {
                "role": "user",
                "content": _format_repair_user_prompt(raw_output=raw_output, parsing_error=parsing_error),
            },
        ]
        repair = _coerce_structured_response(
            self.structured_model.invoke(messages),
            input_messages=messages,
            step=f"format_repair_attempt_{attempt}",
        )
        (node_dir / f"llm_repair_raw_output_attempt_{attempt}.txt").write_text(repair.raw_output, encoding="utf-8")
        write_trace(repair.trace_payload, node_dir / f"messages_format_repair_attempt_{attempt}.json")
        return repair

    @staticmethod
    def _create_structured_model(model: Any) -> Any:
        return model.with_structured_output(
            ProcessDefinition,
            method="function_calling",
            include_raw=True,
        )


def _emit_agent_progress(message: str, *, status: str = "running", attempt: int = 0) -> None:
    try:
        writer = get_stream_writer()
    except Exception:
        return
    try:
        writer(
            {
                "kind": "node_step",
                "node": "process_extraction_agent",
                "status": status,
                "message": message,
                "attempt": attempt,
            }
        )
    except Exception:
        return


@dataclass(frozen=True, slots=True)
class _StructuredExtraction:
    candidate: ProcessDefinition | None
    raw_output: str
    parsing_error: str | None
    trace_payload: dict[str, Any]


def _coerce_structured_response(
    response: Any,
    *,
    input_messages: list[dict[str, Any]],
    step: str,
) -> _StructuredExtraction:
    raw_output = _extract_structured_raw_text(response)
    parsed: Any = None
    parsing_error: Any = None

    if isinstance(response, dict):
        parsed = response.get("parsed")
        parsing_error = response.get("parsing_error")
    elif isinstance(response, ProcessDefinition):
        parsed = response
    else:
        parsed = getattr(response, "parsed", None)
        parsing_error = getattr(response, "parsing_error", None)

    trace_payload = serialize_agent_response(response, input_messages=input_messages)
    trace_payload.update(
        {
            "agent": "process_extraction_agent",
            "step": step,
            "raw_output": raw_output,
            "parsing_error": str(parsing_error) if parsing_error else None,
        }
    )

    if parsing_error:
        return _StructuredExtraction(None, raw_output, str(parsing_error), trace_payload)

    if parsed is None:
        return _StructuredExtraction(None, raw_output, "structured output parser returned no parsed value", trace_payload)

    try:
        if isinstance(parsed, ProcessDefinition):
            candidate = parsed
        else:
            candidate = ProcessDefinition.model_validate(parsed)
    except Exception as exc:
        return _StructuredExtraction(None, raw_output, str(exc), trace_payload)

    if not raw_output:
        raw_output = candidate.model_dump_json(indent=2)
        trace_payload["raw_output"] = raw_output
    return _StructuredExtraction(candidate, raw_output, None, trace_payload)


def _extract_structured_raw_text(response: Any) -> str:
    if isinstance(response, dict):
        raw = response.get("raw")
        if raw is not None:
            text = _raw_message_to_text(raw)
            if text:
                return text
        if isinstance(response.get("output"), str):
            return response["output"]
        messages = response.get("messages")
        if messages:
            return message_content_to_text(getattr(messages[-1], "content", messages[-1]))
        parsed = response.get("parsed")
        if isinstance(parsed, ProcessDefinition):
            return parsed.model_dump_json(indent=2)
        if parsed is not None:
            return json.dumps(parsed, ensure_ascii=False, indent=2)

    if isinstance(response, ProcessDefinition):
        return response.model_dump_json(indent=2)
    return _raw_message_to_text(response)


def _raw_message_to_text(message: Any) -> str:
    content = message_content_to_text(getattr(message, "content", message))
    if content:
        return content

    tool_calls = getattr(message, "tool_calls", None)
    if tool_calls:
        if len(tool_calls) == 1 and isinstance(tool_calls[0], dict) and "args" in tool_calls[0]:
            return json.dumps(to_jsonable(tool_calls[0]["args"]), ensure_ascii=False, indent=2)
        return json.dumps(to_jsonable(tool_calls), ensure_ascii=False, indent=2)

    additional_kwargs = getattr(message, "additional_kwargs", None)
    if isinstance(additional_kwargs, dict) and additional_kwargs.get("tool_calls"):
        return json.dumps(to_jsonable(additional_kwargs["tool_calls"]), ensure_ascii=False, indent=2)

    return ""


def _format_repair_system_prompt() -> str:
    return """你是 JSON 格式修复器，只负责把上一轮模型输出修复为符合 ProcessDefinition schema 的合法 JSON。

要求：
1. 不重新理解业务，不新增、不删除、不改写可从原文中看出的业务信息。
2. 只修复 JSON/Schema 格式问题，例如内部双引号转义、尾逗号、缺失括号、字段类型、None/null、布尔值等。
3. 输出必须是一个完整 JSON object，不要输出 Markdown、解释、代码块或额外文字。
4. 如果文本字段里需要表示引号，优先改成中文单引号或直接去掉引号，避免生成非法 JSON。
"""


def _format_repair_user_prompt(*, raw_output: str, parsing_error: Any) -> str:
    return f"""上一轮输出没有通过 ProcessDefinition structured output 校验。

错误信息：
{parsing_error}

请只修复下面这段输出的 JSON/Schema 格式，返回修复后的完整 JSON object：

{raw_output}
"""


def _system_prompt(*, include_schema: bool = True) -> str:
    schema = json.dumps(ProcessDefinition.model_json_schema(), ensure_ascii=False)
    conventions_section = render_conventions_for_prompt()
    schema_section = (
        f"""
ProcessDefinition JSON Schema:
{schema}
"""
        if include_schema
        else ""
    )
    return f"""你是企业 OA 流程设计 Agent，负责把多份零散 source material 整合为 V1 标准流程定义。

要求：
1. 输出必须符合 ProcessDefinition schema。
2. 若材料中存在早期讨论与后续确认冲突，优先采用后续正式确认、会议纪要、明确拍板结论。
3. V1 只保留简化字段范围：表单字段、流程环节、提交路径、附件、角色。
4. 不生成 Excel、draw.io、Mermaid 或报告。
5. 不发起补问；缺失信息由后续 business validation 和设计页待确认项暴露。
6. 如果 source 中包含已跑过的线下流转记录、历史样例单据、邮件转办链路、纸质签批截图或类似实例材料，只能作为识别业务习惯、角色、字段和可能路径的参考；线上流程应以制度规则、需求说明、会议确认和明确拍板口径为主，不要机械照搬线下实例中的临时转办、纸质签字、邮件转发、线下备案等节点。
7. 对线下实例中的操作，优先判断是否应映射为线上系统字段、通知、归档、可见性或附件要求；只有 source 明确要求线上人工审批/处理时，才新增对应流程环节。
8. 文本内容避免直接嵌入未转义英文双引号；需要引用固定短语时用中文单引号。

字段命名与规范化规则（通用，与具体流程无关）：
1. 表单字段名忠实使用 source 中给出的业务短名，不要带流程名前缀，也不要自行同义改写。例如 source 写“联系电话”就输出“联系电话”，不要改成“紧急联系方式”；source 写“工作交接人”就保留，不要改成“代理人”。
2. 只有 source 明确提到标题/单据标题，或说明标题可系统自动生成时，才输出“标题”字段（component_type=只读文本，required_stages=[]，visible_stages=["all"]，editable_stages=[]）；source 未提及标题则不要凭空输出。
3. 系统自动带出/只读展示的字段（如申请人、所属部门等）输出为只读文本，不要输出为员工选择或部门选择：component_type=只读文本，required_stages=[]，visible_stages=["all"]，editable_stages=[]。
4. 用户在起草环节填写或选择的字段，required_stages 使用 ["draft"]；不要用 ["all"] 表示必填。
5. 系统自动计算且无需用户填写的字段，required_stages 使用 []，editable_stages 使用 []。
6. 不要编造处理时限。只有 source 明确出现审批时限、处理期限、限时办结、截止时间时，才填写 time_limit_days；否则为 null。
7. 不要编造附件和角色配置。source 未明确要求附件配置时 attachments=null；source 未要求单独角色清单时 roles=null。

审批环节与路径规范化规则（通用系统约定，与具体流程无关）：
1. 起草环节固定 node_id="draft"，node_name="起草"。
2. 节点 node_id 用英文蛇形命名，node_name 忠实使用 source 中的中文环节称呼；handler.role 忠实使用 source 中的角色称呼，不要自行改写为其它组织角色名。
3. 审批环节意见条件统一写成“结论性意见=同意”或“结论性意见=不同意”。
4. 退回起草路径统一 target_node_id="DRAFT"，path_name="退回起草"，condition="结论性意见=不同意"。
5. 流程正常结束路径统一 path_name="流程结束"，target_node_id="END"。
6. 节点与路径只依据 source 实际描述输出，不要套用任何固定流程模板或预设节点清单。

冲突处理规则：
1. 如果 source 中出现早期口径和后续口径冲突，优先采用后续正式会议或明确拍板结论。
2. 如果后续材料把某项列为待确认，但前文已有明确业务口径，V1 草稿先采用最近一次明确业务口径，不要输出 <UNKNOWN>。
3. 对这种仍需业务复核的事项，在 logic_description 或路径条件中保持确定值；后续 business validation 会生成待确认项。

{conventions_section}
{schema_section}"""


def _user_prompt(source_context: str) -> str:
    return f"""请根据以下 source material 合成标准流程定义。

{source_context}
"""


def _build_user_prompt(state: WorkflowState) -> str:
    base_prompt = _user_prompt(state["source_context"])
    validation_feedback = state.get("validation_feedback", "")
    if state.get("validation_retry_count", 0) > 0 and validation_feedback:
        return f"""{base_prompt}

请基于同一批 source material 重新检查上一轮输出。注意：
1. 只根据 source material 补齐或修正，不要因为 validator 提示而编造原文没有的信息。
2. 如果 source material 确实缺少某类业务要素，保留为空列表或空值，后续会输出缺失报告。
3. 优先修复 validator 明确指出的问题。

validator 反馈：
{validation_feedback}
"""
    return base_prompt

def _print_verbose_extraction(raw_output: str, *, retry_count: int) -> None:
    print(f"[process_extraction_agent] retry_count={retry_count} raw_output_begin")
    print(raw_output)
    print("[process_extraction_agent] raw_output_end")
