from __future__ import annotations

import hashlib

from app.rag.chunking import split_markdown
from app.rag.index import KnowledgeIndex, build_index


class FakeEmbeddings:
    """确定性假嵌入（不打 Bedrock）：字符哈希到定长向量。只为测通链路，不测语义。"""

    dim = 48

    def _vec(self, text: str) -> list[float]:
        v = [0.0] * self.dim
        for ch in text:
            v[ord(ch) % self.dim] += 1.0
        norm = sum(x * x for x in v) ** 0.5 or 1.0
        return [x / norm for x in v]

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return [self._vec(t) for t in texts]

    def embed_query(self, text: str) -> list[float]:
        return self._vec(text)


def test_split_markdown_by_headings_with_clause() -> None:
    body = "# 标题\n\n## 一、甲\n\n甲的内容。\n\n## 二、乙\n\n乙的内容第一段。\n\n乙的内容第二段。"
    chunks = split_markdown(body)
    clauses = {c.clause for c in chunks}
    assert "一、甲" in clauses and "二、乙" in clauses
    assert any("甲的内容" in c.text for c in chunks)
    # 一级标题不单独成块
    assert all("标题" not in c.text or "一、" in c.clause or "二、" in c.clause for c in chunks)


def test_split_markdown_splits_overlong_section() -> None:
    long_body = "## 大节\n\n" + "\n\n".join(f"第{i}段内容。" * 20 for i in range(6))
    chunks = split_markdown(long_body, max_chars=200)
    assert len(chunks) >= 2
    assert all(c.clause == "大节" for c in chunks)


def test_build_index_indexes_rules_and_chunks(tmp_path) -> None:
    count = build_index(tmp_path, embeddings=FakeEmbeddings())
    # 15 条规则 + 若干文档块
    assert count > 15


def test_semantic_search_returns_hits_with_metadata(tmp_path) -> None:
    build_index(tmp_path, embeddings=FakeEmbeddings())
    idx = KnowledgeIndex(tmp_path, embeddings=FakeEmbeddings())
    # 关阈值：假嵌入的距离语义无意义，这里只验证元数据装配（阈值行为另有专测）
    hits = idx.semantic_search("病假 请假 材料", k=5, max_distance=None)
    assert hits
    assert all(h.kind in {"rule", "chunk"} for h in hits)
    # rule 命中带 rule_id，chunk 命中带 doc_id
    for h in hits:
        if h.kind == "rule":
            assert h.metadata.get("rule_id")
        else:
            assert h.metadata.get("doc_id")


def test_semantic_search_filter_by_kind(tmp_path) -> None:
    build_index(tmp_path, embeddings=FakeEmbeddings())
    idx = KnowledgeIndex(tmp_path, embeddings=FakeEmbeddings())
    rule_hits = idx.semantic_search("审批", k=6, kind="rule", max_distance=None)
    assert rule_hits and all(h.kind == "rule" for h in rule_hits)


def test_max_distance_threshold_yields_honest_empty(tmp_path) -> None:
    """相关性阈值：距离超过阈值的召回被丢弃；全部超阈值则返回空，而不是硬凑 top-k。
    （真实距离语义在真嵌入下才准，这里只机械验证阈值参数生效。）"""
    build_index(tmp_path, embeddings=FakeEmbeddings())
    idx = KnowledgeIndex(tmp_path, embeddings=FakeEmbeddings())
    query = "完全不相关的查询 zzz 午饭"
    # 关阈值 → 拿到 top-k
    assert idx.semantic_search(query, k=4, max_distance=None)
    # 阈值卡到 0 → 非完全相同文本距离都 >0 → 诚实空
    assert idx.semantic_search(query, k=4, max_distance=0.0) == []


def test_retrieve_combines_structured_prefilter_and_semantic(tmp_path) -> None:
    build_index(tmp_path, embeddings=FakeEmbeddings())
    idx = KnowledgeIndex(tmp_path, embeddings=FakeEmbeddings())
    result = idx.retrieve("长假审批", process_id="LEAVE-001", domains={"leave"}, k=4, max_distance=None)
    applicable_ids = {r.rule_id for r in result.applicable_rules}
    # 结构化预筛确定性地把该管请假的规则都带出来（不靠向量运气）
    assert "leave.sick_leave_certificate" in applicable_ids
    assert "leave.line_leader_for_long_or_special" in applicable_ids
    assert result.semantic_hits  # 语义召回也有结果
