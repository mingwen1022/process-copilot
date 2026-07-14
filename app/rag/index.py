"""知识库向量索引 + 混合检索（模块3 · Phase 2）。

往 Chroma 索引两类文档：
- 原子规则（嵌 statement，元数据带 rule_id/dimension/check_type/applies_to/出处）
- 文档块（切知识 md 正文，元数据带 doc_id/dimension/clause）

检索是**混合**的：
- 结构化预筛（rules_for_process，按 applies_to 确定性命中该管的规则，不会漏）
- 语义召回（Chroma 相似度，管模糊问题 + 定性判断的上下文）
结构化那层是主、确定；向量是辅、管模糊——守住确定性纪律。

嵌入函数可注入，测试用假嵌入、不打 Bedrock。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.org_knowledge import parse_knowledge_documents
from app.rag.chunking import split_markdown
from app.rag.embeddings import create_bedrock_embeddings
from app.rag.rules import KNOWLEDGE_ROOT, load_atomic_rules, rules_for_process
from data.schema import AtomicRule

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_INDEX_DIR = PROJECT_ROOT / "data/rag_index"
COLLECTION = "process_knowledge"
# 相关性距离阈值：Chroma 距离越小越相关；实测相关 <0.7、无关 >1.5，取 1.0 兜底。
DEFAULT_MAX_DISTANCE = 1.0


@dataclass
class SemanticHit:
    kind: str  # rule | chunk
    text: str
    metadata: dict[str, Any]
    score: float


@dataclass
class RetrievalResult:
    applicable_rules: list[AtomicRule]  # 结构化预筛（确定性）
    semantic_hits: list[SemanticHit]  # 向量语义召回


def _rule_documents(rules: list[AtomicRule]):
    from langchain_core.documents import Document

    docs = []
    for rule in rules:
        docs.append(
            Document(
                page_content=rule.statement,
                metadata={
                    "kind": "rule",
                    "rule_id": rule.rule_id,
                    "title": rule.title,
                    "dimension": rule.dimension.value,
                    "check_type": rule.check_type,
                    "processes": ",".join(rule.applies_to_processes),
                    "domains": ",".join(rule.applies_to_domains),
                    "provenance_doc": rule.provenance.source_doc,
                    "clause": rule.provenance.clause,
                    "severity": rule.severity,
                },
            )
        )
    return docs


def _chunk_documents(knowledge_root: str | Path):
    from langchain_core.documents import Document

    docs = []
    for doc in parse_knowledge_documents(knowledge_root):
        meta = doc["metadata"]
        for chunk in split_markdown(doc["body"]):
            docs.append(
                Document(
                    page_content=chunk.text,
                    metadata={
                        "kind": "chunk",
                        "doc_id": meta.get("doc_id", ""),
                        "doc_title": meta.get("title", ""),
                        "dimension": meta.get("dimension", ""),
                        "clause": chunk.clause,
                    },
                )
            )
    return docs


def build_index(
    persist_dir: str | Path = DEFAULT_INDEX_DIR,
    *,
    embeddings: Any | None = None,
    knowledge_root: str | Path = KNOWLEDGE_ROOT,
) -> int:
    """建库（会真调嵌入）：清空并重建 collection。返回索引的文档数。"""
    from langchain_chroma import Chroma

    embeddings = embeddings or create_bedrock_embeddings()
    persist_dir = Path(persist_dir)
    persist_dir.mkdir(parents=True, exist_ok=True)

    store = Chroma(collection_name=COLLECTION, embedding_function=embeddings, persist_directory=str(persist_dir))
    store.reset_collection()

    rules = load_atomic_rules()
    documents = _rule_documents(rules) + _chunk_documents(knowledge_root)
    store.add_documents(documents)
    return len(documents)


class KnowledgeIndex:
    def __init__(self, persist_dir: str | Path = DEFAULT_INDEX_DIR, *, embeddings: Any | None = None):
        from langchain_chroma import Chroma

        self.embeddings = embeddings or create_bedrock_embeddings()
        self.store = Chroma(
            collection_name=COLLECTION,
            embedding_function=self.embeddings,
            persist_directory=str(persist_dir),
        )
        self.rules = load_atomic_rules()

    def semantic_search(
        self, query: str, *, k: int = 6, kind: str | None = None, max_distance: float | None = DEFAULT_MAX_DISTANCE
    ) -> list[SemanticHit]:
        """向量语义召回。score 是距离（越小越相关）。max_distance 是相关性阈值：
        超过它的召回一律丢弃——实测相关的 <0.7、无关的 >1.5，阈值默认 1.0 能把无关
        项切掉。若全部超阈值则返回空（诚实空），避免"没相关知识还硬凑几条"。
        max_distance=None 关闭阈值（拿原始 top-k）。"""
        where = {"kind": kind} if kind else None
        pairs = self.store.similarity_search_with_score(query, k=k, filter=where)
        hits: list[SemanticHit] = []
        for doc, score in pairs:
            if max_distance is not None and float(score) > max_distance:
                continue
            hits.append(SemanticHit(kind=doc.metadata.get("kind", ""), text=doc.page_content, metadata=doc.metadata, score=float(score)))
        return hits

    def retrieve(
        self,
        query: str,
        *,
        process_id: str | None = None,
        domains: set[str] | None = None,
        k: int = 6,
        max_distance: float | None = DEFAULT_MAX_DISTANCE,
    ) -> RetrievalResult:
        applicable = rules_for_process(self.rules, process_id, domains) if process_id or domains else []
        semantic = self.semantic_search(query, k=k, max_distance=max_distance)
        return RetrievalResult(applicable_rules=applicable, semantic_hits=semantic)
