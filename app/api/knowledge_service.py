"""流程知识库只读服务（模块3 · Phase 6，点亮"制度与规则库"入口）。

浏览规则（确定性，load_atomic_rules，不打 Bedrock）+ 语义检索 demo（真实向量索引，
带相关性阈值：无相关结果诚实返回空，不硬凑）。
"""

from __future__ import annotations

from typing import Any

from app.rag.rules import load_atomic_rules

_DIMENSION_LABELS = {
    "company_policy": "公司制度/业务规则",
    "design_standard": "设计规范",
    "org_role": "组织角色知识",
    "process_playbook": "流程范例/最佳实践",
    "ops_baseline": "运营基准/指标口径",
    "ops_playbook": "运维处理规则",
}


class KnowledgeService:
    def __init__(self, knowledge_index: Any | None = None):
        self._knowledge_index = knowledge_index  # 可注入；缺省惰性构造（真嵌入）

    def rules_overview(self) -> dict[str, Any]:
        rules = load_atomic_rules()
        by_dim: dict[str, list[dict[str, Any]]] = {}
        for rule in rules:
            by_dim.setdefault(rule.dimension.value, []).append(
                {
                    "rule_id": rule.rule_id,
                    "title": rule.title,
                    "statement": rule.statement,
                    "check_type": rule.check_type,
                    "severity": rule.severity,
                    "applies_to_processes": rule.applies_to_processes,
                    "applies_to_domains": rule.applies_to_domains,
                    "source_doc": rule.provenance.source_doc,
                    "clause": rule.provenance.clause,
                }
            )
        groups = [
            {"dimension": dim, "label": _DIMENSION_LABELS.get(dim, dim), "rules": by_dim[dim]}
            for dim in _DIMENSION_LABELS
            if dim in by_dim
        ]
        deterministic = sum(1 for r in rules if r.check_type == "deterministic")
        return {
            "total": len(rules),
            "deterministic_count": deterministic,
            "llm_judge_count": len(rules) - deterministic,
            "groups": groups,
        }

    def search(self, query: str, *, k: int = 6) -> dict[str, Any]:
        clean = (query or "").strip()
        if not clean:
            return {"query": clean, "hits": []}
        index = self._get_index()
        if index is None:
            return {"query": clean, "hits": [], "unavailable": True}
        try:
            hits = index.semantic_search(clean, k=k)
        except Exception:  # noqa: BLE001 - 检索失败降级为空
            return {"query": clean, "hits": [], "unavailable": True}
        def _describe(h: Any) -> dict[str, Any]:
            if h.kind == "rule":
                title = h.metadata.get("title") or h.metadata.get("rule_id", "")
                source = h.metadata.get("rule_id", "")
                doc_id = h.metadata.get("provenance_doc", "")
            else:
                title = h.metadata.get("doc_title") or h.metadata.get("doc_id", "")
                clause = h.metadata.get("clause", "")
                doc_id = h.metadata.get("doc_id", "")
                source = f"{doc_id}/{clause}".strip("/")
            return {
                "kind": h.kind,
                "title": title,
                "source": source,
                "doc_id": doc_id,
                "text": h.text,
                "score": round(h.score, 3),
            }

        return {"query": clean, "hits": [_describe(h) for h in hits]}

    def documents_overview(self) -> dict[str, Any]:
        """全部知识文档（不限于被原子规则引用的那 4 份），按维度分组，供直接浏览原文。"""
        from app.org_knowledge import parse_knowledge_documents

        by_dim: dict[str, list[dict[str, Any]]] = {}
        total = 0
        for doc in parse_knowledge_documents():
            meta = doc["metadata"]
            doc_id = meta.get("doc_id")
            if not doc_id:
                continue
            total += 1
            dim = meta.get("dimension", "")
            by_dim.setdefault(dim, []).append(
                {
                    "doc_id": doc_id,
                    "title": meta.get("title") or doc_id,
                    "applies_to_processes": meta.get("applies_to_processes", []) or [],
                    "applies_to_domains": meta.get("applies_to_domains", []) or [],
                }
            )
        groups = [
            {"dimension": dim, "label": _DIMENSION_LABELS.get(dim, dim), "docs": by_dim[dim]}
            for dim in _DIMENSION_LABELS
            if dim in by_dim
        ]
        return {"total": total, "groups": groups}

    def get_document(self, doc_id: str) -> dict[str, Any]:
        from app.org_knowledge import parse_knowledge_documents

        for doc in parse_knowledge_documents():
            if doc["metadata"].get("doc_id") == doc_id:
                return {
                    "doc_id": doc_id,
                    "title": doc["metadata"].get("title") or doc_id,
                    "dimension": doc["metadata"].get("dimension", ""),
                    "body": doc["body"],
                }
        raise KeyError(f"未知的知识文档 doc_id: {doc_id}")

    def _get_index(self) -> Any | None:
        if self._knowledge_index is None:
            try:
                from app.rag.index import KnowledgeIndex

                self._knowledge_index = KnowledgeIndex()
            except Exception:  # noqa: BLE001
                return None
        return self._knowledge_index
