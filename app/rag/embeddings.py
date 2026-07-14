"""Bedrock Titan 嵌入封装（模块3 · Phase 2）。

复用 app.models.bedrock 的同一套凭证/region（bearer token 由 botocore 自动读
AWS_BEARER_TOKEN_BEDROCK），只是把 chat model 换成 embeddings model。
"""

from __future__ import annotations

import os
from typing import Any

from dotenv import load_dotenv

DEFAULT_EMBED_MODEL = "amazon.titan-embed-text-v2:0"


def create_bedrock_embeddings(model_id: str = DEFAULT_EMBED_MODEL, **overrides: Any) -> Any:
    load_dotenv()
    region = (os.getenv("AWS_REGION") or os.getenv("AWS_DEFAULT_REGION") or "").strip()
    if not region:
        raise ValueError("AWS_REGION or AWS_DEFAULT_REGION is required for embeddings")
    try:
        from langchain_aws import BedrockEmbeddings
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError("langchain-aws is required for BedrockEmbeddings") from exc
    kwargs: dict[str, Any] = {"model_id": model_id, "region_name": region}
    kwargs.update(overrides)
    return BedrockEmbeddings(**kwargs)
