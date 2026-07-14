from __future__ import annotations

import json
import re
from typing import Any

DRAFT_CANONICAL = "__draft__"
END_CANONICAL = "__end__"


def normalize_text(value: Any) -> str:
    if value is None:
        return ""
    text = str(value).strip().lower()
    if text in {"", "-", "null", "none", "[]"}:
        return ""
    text = text.replace("＞", ">").replace("＜", "<").replace("≥", ">=").replace("≤", "<=")
    text = text.replace("＝", "=")
    text = text.replace("返回起草", "退回起草")
    text = text.replace("结束流程", "流程结束")
    text = text.replace("审批完成", "流程结束")
    text = re.sub(r"(\d+)\s*万", lambda m: str(int(m.group(1)) * 10000) + "元", text)
    text = re.sub(r"(超过|大于)(\d+)天", r">\2天", text)
    text = re.sub(r"(不超过|小于等于|少于等于)(\d+)天", r"<=\2天", text)
    text = re.sub(r"(\d+)天(以内|内)", r"<=\1天", text)
    text = re.sub(r"\s+", "", text)
    return re.sub(r"[，,。；;：:、（）()「」『』【】\[\]\"'“”‘’_\-]", "", text)


def normalize_key(value: Any) -> str:
    return normalize_text(value)


def normalize_condition(value: Any) -> str:
    text = normalize_text(value)
    replacements = {
        "结论性意见=同意": "同意",
        "结论性意见为同意": "同意",
        "结论性意见同意": "同意",
        "结论性意见=不同意": "不同意",
        "结论性意见为不同意": "不同意",
        "结论性意见不同意": "不同意",
        "并且": "",
        "且": "",
    }
    for source, target in replacements.items():
        text = text.replace(normalize_text(source), normalize_text(target))
    return text


def canonical_node_token(node_id: Any = None, node_name: Any = None) -> str:
    values = [normalize_text(node_id), normalize_text(node_name)]
    if any(value in {"draft", "起草", "退回起草"} for value in values):
        return DRAFT_CANONICAL
    if any(value in {"end", "流程结束"} for value in values):
        return END_CANONICAL
    return values[0] or values[1]


def flatten_text(payload: Any) -> str:
    if payload is None:
        return ""
    if isinstance(payload, str):
        return payload
    if isinstance(payload, (int, float, bool)):
        return str(payload)
    if isinstance(payload, list):
        return "\n".join(flatten_text(item) for item in payload)
    if isinstance(payload, dict):
        return "\n".join(flatten_text(value) for value in payload.values())
    return str(payload)


def flatten_clarification(card: dict[str, Any]) -> str:
    values = [
        card.get("id"),
        card.get("topic"),
        card.get("question"),
        card.get("expected_question"),
        card.get("reason"),
        card.get("item"),
        card.get("recommendation"),
        card.get("suggestion"),
        card.get("suggested_answer"),
        card.get("message"),
        card.get("issue_type"),
        card.get("source_hint"),
        card.get("source_hints"),
        card.get("evidence"),
        card.get("expected_question_contains"),
        card.get("must_contain"),
        json.dumps(card.get("options", card.get("choices", [])), ensure_ascii=False),
    ]
    return "\n".join(flatten_text(value) for value in values if value)


def clarification_topic(text_or_payload: Any) -> str | None:
    text = normalize_text(flatten_text(text_or_payload))
    if not text:
        return None
    if any(token in text for token in ["处理期限", "sla", "时限", "限时"]):
        return "sla_time_limit"
    if "附件" in text and any(token in text for token in ["材料", "证明", "必传", "上传", "补交"]):
        return "attachment_requirement"
    if "任务名称" in text and any(token in text for token in ["映射", "对口部门", "枚举", "战略发展部", "默认"]):
        return "task_name_mapping"
    if "合规" in text and any(token in text for token in ["风险管理委员会", "委员会", "可选分支", "主流程"]):
        return "compliance_committee_path"
    if "移动端" in text and any(token in text for token in ["起草", "发起", "申请"]):
        return "mobile_initiation"
    return None


def clarification_terms(target: dict[str, Any]) -> list[str]:
    explicit = target.get("expected_question_contains") or target.get("must_contain") or target.get("keywords")
    if isinstance(explicit, list):
        return [str(item).strip() for item in explicit if str(item).strip()]
    if isinstance(explicit, str) and explicit.strip():
        return [explicit.strip()]

    terms: list[str] = []
    for key in ["question", "expected_question", "target", "topic", "field", "id"]:
        value = target.get(key)
        if isinstance(value, str) and value.strip():
            terms.append(value.strip())
    return terms[:3]


def match_clarification(target: dict[str, Any], actual: dict[str, Any] | str) -> dict[str, Any] | None:
    target_text = flatten_clarification(target) if isinstance(target, dict) else str(target)
    actual_text = flatten_clarification(actual) if isinstance(actual, dict) else str(actual)
    target_topic = clarification_topic(target_text)
    actual_topic = clarification_topic(actual_text)
    if target_topic and target_topic == actual_topic:
        return {
            "match_basis": "clarification_topic",
            "matched_topic": target_topic,
            "matched_terms": [],
            "actual_text": actual_text,
        }

    terms = clarification_terms(target)
    normalized_actual = normalize_text(actual_text)
    matched_terms = [term for term in terms if normalize_text(term) and normalize_text(term) in normalized_actual]
    if terms and len(matched_terms) == len(terms):
        return {
            "match_basis": "clarification_terms_all",
            "matched_topic": None,
            "matched_terms": matched_terms,
            "actual_text": actual_text,
        }
    if len(terms) >= 3 and len(matched_terms) >= 2:
        return {
            "match_basis": "clarification_terms_partial",
            "matched_topic": None,
            "matched_terms": matched_terms,
            "actual_text": actual_text,
        }
    return None
