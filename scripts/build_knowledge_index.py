"""一次性建立流程知识库向量索引（模块3 · Phase 2，真调 Bedrock Titan 嵌入）。

把原子规则 + 文档块嵌入并持久化到 data/rag_index/（gitignore，可重建）。
用法：uv run python scripts/build_knowledge_index.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.rag.index import DEFAULT_INDEX_DIR, build_index


def main() -> None:
    count = build_index(DEFAULT_INDEX_DIR)
    print(f"已索引 {count} 个文档（原子规则 + 文档块）到 {DEFAULT_INDEX_DIR}")


if __name__ == "__main__":
    main()
