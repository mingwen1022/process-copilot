from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from pydantic import BaseModel


def write_trace(payload: Any, output_path: str | Path) -> Path:
    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(to_jsonable(payload), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path


def serialize_agent_response(response: Any, *, input_messages: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    payload: dict[str, Any] = {}
    if input_messages is not None:
        payload["input_messages"] = input_messages

    if isinstance(response, dict):
        messages = response.get("messages")
        if messages is not None:
            payload["messages"] = [serialize_message(message) for message in messages]
        for key, value in response.items():
            if key != "messages":
                payload[key] = to_jsonable(value)
        return payload

    payload["response"] = serialize_message(response)
    return payload


def serialize_llm_call(
    *,
    agent: str,
    step: str,
    system_prompt: str,
    user_prompt: str,
    assistant_output: str,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "agent": agent,
        "step": step,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
            {"role": "assistant", "content": assistant_output},
        ],
        "metadata": to_jsonable(metadata or {}),
    }


def serialize_message(message: Any) -> dict[str, Any]:
    if isinstance(message, dict):
        return to_jsonable(message)

    payload: dict[str, Any] = {
        "type": message.__class__.__name__,
        "content": to_jsonable(getattr(message, "content", None)),
    }
    for attr in ("id", "name", "type", "tool_call_id"):
        value = getattr(message, attr, None)
        if value is not None:
            payload[attr] = to_jsonable(value)
    for attr in ("tool_calls", "invalid_tool_calls", "additional_kwargs", "response_metadata", "usage_metadata"):
        value = getattr(message, attr, None)
        if value:
            payload[attr] = to_jsonable(value)
    return payload


def to_jsonable(value: Any) -> Any:
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        return {str(key): to_jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [to_jsonable(item) for item in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)
