from __future__ import annotations

from typing import Any, Protocol


class TextGenerator(Protocol):
    def generate(self, system_prompt: str, user_prompt: str) -> str:
        """Return raw text for a two-message prompt."""


def generate_text(model_or_provider: Any, system_prompt: str, user_prompt: str) -> str:
    """Generate text from either the legacy test provider protocol or a LangChain chat model."""
    try:
        from langchain_core.messages import HumanMessage, SystemMessage
    except ImportError as exc:  # pragma: no cover - dependencies are declared in pyproject
        raise RuntimeError("langchain-core is required to invoke chat models") from exc

    if hasattr(model_or_provider, "invoke"):
        response = model_or_provider.invoke([SystemMessage(content=system_prompt), HumanMessage(content=user_prompt)])
        return message_content_to_text(getattr(response, "content", response))

    if hasattr(model_or_provider, "generate"):
        return model_or_provider.generate(system_prompt, user_prompt)

    raise TypeError("model_or_provider must be a LangChain chat model or a legacy text generator")


def is_legacy_text_generator(model_or_provider: Any) -> bool:
    return hasattr(model_or_provider, "generate") and not hasattr(model_or_provider, "invoke")


def message_content_to_text(content: Any) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                if isinstance(item.get("text"), str):
                    parts.append(item["text"])
                elif isinstance(item.get("content"), str):
                    parts.append(item["content"])
        return "\n".join(parts)
    return str(content)
