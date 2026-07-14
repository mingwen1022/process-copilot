from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

from dotenv import load_dotenv


@dataclass(frozen=True, slots=True)
class BedrockChatModelConfig:
    model_id: str
    region_name: str
    bedrock_api_key: str
    temperature: float
    max_tokens: int


def load_bedrock_config() -> BedrockChatModelConfig:
    load_dotenv()

    model_id = (os.getenv("BEDROCK_MODEL_ID") or "").strip().removeprefix("amazon-bedrock/")
    region_name = (os.getenv("AWS_REGION") or os.getenv("AWS_DEFAULT_REGION") or "").strip()
    bedrock_api_key = (os.getenv("AWS_BEARER_TOKEN_BEDROCK") or "").strip()
    max_tokens = int(os.getenv("BEDROCK_MAX_TOKENS", "16000"))
    temperature = float(os.getenv("BEDROCK_TEMPERATURE", "0"))

    if not model_id:
        raise ValueError("BEDROCK_MODEL_ID is required")
    if not region_name:
        raise ValueError("AWS_REGION or AWS_DEFAULT_REGION is required")
    if not bedrock_api_key:
        raise ValueError("AWS_BEARER_TOKEN_BEDROCK is required")

    return BedrockChatModelConfig(
        model_id=model_id,
        region_name=region_name,
        bedrock_api_key=bedrock_api_key,
        temperature=temperature,
        max_tokens=max_tokens,
    )


def create_bedrock_chat_model(**overrides: Any) -> Any:
    """Create the LangChain AWS Bedrock Converse ChatModel.

    Authentication uses the single Bedrock API key from AWS_BEARER_TOKEN_BEDROCK.
    This returns a real LangChain BaseChatModel, so LangChain agents can emit
    message history, tool calls, and tool result messages.
    """
    config = load_bedrock_config()

    try:
        from langchain_aws import ChatBedrockConverse
    except ImportError as exc:  # pragma: no cover - dependency is declared in pyproject
        raise RuntimeError("langchain-aws is required for Bedrock ChatBedrockConverse") from exc

    kwargs: dict[str, Any] = {
        "model": config.model_id,
        "region_name": config.region_name,
        "bedrock_api_key": config.bedrock_api_key,
        "temperature": config.temperature,
        "max_tokens": config.max_tokens,
    }
    kwargs.update(overrides)
    return ChatBedrockConverse(**kwargs)
