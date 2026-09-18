"""运维副驾编排：诊断 → agent 决策 → 分级落地 → 应用。

三条落地路径（见 ops_agent.OpsDecision.route）：
- user_confirm：只动发起人自己的数据/撤回（低风险）→ 发起人确认门（confirm/discard）。
- authorization：动流程走向/审批人（高风险）→ 生成授权工单，转流程负责人 approve/reject。
- terminal：deny（规则拒绝）/ escalate（升级）/ no_issue / clarify → 一句话答复，无动作。

复用设计侧"拟议→确认/放弃"哲学，并在其上加了一层"owner 授权闭环"。实例存在内存里
（Mock adapter），不写 DB。
"""

from __future__ import annotations

from typing import Any, Callable
from uuid import uuid4

from pydantic import BaseModel

from app.copilots.mock_instances import MockInstance, build_mock_instances
from app.copilots.ops_agent import OpsCopilotAgent, OpsDecision, build_actions_from_decision
from app.org_knowledge import load_org_seed
from app.runtime.condition_eval import evaluate_condition
from app.tools.instance_actions import InstanceAction, apply_instance_action
from data.schema import AuthorizationRequest, ProcessDefinition


def _context_stuck(ctx: Any) -> bool:
    """这单是不是"真卡"——区别于"在等处理人给结论"。判定口径（确定性，不猜）：
    - 已到 END：办结，不卡。
    - 选不到处理人（组织缺口）：卡。
    - 已有匹配的前进路径：正常在途，不卡。
    - 没有匹配路径时，用「结论性意见=同意」探一下：同意就能走 → 只是在等决策，不算卡；
      同意也走不了 → 真卡（覆盖漏洞/数据落空）。
    这样"待审批但还没填结论"不会被误判成卡住。"""
    if ctx is None or ctx.current_node_id == "END":
        return False
    if ctx.approver_resolution is not None and ctx.approver_resolution.is_empty:
        return True
    if ctx.has_any_matched_path():
        return False
    node = ctx.process.get_node_by_id(ctx.current_node_id)
    if node is None:
        return True
    probe = {**ctx.form_values, "结论性意见": "同意"}
    for p in node.submit_paths:
        if p.target_node_id in ("DRAFT",):  # 退回起草不算前进
            continue
        if evaluate_condition(p.condition, probe).matched:
            return False
    return True

# 运维/override 规则（owner + 运维团队沉淀）。真实系统里这些应存进规则 RAG、按流程
# 检索；此处先内联一小组，让 override 决策有据可依。合规硬环节类规则会触发 deny。
DEFAULT_OPS_RULES = """1. 【覆盖漏洞并档】请假天数落在流程路径的覆盖漏洞区间（既不满足直接结束、也不满足上报条件）
   且用户确认天数无误时，运维可将其并入最近的更高一档路径（如跳转到上一级审批环节）。此为
   override，须流程负责人授权。
2. 【岗位空缺改派】某审批环节因岗位空缺/无人挂接导致解析不到审批人时，运维可临时改派给该
   环节的同级或上级负责人。此为 override，须流程负责人授权。
3. 【合规硬环节不得跳过】涉及合规审核、法务审核、财务复核等合规硬性环节，任何情况下都不得
   跳过或绕过；用户提出跳过这类环节的，直接拒绝（deny），引用本条。
4. 【撤回自助】发起人要求撤回自己尚未完成的单子重填，属自助操作，发起人确认即可。
5. 未被以上规则覆盖的情形，一律升级人工（escalate），不得自行 override。"""


class OpsProposal(BaseModel):
    instance_id: str
    reply: str
    category: str
    rationale: str
    route: str  # user_confirm | authorization | terminal
    needs_confirmation: bool  # == (route == user_confirm)，兼容前端旧字段
    pending_summary: str | None = None       # user_confirm：待发起人确认的动作摘要
    authorization_id: str | None = None      # authorization：已生成的授权工单 id
    authorization_summary: str | None = None  # authorization：拟执行动作摘要


class OpsApplyResult(BaseModel):
    instance_id: str
    applied: bool
    errors: list[str] = []
    now_unblocked: bool | None = None  # 应用后是否已不再卡（供 UI 反馈）


class OpsCopilotService:
    def __init__(
        self,
        agent: OpsCopilotAgent | None = None,
        org_data: dict[str, Any] | None = None,
        knowledge_index: Any | None = None,
        insight_store: Any | None = None,
        process_lookup: Callable[[str], ProcessDefinition] | None = None,
    ) -> None:
        self._org = org_data or load_org_seed()
        self._agent = agent  # 惰性：真正 decide 时才需要（避免无凭证环境构造 Bedrock）
        self._knowledge_index = knowledge_index  # 惰性：检索运维规则用（真嵌入）
        self._insights = insight_store  # 反哺洞察存储（可空；注入则运维终局结算写入）
        # workflow_definition_id → ProcessDefinition：给 ensure_instance 现建"待办队列"里
        # 那些不在下面这份小 mock 库里的单据（比如审批协助的差旅报销）用；不注入就只服务
        # 这份 fixtures，ensure_instance 对未知 id 会老实返回 None，不假装能诊断。
        self._process_lookup = process_lookup
        self._instances: dict[str, MockInstance] = {m.instance_id: m for m in build_mock_instances(self._org)}
        self._pending: dict[str, list[InstanceAction]] = {}
        self._authorizations: dict[str, AuthorizationRequest] = {}
        self._auth_actions: dict[str, list[InstanceAction]] = {}  # 工单 id → 待授权的动作列表
        # 出厂 fixture 的 id 集合——reset_instance 靠它判断"这条有没有出厂快照可以现算复原"，
        # 而不是"能不能在 build_mock_instances() 的返回里找到"（避免复位途中误判）。
        self._original_instance_ids: set[str] = set(self._instances)

    # —— 桥接：待办队列里的单据本没有 InstanceContext（只是 WorklistItem 的表单值+定位
    # 信息），现场建一份、跟这份小 mock 库存进同一个字典——之后 inspect/diagnose/propose/
    # confirm/discard 全部直接复用现成方法，不用另开一套。幂等：已存在直接返回。——
    def ensure_instance(
        self,
        instance_id: str,
        *,
        workflow_definition_id: str | None = None,
        node_id: str | None = None,
        form_values: dict[str, Any] | None = None,
        title: str = "",
        process_name: str = "",
        can_submit_decision: bool = False,
        initiator_user_id: str = "u_it_app_staff",
        uploaded_materials: list[str] | None = None,
        approval_history: list[dict[str, Any]] | None = None,
    ) -> MockInstance | None:
        existing = self._instances.get(instance_id)
        if existing is not None:
            return existing
        if not workflow_definition_id or not node_id or form_values is None or self._process_lookup is None:
            return None
        try:
            process = self._process_lookup(workflow_definition_id)
        except Exception:  # noqa: BLE001 - 定位不到就老实返回 None，不假装能诊断
            return None
        if process.get_node_by_id(node_id) is None:
            return None
        from app.copilots.diagnostics import build_instance_context

        context = build_instance_context(
            instance_id=instance_id, process=process, current_node_id=node_id,
            form_values=form_values, org_data=self._org,
            # 用待办条目携带的真实发起人 id 解析各环节处理人——缺省是有部门归属的员工，
            # 这样"本部门主管/总经理"这类角色能解析到真人，不再出现"处理人为空"的假卡点。
            initiator_user_id=initiator_user_id,
            uploaded_materials=uploaded_materials or [],
            history=approval_history or [],
        )
        inst = MockInstance(
            instance_id=instance_id, title=title or instance_id,
            process_name=process_name or process.meta.process_name,
            status="flowing", stuck_kind=None, context=context,
            can_submit_decision=can_submit_decision,
        )
        self._instances[instance_id] = inst
        return inst

    # —— 反哺闭环：运维臂 结算 + 读取 ——
    def record_incident(self, instance_id: str, at: str | None = None) -> Any | None:
        """运维终局结算：把这单的设计相关根因反哺（若注入了 store）。返回记录的洞察或 None。"""
        if self._insights is None:
            return None
        inst = self.get_instance(instance_id)
        ctx = inst.context
        if ctx is None:
            return None
        from app.insights.producers import OpsInsightProducer

        diag = self.diagnose(instance_id)
        node_name = next(
            (n.node_name for n in ctx.process.flow_nodes if n.node_id == ctx.current_node_id),
            None,
        )
        return OpsInsightProducer.record_from_diagnosis(
            self._insights,
            workflow_definition_id=ctx.process.meta.process_id,
            node_id=ctx.current_node_id,
            node_name=node_name,
            stuck_kind=inst.stuck_kind,
            blocked_paths=diag.get("blocked_paths"),
            version=ctx.process.meta.version,
            at=at,
        )

    def insights_for(self, workflow_definition_id: str) -> list[Any]:
        return self._insights.open_for(workflow_definition_id) if self._insights else []

    def insight_channel_counts(self, workflow_definition_id: str) -> dict[str, int]:
        return self._insights.channel_counts(workflow_definition_id) if self._insights else {}

    def seed_reback_history(self, instance_id: str = "inst_gap", times: int = 4) -> None:
        """demo 用：模拟近 30 天 N 次同因卡住，累计 occurrences（真实运行由每次结算自然累加）。"""
        for k in range(times):
            self.record_incident(instance_id, at=f"2026-06-{10 + k:02d}T09:00:00")

    def seed_authorization(self, instance_id: str = "inst_gap", target_node_id: str = "line_leader") -> str | None:
        """demo 用：预置一张待授权工单，让运维授权台一进来就有内容（真实运行由 propose→authorization 生成）。
        动作与真实 propose 路由到 authorization 时构造的一致（JumpToNode），approve 会真正 apply。"""
        from uuid import uuid4

        from app.tools.instance_actions import JumpToNode

        inst = self._instances.get(instance_id)
        if inst is None or inst.context is None:
            return None
        from app.org_knowledge import user_name

        req_id = f"auth_seed_{uuid4().hex[:8]}"
        self._authorizations[req_id] = AuthorizationRequest(
            request_id=req_id, instance_id=instance_id, instance_title=inst.title,
            action_summary=f"跳转到「{inst.context.process.get_node_name(target_node_id)}」审批",
            reason="落在「4–7 天」覆盖漏洞的请假，按《运维异常处理规则》RULE-OPS-003 并入最近的更高一档路径。",
            requested_by=user_name(self._org, inst.context.initiator_user_id),
        )
        self._auth_actions[req_id] = [JumpToNode(target_node_id=target_node_id)]
        return req_id

    # —— 只读：给"我的流程"页面 ——
    def list_instances(self) -> list[MockInstance]:
        return list(self._instances.values())

    def get_instance(self, instance_id: str) -> MockInstance:
        if instance_id not in self._instances:
            raise KeyError(f"未知实例 {instance_id}")
        return self._instances[instance_id]

    def reset_instance(self, instance_id: str) -> bool:
        """演示重置：撤销运维/在办副驾对这条实例做过的任何修改，回到最初状态。
        出厂 fixture（inst_gap 等 6 条）——按同一套 org_data 重新现算一份新快照覆盖回去，
        这本身就是确定性的（build_instance_context 是纯函数），等于清零。
        桥接来的（ap_expense_wang 等，来自待办队列）——本没有"出厂快照"这回事，直接从库里
        摘除即可；WorklistItem 本身不可变，下次 ensure_instance 会用它的原始字段重新现算，
        效果等价于复位。同时清掉待确认动作/挂起的授权工单，避免复位后残留一个指向旧状态的
        pending action。"""
        self._pending.pop(instance_id, None)
        if instance_id in self._original_instance_ids:
            fresh = {m.instance_id: m for m in build_mock_instances(self._org)}
            restored = fresh.get(instance_id)
            if restored is None:
                return False
            self._instances[instance_id] = restored
            return True
        return self._instances.pop(instance_id, None) is not None

    def reset_demo_state(self) -> None:
        """演示"切回起始态"：把整份内存态推倒重来——出厂 fixture 现算复原、桥接实例摘掉、
        待确认动作/挂起授权工单全清。等价于进程刚启动、seed_* 还没跑的样子；清完由调用方
        重跑 seed_reback_history/seed_authorization 补回出厂演示数据（跟 create_app 启动顺序一致）。
        比逐条 reset_instance 更彻底：它连桥接进来但已不在 worklist 的实例、以及授权工单都一并清掉。"""
        self._instances = {m.instance_id: m for m in build_mock_instances(self._org)}
        self._pending = {}
        self._authorizations = {}
        self._auth_actions = {}
        self._original_instance_ids = set(self._instances)

    _STATUS_LABELS = {"flowing": "流转中", "stuck": "卡住", "completed": "已办结"}

    def list_view(self) -> dict[str, Any]:
        return {
            "items": [
                {
                    "instance_id": m.instance_id, "title": m.title, "process_name": m.process_name,
                    "status": m.status, "status_label": self._STATUS_LABELS.get(m.status, m.status),
                    "current_node_name": self._current_node_name(m),
                }
                for m in self._instances.values()
            ]
        }

    def instance_view(self, instance_id: str) -> dict[str, Any]:
        from app.org_knowledge import user_name

        m = self.get_instance(instance_id)
        ctx = m.context
        nodes = [
            {"node_id": n.node_id, "node_name": n.node_name, "is_draft": n.is_draft,
             "current": ctx is not None and n.node_id == ctx.current_node_id}
            for n in m.context.process.flow_nodes
        ] if ctx else []
        approval_history: list[dict[str, Any]] = []
        if ctx is not None:
            for h in ctx.history:
                node = ctx.process.get_node_by_id(h.node_id)
                approval_history.append({
                    "node_id": h.node_id,
                    "node_name": h.node_name,
                    "opinion_label": (node.opinion_label if node else None) or h.node_name,
                    "actor_user_id": h.actor_user_id,
                    "actor_name": user_name(self._org, h.actor_user_id) if h.actor_user_id else None,
                    "opinion": h.opinion,
                    "comment": h.comment,
                    "occurred_at": h.occurred_at,
                })
        return {
            "instance_id": m.instance_id, "title": m.title, "process_name": m.process_name,
            "status": m.status, "status_label": self._STATUS_LABELS.get(m.status, m.status),
            "current_node_id": ctx.current_node_id if ctx else None,
            "form_values": ctx.form_values if ctx else {},
            "nodes": nodes,
            "approval_history": approval_history,
            "diagnosis": self.diagnose(instance_id),
            "has_pending": self.has_pending(instance_id),
            "can_submit_decision": m.can_submit_decision,
        }

    def inspect(self, instance_id: str) -> dict[str, Any]:
        """在办任务主动体检（#2）：材料缺失 + 合规红线，确定性、复用现有引擎。"""
        from app.copilots.task_inspection import inspect_task

        inst = self.get_instance(instance_id)
        if inst.context is None:
            return {"diagnosable": False, "clean": True, "material_findings": [], "compliance_findings": []}
        result = inspect_task(inst.context)
        return {
            "diagnosable": True,
            "clean": result.clean,
            "material_findings": [f.model_dump() for f in result.material_findings],
            "compliance_findings": result.compliance_findings,
        }

    def _current_node_name(self, m: MockInstance) -> str | None:
        if m.context is None:
            return None
        node = m.context.process.get_node_by_id(m.context.current_node_id)
        return node.node_name if node else m.context.current_node_id

    def diagnose(self, instance_id: str) -> dict[str, Any]:
        """确定性诊断摘要（不调 LLM），供实例详情页"为什么卡"面板直接用。"""
        inst = self.get_instance(instance_id)
        ctx = inst.context
        if ctx is None:
            return {"instance_id": instance_id, "diagnosable": False}
        return {
            "instance_id": instance_id,
            "diagnosable": True,
            "current_node_id": ctx.current_node_id,
            "stuck": _context_stuck(ctx),
            "blocked_paths": [
                {"path_name": p.path_name, "target_node_id": p.target_node_id, "reason": p.reason}
                for p in ctx.blocked_paths()
            ],
            "approver_empty": bool(ctx.approver_resolution and ctx.approver_resolution.is_empty),
            "approver_reason": (ctx.approver_resolution.reason if ctx.approver_resolution else None),
        }

    # —— 对话：拟议（按 route 分流：确认门 / 授权工单 / 终局）——
    def propose(
        self,
        instance_id: str,
        user_message: str,
        *,
        history: list[dict[str, str]] | None = None,
        ops_rules: str | None = None,
    ) -> OpsProposal:
        inst = self.get_instance(instance_id)
        if inst.context is None:
            return _terminal(instance_id, "这张单子已办结，没有可处理的异常。", "no_issue")
        rules = self._retrieve_ops_rules(inst, user_message) if ops_rules is None else ops_rules
        decision = self._get_agent().decide(
            inst.context, user_message, ops_rules=rules, conversation_context=_render_history(history or []),
            can_submit_decision=inst.can_submit_decision, org_data=self._org,
        )
        if decision is None:
            return _terminal(instance_id, "运维助手暂不可用（模型未就绪）。", "escalate")
        return self._proposal_from_decision(instance_id, inst, decision)

    def _proposal_from_decision(self, instance_id: str, inst: MockInstance, decision: OpsDecision) -> OpsProposal:
        route = decision.route()
        if route == "terminal":
            self._pending.pop(instance_id, None)
            return OpsProposal(instance_id=instance_id, reply=decision.reply, category=decision.category,
                               rationale=decision.rationale, route="terminal", needs_confirmation=False)

        actions = build_actions_from_decision(
            decision, inst.context, can_submit_decision=inst.can_submit_decision, org_data=self._org,
        )
        if not actions:  # 决策类别需要动作但没给全参数 → 降级为升级人工（别沿用 LLM 那句可能承诺了授权的 reply）
            self._pending.pop(instance_id, None)
            return _terminal(
                instance_id,
                # 别把发起人指向运维授权台——那是流程负责人的台子，他去了也点不了（同"去授权台"按钮那次的教训）。
                "这个诉求我没能落成一个可执行的动作——改派得指定组织里真实存在的人员、跳转得给到诊断里真实存在的环节。"
                "已按升级人工处理。你也可以补充一个明确的目标人或目标环节，我再重新判断一次。",
                "escalate", rationale="参数不合法，无法构造动作，转人工。",
            )

        summary = _actions_summary(actions, process=inst.context.process, org_data=self._org)
        if route == "user_confirm":
            self._pending[instance_id] = actions
            return OpsProposal(instance_id=instance_id, reply=decision.reply, category=decision.category,
                               rationale=decision.rationale, route="user_confirm", needs_confirmation=True,
                               pending_summary=summary)

        # route == "authorization"：生成授权工单，转流程负责人（发起人这边无确认按钮）
        from app.org_knowledge import user_name

        req_id = f"auth_{uuid4().hex[:10]}"
        self._authorizations[req_id] = AuthorizationRequest(
            request_id=req_id, instance_id=instance_id, instance_title=inst.title,
            action_summary=summary, reason=decision.rationale or decision.reply,
            requested_by=user_name(self._org, inst.context.initiator_user_id),
        )
        self._auth_actions[req_id] = actions
        # reply 用确定性文案，锁定成"实际要执行的那个动作"（summary），不沿用 LLM 可能与动作不一致的自由措辞——
        # 之前出现过 reply 说跳 A、工单里动作却是 B。rationale 仍保留 LLM 的依据说明。
        auth_reply = (
            f"已按你的诉求生成一张运维授权工单：{summary}。这属于会改变流程走向的高风险破例处置，"
            f"不能自助执行，已转交流程负责人在「运维授权台」审批；批准后系统自动执行、单据继续下送。"
        )
        return OpsProposal(instance_id=instance_id, reply=auth_reply, category=decision.category,
                           rationale=decision.rationale, route="authorization", needs_confirmation=False,
                           authorization_id=req_id, authorization_summary=summary)

    def has_pending(self, instance_id: str) -> bool:
        return instance_id in self._pending

    # —— 授权工单（给流程负责人/管理员的"运维授权台"）——
    def list_authorizations(self) -> dict[str, Any]:
        return {"items": [a.model_dump() for a in self._authorizations.values()]}

    def authorize(self, request_id: str, *, approve: bool, note: str = "") -> OpsApplyResult:
        req = self._authorizations.get(request_id)
        if req is None:
            raise KeyError(f"未知授权工单 {request_id}")
        if req.status != "pending":
            raise RuntimeError("该授权工单已处理。")
        req.decision_note = note or None
        if not approve:
            req.status = "rejected"
            return OpsApplyResult(instance_id=req.instance_id, applied=False, errors=["授权被驳回"])
        req.status = "approved"
        actions = self._auth_actions.pop(request_id)
        return self._apply(req.instance_id, actions)

    def confirm(self, instance_id: str) -> OpsApplyResult:
        actions = self._pending.get(instance_id)
        if not actions:
            raise RuntimeError("当前没有待确认的运维动作。")
        result = self._apply(instance_id, actions)
        if result.applied:
            self._pending.pop(instance_id, None)
        return result

    def discard(self, instance_id: str) -> None:
        self._pending.pop(instance_id, None)

    def _apply(self, instance_id: str, actions: list[InstanceAction]) -> OpsApplyResult:
        """确定性依次应用动作列表（data_fix 可多字段）+ 重算 stuck 状态。confirm 与 authorize 共用。
        运维是特权操作：可在非 draft 环节改数据（allow_stage_override=True）。任一动作非法即整体不落地。"""
        inst = self.get_instance(instance_id)
        ctx = inst.context
        for action in actions:
            result = apply_instance_action(ctx, action, org_data=self._org, allow_stage_override=True)
            if not result.applied:
                return OpsApplyResult(instance_id=instance_id, applied=False, errors=result.errors)
            ctx = result.context
        self._instances[instance_id] = _with_context(inst, ctx)
        return OpsApplyResult(instance_id=instance_id, applied=True, now_unblocked=not _context_stuck(ctx))

    def _get_agent(self) -> OpsCopilotAgent:
        if self._agent is None:
            self._agent = OpsCopilotAgent()
        return self._agent

    def _retrieve_ops_rules(self, inst: MockInstance, user_message: str) -> str:
        """从规则 RAG 检索适用的运维规则（跟模块3 打通）；索引不可用则回退到内联规则。
        无据不编：检索到就用检索到的，检索为空/失败才回退，回退也标注来源。"""
        index = self._get_index()
        if index is None:
            return DEFAULT_OPS_RULES
        try:
            proc_name = inst.context.process.meta.process_name if inst.context else ""
            query = f"{proc_name} 运维 override 卡住 {user_message}"
            hits = index.semantic_search(query, k=4)
        except Exception:  # noqa: BLE001 - 检索失败回退
            return DEFAULT_OPS_RULES
        clauses = [h.text for h in hits if getattr(h, "kind", "") == "chunk"]
        if not clauses:
            return DEFAULT_OPS_RULES
        return "\n\n".join(clauses)

    def _get_index(self) -> Any | None:
        if self._knowledge_index is None:
            try:
                from app.rag.index import KnowledgeIndex

                self._knowledge_index = KnowledgeIndex()
            except Exception:  # noqa: BLE001
                return None
        return self._knowledge_index


def _terminal(instance_id: str, reply: str, category: str, *, rationale: str = "") -> OpsProposal:
    return OpsProposal(instance_id=instance_id, reply=reply, category=category,
                       rationale=rationale, route="terminal", needs_confirmation=False)


def _render_history(history: list[dict[str, str]]) -> str:
    lines = []
    for item in history:
        role = "用户" if item.get("role") == "user" else "助手"
        content = (item.get("content") or "").strip()
        if content:
            lines.append(f"{role}：{content}")
    return "\n".join(lines)


def _action_summary(action: InstanceAction, *, process: ProcessDefinition, org_data: dict[str, Any]) -> str:
    from app.org_knowledge import user_name

    op = action.op
    if op == "update_field_value":
        return f"把字段「{action.field_name}」改为 {action.value}"
    if op == "jump_to_node":
        target = "结束" if action.target_node_id == "END" else process.get_node_name(action.target_node_id)
        return f"跳转到「{target}」"
    if op == "skip_current_node":
        return "跳过当前环节"
    if op == "withdraw":
        return "撤回到起草"
    if op == "reassign_approver":
        return f"改派审批人为 {user_name(org_data, action.user_id)}"
    if op == "submit_decision":
        return f"给出结论性意见「{action.opinion}」" + (f"（{action.comment}）" if action.comment else "")
    if op == "update_approval_history_opinion":
        node_name = process.get_node_name(action.node_id)
        parts = []
        if action.opinion is not None:
            parts.append(f"结论订正为「{action.opinion}」")
        if action.comment is not None:
            parts.append("订正说明文字")
        return f"订正「{node_name}」历史记录：" + "、".join(parts)
    return op


def _actions_summary(actions: list[InstanceAction], *, process: ProcessDefinition, org_data: dict[str, Any]) -> str:
    return "；".join(_action_summary(a, process=process, org_data=org_data) for a in actions)


def _with_context(inst: MockInstance, ctx: Any) -> MockInstance:
    # 走到 END 是"办结"；否则用 _context_stuck 判是否真卡（区分"在等决策"与"无路可走"）。
    at_end = ctx.current_node_id == "END"
    new_stuck = _context_stuck(ctx)
    from dataclasses import replace

    status = inst.status
    if inst.context is not None and inst.status == "stuck" and not new_stuck:
        status = "flowing"  # 处理后不再卡
    if at_end:
        status = "completed"
    # 办结后没有环节处理人这回事了，不该再露出"批准/驳回"这类结论性操作
    can_submit_decision = inst.can_submit_decision and not at_end
    return replace(inst, context=ctx, status=status, can_submit_decision=can_submit_decision)
