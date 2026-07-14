from __future__ import annotations

import sys
from types import SimpleNamespace

import pytest

from app.models.bedrock import create_bedrock_chat_model, load_bedrock_config


def test_bedrock_config_uses_bearer_api_key(monkeypatch) -> None:
    monkeypatch.setenv("BEDROCK_MODEL_ID", "amazon-bedrock/us.anthropic.claude-sonnet-4-6")
    monkeypatch.setenv("AWS_REGION", "us-east-1")
    monkeypatch.setenv("AWS_BEARER_TOKEN_BEDROCK", "token-123")
    monkeypatch.setenv("BEDROCK_MAX_TOKENS", "4096")
    monkeypatch.setenv("BEDROCK_TEMPERATURE", "0.2")

    config = load_bedrock_config()

    assert config.model_id == "us.anthropic.claude-sonnet-4-6"
    assert config.region_name == "us-east-1"
    assert config.bedrock_api_key == "token-123"
    assert config.max_tokens == 4096
    assert config.temperature == 0.2


def test_bedrock_factory_creates_langchain_chat_model(monkeypatch) -> None:
    captured: dict[str, object] = {}

    class FakeChatBedrockConverse:
        def __init__(self, **kwargs) -> None:
            captured.update(kwargs)

    monkeypatch.setitem(sys.modules, "langchain_aws", SimpleNamespace(ChatBedrockConverse=FakeChatBedrockConverse))
    monkeypatch.setenv("BEDROCK_MODEL_ID", "us.anthropic.claude-sonnet-4-6")
    monkeypatch.setenv("AWS_REGION", "us-east-1")
    monkeypatch.setenv("AWS_BEARER_TOKEN_BEDROCK", "token-123")
    monkeypatch.setenv("BEDROCK_MAX_TOKENS", "4096")
    monkeypatch.setenv("BEDROCK_TEMPERATURE", "0.2")

    model = create_bedrock_chat_model()

    assert isinstance(model, FakeChatBedrockConverse)
    assert captured == {
        "model": "us.anthropic.claude-sonnet-4-6",
        "region_name": "us-east-1",
        "bedrock_api_key": "token-123",
        "temperature": 0.2,
        "max_tokens": 4096,
    }


def test_bedrock_config_requires_env_values(monkeypatch) -> None:
    monkeypatch.setenv("BEDROCK_MODEL_ID", "")
    monkeypatch.setenv("AWS_REGION", "")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "")
    monkeypatch.setenv("AWS_BEARER_TOKEN_BEDROCK", "token-123")

    with pytest.raises(ValueError, match="BEDROCK_MODEL_ID is required"):
        load_bedrock_config()

    monkeypatch.setenv("BEDROCK_MODEL_ID", "us.anthropic.claude-sonnet-4-6")
    with pytest.raises(ValueError, match="AWS_REGION or AWS_DEFAULT_REGION is required"):
        load_bedrock_config()

    monkeypatch.setenv("AWS_REGION", "us-east-1")
    monkeypatch.setenv("AWS_BEARER_TOKEN_BEDROCK", " ")
    with pytest.raises(ValueError, match="AWS_BEARER_TOKEN_BEDROCK is required"):
        load_bedrock_config()
