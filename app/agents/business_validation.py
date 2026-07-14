from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Literal

from langgraph.config import get_stream_writer
from pydantic import BaseModel, Field, ValidationInfo, field_validator

from app.io_utils import extract_json_object, find_standard_json, load_standard_target
from app.models.bedrock import create_bedrock_chat_model
from app.models.text import generate_text, is_legacy_text_generator, message_content_to_text
from app.process_conventions import render_conventions_for_prompt
from app.run_layout import node_log_dir
from app.tracing import serialize_agent_response, serialize_llm_call, to_jsonable, write_trace
from app.workflows.state import WorkflowState


EvidenceStatus = Literal["found_in_raw", "not_found_in_raw", "uncertain"]
IssueSeverity = Literal["blocking", "warning"]


class BusinessValidationIssue(BaseModel):
    issue_type: str = Field(description="问题类型，例如 missing_form_fields、missing_flow_path、unsupported_claim")
    item: str = Field(description="问题对象，例如字段名、环节名、路径条件或附件要求")
    severity: IssueSeverity = Field(description="blocking 表示阻断生成；warning 表示可继续生成但需报告")
    evidence_status: EvidenceStatus = Field(description="raw_sources 中是否能找到修复依据")
    message: str = Field(description="问题说明")
    source_hint: str | None = Field(default=None, description="raw_sources 中的来源文件/片段提示；没有依据时为 None")
    repair_instruction: str | None = Field(default=None, description="给 extraction agent 的修复指令")


class UserClarificationRequest(BaseModel):
    id: str = Field(description="稳定问题 ID")
    question: str = Field(description="给用户看的确认或补充问题")
    reason: str = Field(description="为什么需要确认")
    severity: IssueSeverity = Field(description="blocking 或 warning")
    issue_type: str = Field(description="关联的问题类型")
    item: str = Field(description="关联的问题对象")
    options: list[str] = Field(default_factory=list, description="建议选项，通常 2-3 个")
    free_text_allowed: bool = Field(default=True, description="是否允许用户自由输入")
    related_items: list[str] = Field(default_factory=list, description="关联字段、环节、路径或角色")
    source_hint: str | None = Field(default=None, description="相关 source chunk；没有依据时为 None")


class BusinessValidationResult(BaseModel):
    passed: bool = Field(description="没有 blocking issues 时为 true")
    blocking_issues: list[BusinessValidationIssue] = Field(default_factory=list, description="阻断生成的问题")
    warning_issues: list[BusinessValidationIssue] = Field(default_factory=list, description="不阻断生成的风险提示")
    raw_missing_items: list[BusinessValidationIssue] = Field(
        default_factory=list,
        description="raw_sources 本身缺少依据的问题，通常 evidence_status=not_found_in_raw",
    )
    user_clarification_requests: list[UserClarificationRequest] = Field(
        default_factory=list,
        description="需要用户确认或补充的问题，供 AI 设计助手继续追问",
    )
    repair_instructions: list[str] = Field(default_factory=list, description="汇总给 extraction agent 的修复指令")
    summary: str = Field(default="", description="本次业务校验摘要")

    @field_validator(
        "blocking_issues",
        "warning_issues",
        "raw_missing_items",
        "user_clarification_requests",
        "repair_instructions",
        mode="before",
    )
    @classmethod
    def _parse_json_encoded_list(cls, value: Any, info: ValidationInfo) -> Any:
        if not isinstance(value, str):
            return value
        text = value.strip()
        if not text:
            return []
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            if info.field_name == "repair_instructions":
                return [text]
            return value
        return parsed


class BusinessValidationAgent:
    """Agent node that reviews a candidate ProcessDefinition against raw sources."""

    def __init__(self, model: Any | None = None, agent: Any | None = None) -> None:
        self.model = model or create_bedrock_chat_model()
        self.structured_model = agent or (
            None if is_legacy_text_generator(self.model) else self.model.with_structured_output(
                BusinessValidationResult,
                method="function_calling",
                include_raw=True,
            )
        )

    def run(self, state: WorkflowState) -> WorkflowState:
        node_dir = node_log_dir(state["out_dir"], "business_validation_agent")
        node_dir.mkdir(parents=True, exist_ok=True)
        attempt = state.get("validation_retry_count", 0)
        candidate = state.get("candidate_process")
        _emit_agent_progress("正在准备业务缺口校验上下文", attempt=attempt)

        if candidate is None:
            _emit_agent_progress("候选流程缺失，无法执行业务校验", status="failed", attempt=attempt)
            result = BusinessValidationResult(
                passed=False,
                blocking_issues=[
                    BusinessValidationIssue(
                        issue_type="missing_candidate_process",
                        item="candidate_process",
                        severity="blocking",
                        evidence_status="uncertain",
                        message="没有可审核的 ProcessDefinition 候选输出",
                    )
                ],
                summary="candidate_process 缺失，无法做业务校验",
            )
            return _with_business_validation_state(state, result, raw_output="", node_dir=node_dir, attempt=attempt)

        review_context = _load_business_review_context(state.get("case_dir"))
        system_prompt = _system_prompt()
        user_prompt = _user_prompt(
            source_context=state.get("source_context", ""),
            candidate_json=candidate.model_dump_json(indent=2),
            review_context=review_context,
        )
        raw_output = ""
        try:
            if self.structured_model is None:
                _emit_agent_progress("正在调用业务校验文本模型", attempt=attempt)
                raw_output = generate_text(self.model, system_prompt, user_prompt)
                _emit_agent_progress("正在解析业务校验 JSON", attempt=attempt)
                raw_output, result = _parse_or_repair_business_validation_output(
                    self.model,
                    raw_output=raw_output,
                    node_dir=node_dir,
                    attempt=attempt,
                )
                write_trace(
                    serialize_llm_call(
                        agent="business_validation_agent",
                        step=f"business_validate_attempt_{attempt}",
                        system_prompt=system_prompt,
                        user_prompt=user_prompt,
                        assistant_output=raw_output,
                    ),
                    node_dir / f"messages_attempt_{attempt}.json",
                )
            else:
                messages = [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ]
                _emit_agent_progress("正在调用 BusinessValidationAgent structured output", attempt=attempt)
                response = self.structured_model.invoke(messages)
                try:
                    _emit_agent_progress("正在解析业务校验 structured output", attempt=attempt)
                    raw_output, result = _coerce_structured_response(response)
                    trace_payload = serialize_agent_response(response, input_messages=messages)
                except Exception as structured_exc:
                    _emit_agent_progress("structured output 解析失败，正在切换 JSON fallback", attempt=attempt)
                    raw_output = _run_json_fallback_validation(
                        self.model,
                        source_context=state.get("source_context", ""),
                        candidate_json=candidate.model_dump_json(indent=2),
                        review_context=review_context,
                        structured_error=str(structured_exc),
                    )
                    raw_output, result = _parse_or_repair_business_validation_output(
                        self.model,
                        raw_output=raw_output,
                        node_dir=node_dir,
                        attempt=attempt,
                    )
                    trace_payload = serialize_agent_response(response, input_messages=messages)
                    trace_payload["structured_output_error"] = str(structured_exc)
                    trace_payload["fallback_raw_output"] = raw_output
                trace_payload.update(
                    {
                        "agent": "business_validation_agent",
                        "step": f"business_validate_attempt_{attempt}",
                        "raw_output": raw_output,
                    }
                )
                write_trace(trace_payload, node_dir / f"messages_attempt_{attempt}.json")
            result = _apply_business_review_context(result, review_context)
            _emit_agent_progress(
                f"业务校验完成：{len(result.blocking_issues)} 个阻断项、{len(result.warning_issues)} 个提示项、{len(result.user_clarification_requests)} 个待确认项",
                attempt=attempt,
            )
            return _with_business_validation_state(state, result, raw_output=raw_output, node_dir=node_dir, attempt=attempt)
        except Exception as exc:
            _emit_agent_progress(f"业务校验失败：{exc}", status="failed", attempt=attempt)
            result = BusinessValidationResult(
                passed=False,
                blocking_issues=[
                    BusinessValidationIssue(
                        issue_type="business_validation_agent_error",
                        item="business_validation_agent",
                        severity="blocking",
                        evidence_status="uncertain",
                        message=str(exc),
                    )
                ],
                summary=f"business_validation_agent 执行失败：{exc}",
            )
            write_trace(
                {
                    "agent": "business_validation_agent",
                    "step": f"business_validate_attempt_{attempt}",
                    "error_type": exc.__class__.__name__,
                    "message": str(exc),
                    "raw_output": raw_output,
                    "input_messages": [
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": user_prompt},
                    ],
                },
                node_dir / f"messages_attempt_{attempt}.json",
            )
            return _with_business_validation_state(state, result, raw_output=raw_output, node_dir=node_dir, attempt=attempt)


def _emit_agent_progress(message: str, *, status: str = "running", attempt: int = 0) -> None:
    try:
        writer = get_stream_writer()
    except Exception:
        return
    try:
        writer(
            {
                "kind": "node_step",
                "node": "business_validation_agent",
                "status": status,
                "message": message,
                "attempt": attempt,
            }
        )
    except Exception:
        return


def _coerce_structured_response(response: Any) -> tuple[str, BusinessValidationResult]:
    raw_output = _extract_raw_text(response)
    parsed: Any = None
    parsing_error: Any = None
    if isinstance(response, dict):
        parsed = response.get("parsed")
        parsing_error = response.get("parsing_error")
    elif isinstance(response, BusinessValidationResult):
        parsed = response
    else:
        parsed = getattr(response, "parsed", None)
        parsing_error = getattr(response, "parsing_error", None)

    if parsing_error:
        recovered = _try_parse_raw_structured_output(raw_output)
        if recovered is not None:
            return raw_output or recovered.model_dump_json(indent=2), recovered
        raise ValueError(str(parsing_error))
    if parsed is None:
        raise ValueError("structured output parser returned no parsed value")
    if isinstance(parsed, BusinessValidationResult):
        result = parsed
    else:
        result = BusinessValidationResult.model_validate(parsed)
    if not raw_output:
        raw_output = result.model_dump_json(indent=2)
    return raw_output, result


def _extract_raw_text(response: Any) -> str:
    if isinstance(response, dict):
        raw = response.get("raw")
        if raw is not None:
            text = message_content_to_text(getattr(raw, "content", raw))
            if text:
                return text
            tool_calls = getattr(raw, "tool_calls", None)
            if tool_calls:
                return json.dumps(tool_calls, ensure_ascii=False, default=str, indent=2)
            invalid_tool_calls = getattr(raw, "invalid_tool_calls", None)
            if invalid_tool_calls:
                return json.dumps(to_jsonable(invalid_tool_calls), ensure_ascii=False, indent=2)
            additional_kwargs = getattr(raw, "additional_kwargs", None)
            if isinstance(additional_kwargs, dict):
                for key in ("tool_calls", "invalid_tool_calls"):
                    if additional_kwargs.get(key):
                        return json.dumps(to_jsonable(additional_kwargs[key]), ensure_ascii=False, indent=2)
        parsed = response.get("parsed")
        if isinstance(parsed, BusinessValidationResult):
            return parsed.model_dump_json(indent=2)
        if parsed is not None:
            return json.dumps(parsed, ensure_ascii=False, indent=2)
    if isinstance(response, BusinessValidationResult):
        return response.model_dump_json(indent=2)
    return message_content_to_text(getattr(response, "content", response))


def _try_parse_raw_structured_output(raw_output: str) -> BusinessValidationResult | None:
    if not raw_output.strip():
        return None
    try:
        payload = json.loads(raw_output)
    except json.JSONDecodeError:
        return None

    candidates: list[Any] = [payload]
    if isinstance(payload, list):
        for item in payload:
            if isinstance(item, dict):
                candidates.extend([item.get("args"), item.get("function", {}).get("arguments")])
    elif isinstance(payload, dict):
        candidates.extend([payload.get("args"), payload.get("function", {}).get("arguments")])
        if isinstance(payload.get("raw"), dict):
            candidates.append(payload["raw"].get("args"))

    for candidate in candidates:
        if candidate is None:
            continue
        try:
            if isinstance(candidate, str):
                candidate = json.loads(candidate)
            return BusinessValidationResult.model_validate(candidate)
        except Exception:
            continue
    return None


def _parse_or_repair_business_validation_output(
    model: Any,
    *,
    raw_output: str,
    node_dir: Path,
    attempt: int,
) -> tuple[str, BusinessValidationResult]:
    try:
        return raw_output, BusinessValidationResult.model_validate(extract_json_object(raw_output))
    except Exception as exc:
        repair_output = generate_text(
            model,
            _format_repair_system_prompt(),
            _format_repair_user_prompt(raw_output=raw_output, parsing_error=exc),
        )
        (node_dir / f"business_validation_repair_raw_output_attempt_{attempt}.txt").write_text(
            repair_output,
            encoding="utf-8",
        )
        result = BusinessValidationResult.model_validate(extract_json_object(repair_output))
        return repair_output, result


def _format_repair_system_prompt() -> str:
    schema = json.dumps(BusinessValidationResult.model_json_schema(), ensure_ascii=False, indent=2)
    return f"""你是 JSON 格式修复器，只负责把上一轮模型输出修复为符合 BusinessValidationResult schema 的合法 JSON。

要求：
1. 不重新理解业务，不新增、不删除、不改写业务判断。
2. 只修复 JSON/Schema 格式问题，例如文本字段中的英文双引号转义、尾逗号、缺失括号、字段类型、null、布尔值、数组字段。
3. 输出必须是一个完整 JSON object，不要输出 Markdown、解释、代码块或额外文字。
4. 如果文本字段里需要表示引号，优先改成中文引号“...”或直接去掉引号，避免生成非法 JSON。

BusinessValidationResult JSON Schema:
{schema}
"""


def _format_repair_user_prompt(*, raw_output: str, parsing_error: Any) -> str:
    return f"""上一轮业务校验输出没有通过 BusinessValidationResult JSON 解析。

解析错误：
{parsing_error}

上一轮输出：
{raw_output}
"""


def _with_business_validation_state(
    state: WorkflowState,
    result: BusinessValidationResult,
    *,
    raw_output: str,
    node_dir: Path,
    attempt: int,
) -> WorkflowState:
    report = result.model_dump(mode="json")
    if not raw_output:
        raw_output = result.model_dump_json(indent=2)
    (node_dir / f"business_validation_raw_output_attempt_{attempt}.txt").write_text(raw_output, encoding="utf-8")
    write_trace(report, node_dir / f"business_validation_report_attempt_{attempt}.json")
    return {
        **state,
        "business_validation_result": report,
        "business_validation_raw_output": raw_output,
    }


def _run_json_fallback_validation(
    model: Any,
    *,
    source_context: str,
    candidate_json: str,
    review_context: dict[str, Any] | None,
    structured_error: str,
) -> str:
    system_prompt = _system_prompt(include_schema=True)
    user_prompt = (
        _user_prompt(source_context=source_context, candidate_json=candidate_json, review_context=review_context)
        + "\n\n上一轮 structured output 解析失败，错误如下：\n"
        + structured_error
        + "\n\n请重新输出一个完整 JSON object。不要输出 Markdown、代码块或解释文字。"
    )
    return generate_text(model, system_prompt, user_prompt)


def _system_prompt(*, include_schema: bool = False) -> str:
    conventions_section = render_conventions_for_prompt()
    prompt = """你是企业 OA 流程设计的业务校验 Agent，负责审核候选 ProcessDefinition 是否充分、准确地反映 source material。

边界：
1. 不使用 standard/gold answer。
2. 不重新生成完整流程 JSON，只做审核和给出修复建议。
3. 必须基于 source material 举证；不要因为常识或猜测判定缺失。
4. blocking issue 只用于会导致流程草稿明显不可用的问题。
5. 如果用户人工审定过“当前 demo 边界/可接受的可选确认项”，这些内容只用于判断哪些线下或高级运行能力暂不进入当前 demo，不用于反推标准答案。

重点检查：
1. 大块缺失：表单字段、流程环节、提交路径、角色、附件。
2. source material 明确提到但候选输出遗漏的关键字段、审批环节、路径条件、角色或附件要求。
3. 候选输出中 source material 没有依据的明显编造。
4. 路径条件、审批角色、必填/可见/可编辑环节是否与 source material 明显冲突。

证据状态：
- found_in_raw：source material 有明确依据，应该退回 extraction agent 修复。
- not_found_in_raw：source material 缺少依据，不应编造，应进入缺失报告。
- uncertain：依据不清，需要谨慎处理；只在阻断质量时作为 blocking。

blocking 判定要克制：
- 只有表单字段、核心环节、核心提交路径大块缺失，或候选流程采用了明显错误且 source 中有明确可修复依据时，才作为 blocking。
- 如果 source 中存在冲突，但候选流程已经采用了一个可运行的明确口径，应作为 warning + user_clarification_requests，不要 blocking。
- 如果 source 后续写了待确认，但前文已有明确业务口径，候选流程可先采用该明确口径；校验结果只提示用户复核。
- 缺少审批时限、附件要求、字段长度、手机号格式这类发布前优化项，通常是 warning，不要 blocking。
- V1 简化表单组件允许用“单行文本 + logic_description/max_length”表达手机号、电话、账号等输入；不要仅因未使用手机号专用组件就生成 warning。
- 如果字段已在 logic_description 或 placeholder 中说明手机号/电话号码，并设置了合理长度，不要再提示手机号格式缺失。

流程设计输出规范：
{conventions_section}

V1 系统约定与运行时能力边界（通用，不要误判为问题）：
- 起草节点真实 node_id 使用 draft；审批节点退回起草路径 target_node_id 可以使用系统保留别名 DRAFT，表示退回起草，这是系统约定不是错误。
- 候选流程的字段名、节点名、角色名应忠实于 source 原文措辞；不要因为它没有改写成某个“标准”同义词而生成 warning。
- 当前 demo 运行时不支持同一环节多人并行/会签审批：“结束本人处理”“本人处理完成”“结束本部门处理”这类同环节多人并行/会签后的运行控制动作，不要作为必须补齐的 submit_path blocking。
- 当前 demo 运行时不支持并行分支：“同时送”“同时提交多个环节”“并行审批”“并行分支”需要运行时支持并行任务，未实现时不要因为候选流程没有完整表达并行分支而 blocking，可作为 warning 或 user_clarification_requests。
- 如果已提供的人工审定边界（demo 边界说明）指出某线下动作、并行动作或多人会签动作当前不纳入 deterministic 流程，则不要把它作为 extraction repair blocking，也不要要求 extraction agent 强行新增对应节点或路径。

用户确认项：
- 如果 source material 本身没写、写法冲突或需要业务口径确认，写入 user_clarification_requests。
- 如果 source material 明确出现“待确认”“暂未配置”“上线前确认”“是否允许”等未决口径，即使候选流程已经可运行，也必须写入 user_clarification_requests，不要只写入 raw_missing_items 或 warning_issues。
- 处理期限/SLA、端侧入口权限（移动端/PC端/退回后编辑）、附件配置、加签/增加处理人动作，只要 source 中标注未决，就必须生成用户确认项。
- 如果 raw_missing_items 中包含 warning 级的处理期限、端侧入口权限、附件配置或加签动作，也必须同步生成对应 user_clarification_requests。
- extraction agent 重试修复后，不要丢弃 source material 中仍然存在的未决口径；候选流程可运行不代表这些确认项消失。
- 每个确认项用问题形式表达，并给 2-3 个建议选项；允许用户自由输入。
- 同一个确认项的 options 必须互斥且不能重复；不要把同一个建议换句话写成两个选项。
- options 只放用户可选择的业务口径，不要放“手动输入”“按建议处理”“请确认”等泛化选项。
- 如果问题可以从 source material 明确修复，不要写成用户确认项，应写入 repair_instructions。

JSON 输出硬约束：
- 输出必须符合 BusinessValidationResult schema。
- 所有字符串字段必须简短，不要直接摘录原文长句。
- 不要在任何字符串字段中使用英文双引号字符；需要引用词语时使用中文引号或直接省略引号。
- source_hint 只写 source 文件名和 chunk id，例如 05_会议纪要_0428.txt / src_005_c001，不要摘录原文。
- repair_instructions 每条只写一个短句，不要包含 JSON、引号或长原文。""".format(
        conventions_section=conventions_section
    )
    if include_schema:
        schema = json.dumps(BusinessValidationResult.model_json_schema(), ensure_ascii=False, indent=2)
        prompt += f"\n\nBusinessValidationResult JSON Schema:\n{schema}"
    return prompt


def _user_prompt(
    *,
    source_context: str,
    candidate_json: str,
    review_context: dict[str, Any] | None = None,
) -> str:
    review_block = ""
    if review_context:
        review_block = (
            "\n\n人工审定的当前 demo 边界/可接受可选项：\n"
            f"{json.dumps(review_context, ensure_ascii=False, indent=2)}\n\n"
            "使用方式：只用于避免把当前 demo 明确不支持或已接受排除的高级运行能力误判为 blocking；"
            "不要把它当作标准流程答案，也不要据此补造 source material 中没有的字段、环节或路径。\n"
        )
    return f"""请审核下面的候选 ProcessDefinition。

source material:
{source_context}

candidate_process:
{candidate_json}
{review_block}
"""


def _load_business_review_context(case_dir: str | None) -> dict[str, Any] | None:
    if not case_dir:
        return None
    try:
        standard_path = find_standard_json(case_dir)
        target = load_standard_target(standard_path)
    except Exception:
        return None
    review_metadata = target.get("review_metadata") or {}
    if review_metadata.get("review_status") not in {"human_reviewed", "locked"}:
        return None
    accepted = target.get("accepted_optional_clarifications") or []
    forbidden = target.get("forbidden_clarifications") or []
    notes = review_metadata.get("review_notes") or ""
    if not accepted and not forbidden and not notes:
        return None
    return {
        "review_status": review_metadata.get("review_status"),
        "review_notes": notes,
        "accepted_optional_clarifications": accepted,
        "forbidden_clarifications": forbidden,
    }


def _apply_business_review_context(
    result: BusinessValidationResult,
    review_context: dict[str, Any] | None,
) -> BusinessValidationResult:
    if not review_context:
        return result
    keywords = _accepted_optional_keywords(review_context)
    if not keywords:
        return result

    remaining_blocking: list[BusinessValidationIssue] = []
    demoted_warnings: list[BusinessValidationIssue] = []
    for issue in result.blocking_issues:
        if _issue_matches_keywords(issue, keywords):
            demoted = issue.model_copy(
                update={
                    "severity": "warning",
                    "evidence_status": "uncertain",
                    "message": f"{issue.message}；该项命中人工审定的当前 demo 可选/排除边界，已降级为 warning。",
                    "repair_instruction": None,
                }
            )
            demoted_warnings.append(demoted)
        else:
            remaining_blocking.append(issue)

    if not demoted_warnings:
        return result

    raw_missing = [
        issue for issue in result.raw_missing_items if not _issue_matches_keywords(issue, keywords)
    ]
    repair_instructions = [
        item for item in result.repair_instructions if not _text_matches_keywords(item, keywords)
    ]
    summary_suffix = f" 已根据人工审定 demo 边界降级 {len(demoted_warnings)} 个特殊运行控制项。"
    return result.model_copy(
        update={
            "passed": not remaining_blocking,
            "blocking_issues": remaining_blocking,
            "warning_issues": [*result.warning_issues, *demoted_warnings],
            "raw_missing_items": raw_missing,
            "repair_instructions": repair_instructions,
            "summary": (result.summary or "").rstrip() + summary_suffix,
        }
    )


def _accepted_optional_keywords(review_context: dict[str, Any]) -> list[str]:
    text = json.dumps(
        {
            "accepted_optional_clarifications": review_context.get("accepted_optional_clarifications") or [],
            "review_notes": review_context.get("review_notes") or "",
        },
        ensure_ascii=False,
    )
    candidate_keywords = [
        "结束本人处理",
        "本人处理完成",
        "结束本部门处理",
        "同时送",
        "同时提交",
        "并行审批",
        "并行分支",
        "多人并行",
        "多人审批",
        "会签",
    ]
    return [keyword for keyword in candidate_keywords if keyword in text]


def _issue_matches_keywords(issue: BusinessValidationIssue, keywords: list[str]) -> bool:
    return _text_matches_keywords(
        " ".join(
            str(value or "")
            for value in (
                issue.issue_type,
                issue.item,
                issue.message,
                issue.repair_instruction,
                issue.source_hint,
            )
        ),
        keywords,
    )


def _text_matches_keywords(text: str, keywords: list[str]) -> bool:
    return any(keyword and keyword in text for keyword in keywords)
