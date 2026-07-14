from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.api.knowledge_service import KnowledgeService
from app.api.server import create_app


class _StubHit:
    def __init__(self, kind, metadata, text, score):
        self.kind = kind
        self.metadata = metadata
        self.text = text
        self.score = score


class _StubIndex:
    def __init__(self, hits):
        self._hits = hits
        self.seen_query = None

    def semantic_search(self, query, *, k=6):
        self.seen_query = query
        return self._hits


def test_rules_overview_is_deterministic_and_grouped() -> None:
    # 不注入索引 —— rules_overview 只读原子规则，不打 Bedrock
    overview = KnowledgeService().rules_overview()
    assert overview["total"] == overview["deterministic_count"] + overview["llm_judge_count"]
    assert overview["total"] > 0
    assert overview["deterministic_count"] > 0
    # 每组都有可读中文标签和至少一条规则
    for group in overview["groups"]:
        assert group["label"]
        assert group["rules"]
        first = group["rules"][0]
        assert first["rule_id"] and first["statement"]
        assert first["title"]  # 中文标题，不是只有机器 id
        assert first["source_doc"]  # 有据可溯


def test_search_returns_hits_from_injected_index() -> None:
    hit = _StubHit(
        kind="rule",
        metadata={"rule_id": "leave.sick_leave_certificate"},
        text="病假且请假天数大于等于3天的，必须上传就诊证明。",
        score=0.671234,
    )
    index = _StubIndex([hit])
    result = KnowledgeService(knowledge_index=index).search("病假需要证明吗")
    assert index.seen_query == "病假需要证明吗"
    assert len(result["hits"]) == 1
    got = result["hits"][0]
    assert got["kind"] == "rule"
    assert got["source"] == "leave.sick_leave_certificate"
    assert got["score"] == 0.671  # 四舍五入到三位


def test_search_empty_query_short_circuits_without_index() -> None:
    # 空查询不应触发索引（惰性构造会打 Bedrock）
    result = KnowledgeService().search("   ")
    assert result == {"query": "", "hits": []}


def test_search_honest_empty_when_no_relevant_hits() -> None:
    result = KnowledgeService(knowledge_index=_StubIndex([])).search("今天午餐吃什么")
    assert result["hits"] == []
    assert "unavailable" not in result  # 索引可用、只是无相关结果


def test_search_degrades_gracefully_when_index_raises() -> None:
    class _Boom:
        def semantic_search(self, query, *, k=6):
            raise RuntimeError("bedrock down")

    result = KnowledgeService(knowledge_index=_Boom()).search("请假")
    assert result["hits"] == []
    assert result["unavailable"] is True


def test_knowledge_rules_endpoint() -> None:
    client = TestClient(create_app(knowledge_service=KnowledgeService()))
    response = client.get("/api/v1/knowledge/rules")
    assert response.status_code == 200
    body = response.json()
    assert body["total"] > 0
    assert body["groups"]


def test_knowledge_search_endpoint_uses_injected_index() -> None:
    hit = _StubHit(
        kind="chunk",
        metadata={"doc_id": "leave_management_policy", "clause": "三、材料要求"},
        text="病假需附就诊证明。",
        score=0.6,
    )
    service = KnowledgeService(knowledge_index=_StubIndex([hit]))
    client = TestClient(create_app(knowledge_service=service))
    response = client.post("/api/v1/knowledge/search", json={"message": "病假证明", "history": []})
    assert response.status_code == 200
    body = response.json()
    assert body["query"] == "病假证明"
    assert body["hits"][0]["source"] == "leave_management_policy/三、材料要求"
    assert body["hits"][0]["doc_id"] == "leave_management_policy"


def test_documents_overview_lists_all_docs_not_just_rule_referenced() -> None:
    overview = KnowledgeService().documents_overview()
    all_doc_ids = {doc["doc_id"] for group in overview["groups"] for doc in group["docs"]}
    assert overview["total"] == len(all_doc_ids)
    # 一份从未被任何原子规则引用的文档也应出现在列表里（否则页面上永远看不到它）
    assert "finance_expense_policy" in all_doc_ids
    for group in overview["groups"]:
        assert group["label"]
        assert group["docs"]
        assert group["docs"][0]["title"]


def test_knowledge_documents_endpoint() -> None:
    client = TestClient(create_app(knowledge_service=KnowledgeService()))
    response = client.get("/api/v1/knowledge/documents")
    assert response.status_code == 200
    body = response.json()
    assert body["total"] > 0
    assert body["groups"]


def test_get_document_returns_body_for_known_doc_id() -> None:
    result = KnowledgeService().get_document("leave_management_policy")
    assert result["doc_id"] == "leave_management_policy"
    assert result["title"]
    assert "请假" in result["body"]


def test_get_document_raises_for_unknown_doc_id() -> None:
    with pytest.raises(KeyError):
        KnowledgeService().get_document("does_not_exist")


def test_knowledge_document_endpoint_returns_200_for_known_doc() -> None:
    client = TestClient(create_app(knowledge_service=KnowledgeService()))
    response = client.get("/api/v1/knowledge/documents/leave_management_policy")
    assert response.status_code == 200
    assert response.json()["doc_id"] == "leave_management_policy"


def test_knowledge_document_endpoint_returns_404_for_unknown_doc() -> None:
    client = TestClient(create_app(knowledge_service=KnowledgeService()))
    response = client.get("/api/v1/knowledge/documents/does_not_exist")
    assert response.status_code == 404
