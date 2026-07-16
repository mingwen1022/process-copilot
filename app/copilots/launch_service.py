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


# 发起向导被问"一般要走多久/平均时长/耗时"这类问题时，该答的是**运行时长数据**，
# 而它自己只有流程定义 + 规则、没有运行数据，硬答就会"答非所问"。这些关键词命中时，
# 转由已接入的效能分析给真实平均办结时长（进程内直接查，不再多绕一层 LLM）。
_DURATION_KEYWORDS = ("多久", "多长时间", "时长", "耗时", "几天能", "多少天能", "平均.*时间", "走完", "时效")


class LaunchCopilotService:
    def __init__(
        self,
        agent: LaunchCopilotAgent | None = None,
        knowledge_index: Any | None = None,
        analytics_by_process_id: dict[str, Any] | None = None,
    ) -> None:
        self._agent = agent
        self._knowledge_index = knowledge_index
        self._catalog = build_catalog()
        # 键 = 流程 process_id（LEAVE-001 等），与 launch 目录里的 process_id 同名，直接对得上
        self._analytics_by_process_id = analytics_by_process_id or {}

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
        reply = answer.reply
        # 时长类问题：发起向导没有运行数据、答不准，补上效能分析里的真实平均办结时长
        duration_note = self._duration_note(clean, recommended)
        if duration_note:
            reply = reply.rstrip() + "\n\n" + duration_note
        return LaunchReply(
            reply=reply,
            recommended_process_id=answer.recommended_process_id,
            recommended_process_name=(recommended.name if recommended else None),
            path_preview=path,
        )

    def _duration_note(self, message: str, recommended: Any) -> str:
        import re

        if not any(re.search(k, message) for k in _DURATION_KEYWORDS):
            return ""
        if recommended is None:
            return ""
        service = self._analytics_by_process_id.get(recommended.process_id)
        if service is None:
            return "（这条流程暂未接入运行数据，平均办结时长得看效能分析或问流程负责人。）"
        try:
            overview = (service.dashboard_metrics() or {}).get("overview") or {}
            hours = overview.get("avg_case_duration_hours")
        except Exception:  # noqa: BLE001 - 取不到就退化为不给数字，不报错
            hours = None
        if not hours:
            return "（这条流程暂无可用的运行时长统计。）"
        days = hours / 24
        return f"参考运行数据：**{recommended.name}** 近期正常办结的平均处理时长约 **{days:.1f} 天**（{hours:.0f} 小时）；具体因金额/材料/审批人响应而异。"

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
