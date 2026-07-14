from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.models.bedrock import create_bedrock_chat_model
from app.models.text import generate_text


def main() -> int:
    parser = argparse.ArgumentParser(description="Smoke-test Bedrock LLM provider.")
    parser.add_argument("--prompt", default="请回复：Bedrock 连通成功")
    args = parser.parse_args()

    model = create_bedrock_chat_model()
    print(generate_text(model, "你是一个简洁的连通性测试助手。", args.prompt))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
