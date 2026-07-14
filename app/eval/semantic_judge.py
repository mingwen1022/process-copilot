"""设计评测 · 语义裁判层（可选，LLM-as-judge）。

确定性评测是**字面比对**（normalize 标点后精确匹配），"自动带出"vs"系统带出"这类
同义改写会被算成差异。本模块在确定性主分**之上**加一层可选的语义复评：把每处属性
差异交给 LLM 判"语义是否等价"，等价的（同义改写）可视为抽对，据此估算一个"语义校正分"。

设计纪律：这是**叠加层，不改确定性主分**。主分永远是确定性的（可复现、可审计）；
语义复评是显式的、按需触发的二次视图，判断权交给 LLM 但明确标注为"估算"。
"""

from __future__ import annotations

from typing import Any, Optional

from pydantic import BaseModel, Field

from app.models.bedrock import create_bedrock_chat_model
from app.models.text import is_legacy_text_generator


class DiffJudgment(BaseModel):
    id: int = Field(description="对应输入差异的序号 id")
    equivalent: bool = Field(description="模型抽取值与金标准值语义是否等价（措辞不同但意思一致=true）")
    reason: str = Field(default="", description="一句简短判断理由")


class SemanticJudgeResult(BaseModel):
    judgments: list[DiffJudgment]


_SYSTEM = (
    "你是流程定义抽取评测的语义裁判。给定若干处「模型抽取值」vs「金标准值」的差异，"
    "逐条判断两者**指向的信息是否一致**。判断从宽，只看实质信息，不抠字面：\n"
    "- 措辞/表达/详略不同但指同一件事 → 等价（equivalent=true）。例：「当前登录人」与「当前登录人姓名」"
    "指同一个值；「系统自动带出」与「自动带出」是同一机制的不同写法；一方更啰嗦但没多出新约束 → 都算等价。\n"
    "- 不要因为**写法风格、字段该放什么内容的格式偏好**（如「default_value 不该带动作描述」）就判不等价——"
    "那是格式规范，不是语义。只要两者说的是同一件事，就等价。\n"
    "- 只有真的**信息不同**才判不等价：模型漏了金标准有的关键信息、加了金标准没有的约束/条件、"
    "数值不同、逻辑/流向不同。\n"
    "对每条按其 id 返回 equivalent 与一句简短 reason。只做语义判断，不改写、不臆造。拿不准时倾向等价。"
)


def flatten_attribute_diffs(
    comparison: dict[str, Any], *, meta_differences: list[dict[str, Any]] | None = None
) -> list[dict[str, Any]]:
    """把 comparison_report 里的字段/环节/路径/附件属性差异 + meta 差异展平成带 dim 归属的
    差异列表（供裁判逐条判）。meta_differences 来自 deterministic_eval.details.meta.differences，
    不在 comparison_report 里，单独传入。"""
    diffs: list[dict[str, Any]] = []
    i = 0
    for d in meta_differences or []:
        diffs.append({"id": i, "dim": "meta", "location": "流程元信息",
                      "attribute": d.get("attribute"), "expected": d.get("expected"), "actual": d.get("actual")})
        i += 1
    for fd in comparison.get("field_differences", []):
        for d in fd.get("differences", []):
            diffs.append({"id": i, "dim": "form_fields", "location": f"字段「{fd.get('field_name')}」",
                          "attribute": d.get("attribute"), "expected": d.get("expected"), "actual": d.get("actual")})
            i += 1
    for nd in comparison.get("node_differences", []):
        for d in nd.get("differences", []):
            diffs.append({"id": i, "dim": "flow_nodes", "location": f"环节「{nd.get('node_id')}」",
                          "attribute": d.get("attribute"), "expected": d.get("expected"), "actual": d.get("actual")})
            i += 1
    for pd in comparison.get("path_condition_differences", []):
        diffs.append({"id": i, "dim": "submit_paths", "location": f"路径「{pd.get('path_name')}」",
                      "attribute": "condition", "expected": pd.get("expected_condition"), "actual": pd.get("actual_condition")})
        i += 1
    for ad in comparison.get("attachment_differences", []):
        for d in ad.get("differences", []):
            diffs.append({"id": i, "dim": "attachments", "location": f"附件「{ad.get('attachment_type')}」",
                          "attribute": d.get("attribute"), "expected": d.get("expected"), "actual": d.get("actual")})
            i += 1
    return diffs


def _user_prompt(diffs: list[dict[str, Any]]) -> str:
    lines = ["以下每条是一处差异，请逐条判断语义是否等价：\n"]
    for d in diffs:
        lines.append(
            f"[id={d['id']}] 位置：{d.get('location','')} · 属性：{d.get('attribute','')}\n"
            f"  金标准：{d.get('expected')!r}\n"
            f"  模型抽取：{d.get('actual')!r}"
        )
    return "\n".join(lines)


# 单批次差异条数上限。实测：EOA140 一次性发 33 条时，Bedrock 结构化输出偶发把 judgments
# 数组整体序列化成一个 JSON 字符串塞进 tool_call 参数（而不是真正的数组），Pydantic 校验失败，
# 导致整批全部判不可用——分批发送降低这种"大数组被拍扁"的概率，且一批失败不拖累其它批次。
_BATCH_SIZE = 8
_BATCH_RETRIES = 2


def judge_diffs(diffs: list[dict[str, Any]], model: Optional[Any] = None) -> Optional[dict[int, DiffJudgment]]:
    """对差异列表做语义裁判，返回 {id: DiffJudgment}；分批调用，某批持续失败时该批的 id
    就不会出现在返回字典里（调用方对缺失 id 默认按"不等价"处理，不会误判等价）。
    仅当所有批次都失败（如模型整体不可用）才返回 None。"""
    if not diffs:
        return {}
    model = model or create_bedrock_chat_model()
    if is_legacy_text_generator(model):
        return None
    structured = model.with_structured_output(SemanticJudgeResult, method="function_calling", include_raw=True)

    results: dict[int, DiffJudgment] = {}
    any_batch_succeeded = False
    for start in range(0, len(diffs), _BATCH_SIZE):
        batch = diffs[start : start + _BATCH_SIZE]
        parsed = None
        for attempt in range(_BATCH_RETRIES + 1):
            response = structured.invoke(
                [
                    {"role": "system", "content": _SYSTEM},
                    {"role": "user", "content": _user_prompt(batch)},
                ]
            )
            parsed = _coerce(response)
            if parsed is not None:
                break
        if parsed is None:
            continue  # 这一批多次尝试后仍解析失败，跳过；其它批次照常
        any_batch_succeeded = True
        for j in parsed.judgments:
            results[j.id] = j
    return results if any_batch_succeeded else None


def _coerce(response: Any) -> Optional[SemanticJudgeResult]:
    parsed: Any = None
    parsing_error: Any = None
    if isinstance(response, dict):
        parsed = response.get("parsed")
        parsing_error = response.get("parsing_error")
    elif isinstance(response, SemanticJudgeResult):
        parsed = response
    else:
        parsed = getattr(response, "parsed", None)
        parsing_error = getattr(response, "parsing_error", None)
    if parsing_error or not isinstance(parsed, SemanticJudgeResult):
        return None
    return parsed
