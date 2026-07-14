"""发起问答副驾（#4）编排：检索规则 → agent 推荐 → 确定性路径预演。纯只读。"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel

from app.copilots.launch_agent import LaunchCopilotAgent
from app.copilots.launch_catalog import build_catalog, preview_path


class LaunchReply(BaseModel):
    reply: str
    recommended_process_id: str | None = None
    recommended_process_name: str | None = None
    path_preview: list[str] = []  # 确定性算出的会经过的环节；空=无法预演


class LaunchCopilotService:
    def __init__(self, agent: LaunchCopilotAgent | None = None, knowledge_index: Any | None = None) -> None:
        self._agent = agent
        self._knowledge_index = knowledge_index
        self._catalog = build_catalog()

    def catalog_view(self) -> dict[str, Any]:
        return {"items": [{"process_id": c.process_id, "name": c.name, "description": c.description} for c in self._catalog]}

    def ask(self, user_message: str) -> LaunchReply:
        clean = (user_message or "").strip()
        if not clean:
            return LaunchReply(reply="请描述你的情况，例如：我要请8天病假该走哪个流程、会经过哪些环节。")
        rules = self._retrieve_rules(clean)
        answer = self._get_agent().answer(self._catalog, rules, clean)
        if answer is None:
            return LaunchReply(reply="发起助手暂不可用（模型未就绪）。")

        recommended = next((c for c in self._catalog if c.process_id == answer.recommended_process_id), None)
        path: list[str] = []
        if recommended and recommended.definition is not None and answer.leave_days is not None:
            attrs: dict[str, Any] = {"请假天数": answer.leave_days}
            if answer.leave_type:
                attrs["请假类型"] = answer.leave_type
            path = preview_path(recommended.definition, attrs)
        return LaunchReply(
            reply=answer.reply,
            recommended_process_id=answer.recommended_process_id,
            recommended_process_name=(recommended.name if recommended else None),
            path_preview=path,
        )

    def _retrieve_rules(self, query: str) -> str:
        index = self._get_index()
        if index is None:
            return ""
        try:
            hits = index.semantic_search(query, k=4)
        except Exception:  # noqa: BLE001
            return ""
        clauses = [h.text for h in hits if getattr(h, "kind", "") == "chunk"]
        return "\n\n".join(clauses)

    def _get_agent(self) -> LaunchCopilotAgent:
        if self._agent is None:
            self._agent = LaunchCopilotAgent()
        return self._agent

    def _get_index(self) -> Any | None:
        if self._knowledge_index is None:
            try:
                from app.rag.index import KnowledgeIndex

                self._knowledge_index = KnowledgeIndex()
            except Exception:  # noqa: BLE001
                return None
        return self._knowledge_index
