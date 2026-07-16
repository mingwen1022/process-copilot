"""流程管理页副驾（流程总览）——路由 + 编排，不是又一个独立业务 agent。

流程总览 agent 只做一件事：判断用户这句话该转给哪个已有能力处理，然后**直接调用对应
的确定性/半确定性服务**——不是"两个 LLM 互相聊天"（那样谁也不知道对方到底确认了什么，
黑盒还难调）。四条路由：

- batch_fix：批量修复。分类到位后交给前端——前端本来就有每条流程的完整数据，复用
  单流程设计页已经跑通的「诊断→pending edit→确认」链路，在多条流程上循环跑一遍。
  这里只做识别 + 报个数，不在后端重新实现一遍编辑逻辑。
- flow_structure：问某条流程怎么设计的。用该流程真实的 ProcessDefinition 作答，
  提示词专门为"给流程 owner 解释结构"调——不是把问题原样转给发起向导的 LLM（发起
  向导的提示词是照"该走哪个流程"调的，人格对不上，会答非所问）。
- analytics：问效能数据。直接调用已有 AnalyticsService.answer_question()，原样透传
  它的回答——这里只是路由，不重新生成一遍分析结论，没接的流程如实说没有。
- general：范围外的问题（含运维类——运维 copilot 目前是按运行实例诊断的，没有流程级
  入口可转发，这版先不接），给一句范围说明，不编答案。
"""

from __future__ import annotations

from typing import Any, Literal, Optional

from pydantic import BaseModel, Field

from app.models.bedrock import create_bedrock_chat_model
from app.models.text import is_legacy_text_generator


class ManageRoute(BaseModel):
    route: Literal["catalog", "batch_fix", "flow_issues", "flow_structure", "analytics", "launch_guide", "general"] = Field(
        description="问题类型：catalog=问流程目录本身（有几条流程、都叫什么、哪些已上架/有待处理项——"
        "不是问某条流程的设计细节或效能，是问「清单」层面的事）；"
        "flow_issues=问**某一条具体流程**有没有待处理项 / 需不需要优化 / 有什么问题（针对单条，"
        "不是问全部、也不是问它整体怎么设计的、也不是纯问效能指标），要选出 target_workflow_id；"
        "batch_fix=要求处理/修复待处理事项（不针对单条流程，或要求处理全部）；"
        "flow_structure=以流程 owner/设计者视角，问某条具体流程「整体是怎么设计的」"
        "（有哪些字段/环节/路径、分支逻辑），是对流程定义的静态讲解；"
        "analytics=问运行效能/指标——可以是问某条具体流程的，也可以是跨流程/全局的汇总问题"
        "（如「总共有多少单在跑」「整体完成率怎么样」），后者不用选具体流程、target_workflow_id 留空；"
        "launch_guide=以「申请人/员工」视角问的事务性问题（owner 可能替员工转问）——"
        "如「该走哪个流程」「我这情况（给了具体天数/类型等取值）会经过哪些环节」「需要什么材料」"
        "「符不符合条件」，需要按具体取值预演路径或判断资格；"
        "general=其它，包括运维类问题（暂不支持转发）"
    )
    target_workflow_id: Optional[str] = Field(
        default=None,
        description="flow_structure/analytics 且问题针对某条具体流程时，从目录里选中问的是哪条流程的"
        " workflow_id（必须是目录里真实存在的 id）；跨流程/全局的 analytics 问题，或选不出具体是哪条，"
        "必须真正留空（不要填占位符或猜测的 id）",
    )
    general_reply: str = Field(
        default="", description="仅 route=general 时填写：一句话说明当前支持的问题范围，不编造能力"
    )


def _classify_system_prompt() -> str:
    return """你是企业 OA「流程管理」总览页的路由助手。用户是管理多条流程的流程 owner。owner 的
问题分两种视角：① 管理/设计视角——批量修复待处理项、某条流程整体是怎么设计的、某条流程运行
效能如何；② 员工/申请人视角（owner 常被员工问、于是替员工来问）——该走哪个流程、我这情况
（给了具体天数/类型）会经过哪些环节、需要什么材料、符不符合条件。也可能是范围外的问题（如
运维单据处理——目前没有流程级入口，归 general）。

判断关键：问「这条流程整体结构/分支逻辑」→ flow_structure；问「这条流程有没有待处理项/
要不要优化/有什么问题」→ flow_issues（针对单条流程列它的待处理项）；问「给了具体取值、我会
怎么流转/符不符合」→ launch_guide（它能按取值确定性预演路径）。注意区分：问「哪些流程有待
处理项」（清单层面）是 catalog；问「员工请假申请流程有没有需要处理的」（指名某一条）是 flow_issues。

给你一份流程目录（workflow_id/名称/是否有待处理项/是否支持效能问答）。若问题针对某条具体流程，
从目录里选出它的 workflow_id 填 target_workflow_id（必须是目录里存在的 id）；问题是跨流程/全局的
（如"总共多少单在跑"），或选不出具体是哪条，把 target_workflow_id 真正留空，不要填任何占位符
（如 "<UNKNOWN>"/"unknown"/"N/A" 之类）或瞎猜的 id——那样反而会被当成一个无效流程处理。
route=general 时用 general_reply 给一句范围说明，别编造你能做到的事。"""


def _render_history(history: list[dict[str, str]] | None) -> str:
    if not history:
        return ""
    lines = []
    for h in history[-6:]:  # 只带最近几轮，避免 prompt 无限增长
        role = "用户" if h.get("role") == "user" else "助手"
        content = (h.get("content") or "").strip()
        if content:
            lines.append(f"{role}：{content[:300]}")
    return "\n".join(lines)


def _classify_user_prompt(catalog: list[dict[str, Any]], message: str, history: list[dict[str, str]] | None = None) -> str:
    lines = []
    for c in catalog:
        tags = []
        if c.get("has_issues"):
            tags.append("有待处理项")
        if c.get("has_analytics"):
            tags.append("支持效能问答")
        lines.append(f"  - {c['workflow_id']} | {c['name']}" + (f"（{'、'.join(tags)}）" if tags else ""))
    catalog_text = "\n".join(lines) or "（暂无流程）"
    hist = _render_history(history)
    hist_block = f"\n\n最近对话（用于理解指代，如「那这条呢」指的是上文提到的流程）：\n{hist}" if hist else ""
    return f"流程目录：\n{catalog_text}{hist_block}\n\n用户当前这句：{message}"


class ManageCopilotAgent:
    def __init__(self, model: Any | None = None) -> None:
        self.model = model or create_bedrock_chat_model()
        self.structured_model = (
            None
            if is_legacy_text_generator(self.model)
            else self.model.with_structured_output(ManageRoute, method="function_calling", include_raw=True)
        )

    def classify(self, catalog: list[dict[str, Any]], message: str, history: list[dict[str, str]] | None = None) -> ManageRoute | None:
        if self.structured_model is None:
            return None
        response = self.structured_model.invoke(
            [
                {"role": "system", "content": _classify_system_prompt()},
                {"role": "user", "content": _classify_user_prompt(catalog, message, history)},
            ]
        )
        return _coerce(response)


def _coerce(response: Any) -> ManageRoute | None:
    parsed: Any = None
    parsing_error: Any = None
    if isinstance(response, dict):
        parsed = response.get("parsed")
        parsing_error = response.get("parsing_error")
    elif isinstance(response, ManageRoute):
        parsed = response
    else:
        parsed = getattr(response, "parsed", None)
        parsing_error = getattr(response, "parsing_error", None)
    if parsing_error or parsed is None:
        return None
    if not isinstance(parsed, ManageRoute):
        try:
            parsed = ManageRoute.model_validate(parsed)
        except Exception:  # noqa: BLE001
            return None
    return parsed


def _global_analytics_system_prompt() -> str:
    return """你是企业 OA「流程管理」总览页的效能问答助手，负责把多条流程各自的效能分析结果
汇总成一句话回答 owner 的跨流程问题（如"总共在跑多少单""整体完成率如何"）。只依据给你的各
流程回答做加总/归纳，不要编造未提供的数字；有流程暂未接入效能数据的，在回答里如实说明未
计入统计，不要假装有全量数据。用简洁中文回答，不用英文双引号。"""


def _structure_system_prompt() -> str:
    return """你是企业 OA 的流程设计讲解助手，给流程 owner 解释某条流程当前是怎么设计的——
字段、环节、审批人、提交路径。只依据给你的流程定义原文作答，不要编造定义里没有的内容；
定义里没覆盖到的细节，直说"当前定义未体现"。用简洁中文回答，不用英文双引号。"""


def _structure_user_prompt(name: str, definition: dict[str, Any], question: str, history: list[dict[str, str]] | None = None) -> str:
    fields = definition.get("form_fields") or []
    nodes = definition.get("flow_nodes") or []
    field_lines = "\n".join(
        f"  - {f.get('field_name')}：{f.get('component_type')}，{f.get('logic_description') or ''}" for f in fields
    )
    node_lines = []
    for n in nodes:
        paths = n.get("submit_paths") or []
        path_desc = "；".join(
            f"{p.get('path_name')}→{p.get('target_node_id')}" + (f"（{p.get('condition')}）" if p.get("condition") else "")
            for p in paths
        )
        handler = ((n.get("handler") or {}).get("role")) if n.get("handler") else ("起草人" if n.get("is_draft") else "—")
        node_lines.append(f"  - {n.get('node_name')}（处理人：{handler}）：{path_desc}")
    hist = _render_history(history)
    hist_block = f"\n\n最近对话：\n{hist}" if hist else ""
    return f"""流程「{name}」当前定义：

表单字段：
{field_lines or "（无）"}

流程环节与提交路径：
{chr(10).join(node_lines) or "（无）"}{hist_block}

流程 owner 的问题：{question}"""


class ManageCopilotService:
    def __init__(
        self,
        *,
        slice1_service: Any,
        workflow_design_service: Any,
        analytics_by_workflow_id: dict[str, Any] | None = None,
        launch_service: Any | None = None,
        insight_store: Any | None = None,
        agent: ManageCopilotAgent | None = None,
        model: Any | None = None,
    ) -> None:
        self._slice1 = slice1_service
        self._workflow_design = workflow_design_service
        self._analytics_by_workflow_id = analytics_by_workflow_id or {}
        self._launch = launch_service  # 复用发起向导：申请人视角问题（该走哪个流程/按取值预演路径/资格）
        self._insights = insight_store  # 反哺洞察：答"某条流程有没有待处理/优化项"用
        self._agent = agent
        self._model = model

    def _get_agent(self) -> ManageCopilotAgent:
        if self._agent is None:
            self._agent = ManageCopilotAgent(model=self._model)
        return self._agent

    def _catalog(self) -> list[dict[str, Any]]:
        # 待处理判定要跟流程管理卡片同一口径：若已有设计会话，issue_count 用会话里更准确的
        # 完整校验结果（覆盖字段/环节/角色/路径等全部类别），不是行内定义算出的浅层数字
        # （否则像"已经在设计页修过一次"的流程，这里还会误判成有问题）。
        defs = self._slice1.workflow_definitions()
        catalog = []
        for d in defs:
            issue_count = (d.get("management") or {}).get("issue_count") or 0
            summary = self._workflow_design.design_summary_for_workflow(d["workflow_definition_id"])
            if summary:
                issue_count = summary.get("issue_count", issue_count)
            catalog.append(
                {
                    "workflow_id": d["workflow_id"],
                    "workflow_definition_id": d["workflow_definition_id"],
                    "name": d["name"],
                    "status": d.get("status"),
                    "has_issues": bool(issue_count),
                    "has_analytics": d["workflow_id"] in self._analytics_by_workflow_id,
                }
            )
        return catalog

    def ask(self, message: str, history: list[dict[str, str]] | None = None) -> dict[str, Any]:
        clean = (message or "").strip()
        if not clean:
            return {
                "route": "general",
                "reply": "问我：批量处理待办事项 / 某条流程怎么设计的 / 某条流程的效能数据（目前请假、报销支持效能问答）。",
            }
        catalog = self._catalog()
        route = self._get_agent().classify(catalog, clean, history)
        if route is None:
            return {"route": "general", "reply": "流程总览助手暂不可用（模型未就绪）。"}

        if route.route == "catalog":
            return self._catalog_answer(catalog)

        if route.route == "batch_fix":
            n = sum(1 for c in catalog if c["has_issues"])
            reply = (
                f"好，我来盘点一下——目前有 {n} 条流程有待处理项，逐条诊断中，请稍候…"
                if n
                else "目前没有流程有待处理项，都很干净。"
            )
            return {"route": "batch_fix", "reply": reply, "flows_with_issues": n}

        if route.route == "flow_issues":
            return self._flow_issues_answer(route.target_workflow_id, catalog)

        if route.route == "flow_structure":
            return self._flow_structure_answer(route.target_workflow_id, catalog, clean, history)

        if route.route == "analytics":
            return self._analytics_answer(route.target_workflow_id, catalog, clean, history)

        if route.route == "launch_guide":
            return self._launch_guide_answer(clean)

        return {"route": "general", "reply": route.general_reply or "这类问题目前不在流程总览助手的范围内。"}

    def _cross_hook(self, entry: dict[str, Any] | None) -> str:
        # 轻量编排：事实主体由子能力原样给出，orchestrator 只在末尾挂一句它自己才知道的
        # 跨模块钩子（该流程有没有待处理项）。纯确定性拼接，不重跑 LLM、不改事实内容。
        if entry and entry.get("has_issues"):
            return f"\n\n（另外，这条流程还有待处理项，需要的话我可以帮你逐条诊断修复。）"
        return ""

    def _catalog_answer(self, catalog: list[dict[str, Any]]) -> dict[str, Any]:
        # 目录本身是已经算好的确定性数据（有几条、叫什么、状态），直接格式化，不用再叫一次 LLM 猜。
        if not catalog:
            return {"route": "catalog", "reply": "当前没有任何流程。"}
        published = [c for c in catalog if c.get("status") == "PUBLISHED"]
        draft = [c for c in catalog if c.get("status") != "PUBLISHED"]
        lines = [f"共 {len(catalog)} 条流程（已上架 {len(published)} 条，草稿 {len(draft)} 条）："]
        for c in catalog:
            tags = []
            tags.append("已上架" if c.get("status") == "PUBLISHED" else "草稿")
            if c.get("has_issues"):
                tags.append("有待处理项")
            if c.get("has_analytics"):
                tags.append("支持效能问答")
            lines.append(f"- {c['name']}（{c['workflow_id']}） · {'、'.join(tags)}")
        return {"route": "catalog", "reply": "\n".join(lines)}

    def _flow_issues_answer(self, workflow_id: str | None, catalog: list[dict[str, Any]]) -> dict[str, Any]:
        """某一条流程有没有待处理/需优化项——确定性列出该流程的反哺洞察（运维/分析识别的）
        + 设计待确认数，不重跑 LLM。这些正是流程管理卡片上「待处理」那几条的来源。"""
        entry = next((c for c in catalog if c["workflow_id"] == workflow_id), None)
        if entry is None:
            return {"route": "flow_issues", "reply": "没能定位到你问的是哪条流程，说下流程名称？"}
        lines: list[str] = []
        if self._insights is not None:
            try:
                for ins in self._insights.open_for(entry["workflow_id"]):
                    head = getattr(ins, "headline", "") or ""
                    if head:
                        lines.append(f"- {head}")
            except Exception:  # noqa: BLE001 - 洞察取不到就只报设计待确认，不崩
                pass
        summary = self._workflow_design.design_summary_for_workflow(entry["workflow_definition_id"])
        design_n = (summary or {}).get("issue_count", 0) if summary else 0
        if design_n:
            lines.append(f"- 设计侧还有 {design_n} 项待确认（配置缺口或澄清未清）")
        if not lines:
            return {"route": "flow_issues", "target_workflow_id": workflow_id,
                    "reply": f"「{entry['name']}」目前没有待处理项，也没有检出需要优化的地方，挺干净的。"}
        body = "\n".join(lines)
        return {"route": "flow_issues", "target_workflow_id": workflow_id,
                "reply": f"「{entry['name']}」有这些待处理 / 可优化项：\n{body}\n\n需要的话，进这条流程的设计页，我可以逐条诊断并给修复方案。"}

    def _flow_structure_answer(self, workflow_id: str | None, catalog: list[dict[str, Any]], message: str, history: list[dict[str, str]] | None = None) -> dict[str, Any]:
        entry = next((c for c in catalog if c["workflow_id"] == workflow_id), None)
        if entry is None:
            return {"route": "flow_structure", "reply": "没能定位到你问的是哪条流程，可以说下流程名称吗？"}
        detail = self._slice1.workflow_definition_detail(entry["workflow_definition_id"])
        definition = (detail or {}).get("definition") or {}
        model = self._model or create_bedrock_chat_model()
        if is_legacy_text_generator(model):
            return {"route": "flow_structure", "target_workflow_id": workflow_id, "reply": "流程总览助手暂不可用（模型未就绪）。"}
        response = model.invoke(
            [
                {"role": "system", "content": _structure_system_prompt()},
                {"role": "user", "content": _structure_user_prompt(entry["name"], definition, message, history)},
            ]
        )
        reply = getattr(response, "content", None) or str(response)
        return {"route": "flow_structure", "target_workflow_id": workflow_id, "reply": reply + self._cross_hook(entry)}

    def _analytics_answer(self, workflow_id: str | None, catalog: list[dict[str, Any]], message: str, history: list[dict[str, str]] | None = None) -> dict[str, Any]:
        entry = next((c for c in catalog if c["workflow_id"] == workflow_id), None)
        if workflow_id and entry is None:
            # 模型给的 target_workflow_id 在目录里找不到（结构化输出偶尔会吐"<UNKNOWN>"这类占位符
            # 而不是真的留空）——当成"没指定具体流程"处理，走跨流程聚合，不把这个无意义字符串
            # 直接展示给用户。
            workflow_id = None
        if workflow_id is None:
            return self._global_analytics_answer(catalog, message, history)
        service = self._analytics_by_workflow_id.get(workflow_id)
        if service is None:
            return {"route": "analytics", "target_workflow_id": workflow_id, "reply": f"「{entry['name']}」暂未接入效能分析数据，答不了这个问题。"}
        # 分析服务本来就支持 history，透传下去，让它的问答也能理解多轮上下文
        result = service.answer_question(message=message, history=history)
        return {"route": "analytics", "target_workflow_id": workflow_id, "reply": result.get("reply", "") + self._cross_hook(entry)}

    def _global_analytics_answer(self, catalog: list[dict[str, Any]], message: str, history: list[dict[str, str]] | None = None) -> dict[str, Any]:
        """跨流程/全局效能问题：没法只查一条流程的数据源作答，把每条已接入效能分析的流程
        都各自问一遍（各自只看得到自己的数据，互不干扰），再用一次 LLM 把几条回答汇总成一句话。
        没接入的流程如实报"未计入"，不假装有全量数据。"""
        wired = [c for c in catalog if c["workflow_id"] in self._analytics_by_workflow_id]
        unwired_names = [c["name"] for c in catalog if c["workflow_id"] not in self._analytics_by_workflow_id]
        if not wired:
            return {"route": "analytics", "target_workflow_id": None, "reply": "目前没有任何流程接入效能分析数据，答不了这个问题。"}
        per_flow: list[tuple[str, str]] = []
        for c in wired:
            service = self._analytics_by_workflow_id[c["workflow_id"]]
            result = service.answer_question(message=message, history=history)
            per_flow.append((c["name"], result.get("reply", "")))
        reply = self._synthesize_global_answer(message, per_flow, unwired_names)
        return {"route": "analytics", "target_workflow_id": None, "reply": reply}

    def _synthesize_global_answer(self, message: str, per_flow: list[tuple[str, str]], unwired_names: list[str]) -> str:
        model = self._model or create_bedrock_chat_model()
        unwired_note = f"\n\n以下流程暂未接入效能分析数据，不在统计范围内：{'、'.join(unwired_names)}" if unwired_names else ""
        if is_legacy_text_generator(model):
            # 模型不可用时退化成直接拼接，不做二次组织，但仍然给出各流程的原始回答
            lines = [f"「{name}」：{reply}" for name, reply in per_flow]
            return "\n".join(lines) + unwired_note
        per_flow_text = "\n\n".join(f"「{name}」的回答：{reply}" for name, reply in per_flow)
        response = model.invoke(
            [
                {"role": "system", "content": _global_analytics_system_prompt()},
                {"role": "user", "content": f"用户问题：{message}\n\n各流程分别给出的回答：\n{per_flow_text}{unwired_note}"},
            ]
        )
        return getattr(response, "content", None) or str(response)

    def _launch_guide_answer(self, message: str) -> dict[str, Any]:
        # 复用发起向导：它自己就会推荐流程 + 确定性预演路径（preview_path）+ 检索资格规则。
        # orchestrator 只透传它的答复，不重写（路径预演是确定性算出来的，尤其不能过 LLM 再组织）。
        if self._launch is None:
            return {"route": "launch_guide", "reply": "发起向导能力未接入，答不了申请人视角的问题。"}
        r = self._launch.ask(message)
        reply = getattr(r, "reply", "") or ""
        preview = getattr(r, "path_preview", None) or []
        if preview:
            reply += "\n\n预演路径（确定性）：" + " → ".join(preview)
        return {"route": "launch_guide", "reply": reply, "recommended_process_name": getattr(r, "recommended_process_name", None)}
