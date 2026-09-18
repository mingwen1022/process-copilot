"""在办副驾（#2 办理 + #3 排障合并）的决策 agent。

心法跟全项目一致：**LLM 只判断，代码构造并执行**。这是同一个座位、同一条任务的
一段连续动作光谱，不是两个不同域的能力硬拼在一起——共用同一套 InstanceContext、
同一套"typed 决策 → 校验 → 确认门/授权工单/终局"的落地 harness：

    办理（正常动作）──────────────────────► 排障（例外动作）
    submit_decision(同意/不同意)          data_fix / override跳转 / 改派 / 撤回 / 拒绝 / 升级

- LLM 拿到：当前环节的真实内容（表单值+提交路径+条件）+ 确定性诊断（为什么卡）+
  用户诉求 + 适用的规则 → 输出一个轻量决策（category + 少量参数 + 依据），不直接
  吐 typed 动作。
- 代码：把决策映射成 typed InstanceAction、做合法性校验（submit_decision 的目标
  环节由确定性条件求值算出，不是 LLM/用户指定的——安全边界跟 override 类不一样）、
  决定要不要人工确认。

决策优先级（体现在 prompt 里）：当前处理人正常给结论性意见 → submit_decision；
数据填错（低风险、优先于 override）→ data_fix；需要破例 → override（要有规则
支撑）；规则没覆盖或涉及合规硬环节 → escalate 升级人工。

can_submit_decision 开关：只有"当前处理人正在待办的这个环节"才该出现
submit_decision——同一条任务被发起人自己打开查看进度时（比如运维排障场景），
当前浏览者不是这个环节的处理人，不能替处理人下结论，这时候只保留排障那些类别。
这个开关来自调用方（是否是"待我审批"的单据），不是 LLM 自己判断的。
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

from app.models.bedrock import create_bedrock_chat_model
from app.models.text import is_legacy_text_generator
from app.tools.instance_actions import (
    InstanceAction,
    JumpToNode,
    ReassignApprover,
    SkipCurrentNode,
    SubmitDecision,
    UpdateApprovalHistoryOpinion,
    UpdateFieldValue,
    Withdraw,
)
from data.schema import ComponentType, InstanceContext

OpsCategory = Literal[
    "submit_decision",  # 当前处理人给出结论性意见（同意/不同意），走该环节真实提交路径（办理，非 override）
    "data_fix",       # 用户填错了值，改对即可（不需要 override）
    "history_fix",    # 订正某个已完成环节记录的审批意见/说明文字（不重新触发路由，只是改台账）
    "override_jump",  # 跳到指定环节（高风险，须 owner 授权）
    "override_skip",  # 跳过当前环节（高风险，须 owner 授权）
    "reassign",       # 改派审批人（动审批人，须 owner 授权）
    "withdraw",       # 撤回到起草（发起人自己的单，发起人确认即可）
    "deny",           # 规则明令禁止：直接拒绝、引用规则，终局、不占用人
    "escalate",       # 给不出具体方案（无规则/拿不准）→ 升级人工接手
    "no_issue",       # 没发现需要处理的异常
    "clarify",        # 诉求不清，反问
]

# 当前处理人/发起人自己就能确认（只动自己的数据/撤回自己的单/给自己环节的结论，低风险）
_USER_CONFIRM = {"data_fix", "history_fix", "withdraw", "submit_decision"}
# 动流程走向/审批人 → 高风险，须流程负责人授权
_AUTHORIZATION = {"override_jump", "override_skip", "reassign"}


class FieldUpdate(BaseModel):
    field_name: str
    field_value: str = Field(description="新值（字符串，代码按字段类型转换）")


class OpsDecision(BaseModel):
    reply: str = Field(description="给用户的自然语言答复")
    category: OpsCategory
    rationale: str = Field(default="", description="判断依据；override 类须引用规则")
    target_node_id: str | None = Field(default=None, description="override_jump 的目标环节 node_id")
    field_updates: list[FieldUpdate] = Field(default_factory=list, description="data_fix 要改的字段（可多个，一次改齐）")
    field_name: str | None = Field(default=None, description="（兼容单字段写法；优先用 field_updates）")
    field_value: str | None = Field(default=None, description="（兼容单字段写法）")
    reassign_user_id: str | None = Field(default=None, description="reassign 改派给的用户 id")
    opinion: Literal["同意", "不同意"] | None = Field(default=None, description="submit_decision 类：推荐的结论性意见")
    comment: str | None = Field(default=None, description="submit_decision 类：审批意见/驳回或要求补充的说明文字")
    history_node_id: str | None = Field(default=None, description="history_fix：要订正意见的环节 node_id（必须是历史记录里已有的）")
    history_opinion: str | None = Field(default=None, description="history_fix：订正后的结论性意见；不改结论只改说明文字时留空")
    history_comment: str | None = Field(default=None, description="history_fix：订正后的说明文字；不改说明只改结论时留空")

    def route(self) -> Literal["user_confirm", "authorization", "terminal"]:
        """决策的落地路径：发起人确认 / owner 授权 / 终局（拒绝·升级·答复）。"""
        if self.category in _USER_CONFIRM:
            return "user_confirm"
        if self.category in _AUTHORIZATION:
            return "authorization"
        return "terminal"

    def _resolved_field_updates(self) -> list[FieldUpdate]:
        if self.field_updates:
            return self.field_updates
        if self.field_name and self.field_value is not None:
            return [FieldUpdate(field_name=self.field_name, field_value=self.field_value)]
        return []


def build_actions_from_decision(
    decision: OpsDecision, context: InstanceContext, *, can_submit_decision: bool = True,
    org_data: dict[str, Any] | None = None,
) -> list[InstanceAction]:
    """把 LLM 的决策映射成 typed 动作列表（data_fix 可多字段→多个动作；其余单动作）。
    构造不出返回 []（escalate/no_issue/clarify）。合法性由 apply_instance_action 兜底。

    can_submit_decision：确定性兜底——即便 prompt 已经按这个开关裁剪过可选项，LLM
    仍可能在不该给结论性意见的场景下给了 submit_decision（比如发起人查看自己进度时），
    这里再挡一道，不信任模型自己守规矩。

    org_data：给了就顺带校验 reassign_user_id 是不是组织里真实存在的用户——结构化输出
    偶尔会在"选不出目标人"时吐一个占位符字符串（如 "<UNKNOWN>"）而不是真的留空，不校验
    就会生成一张写着改派给不存在的人的授权工单（apply_instance_action 那层最终会拦下来，
    但没必要让流程负责人先看到一张注定失败的工单）。不给 org_data 时跳过这层，只留
    apply 时的硬校验。"""
    if decision.category == "submit_decision":
        if not can_submit_decision or decision.opinion is None:
            return []
        return [SubmitDecision(opinion=decision.opinion, comment=decision.comment)]
    if decision.category == "data_fix":
        return [
            UpdateFieldValue(field_name=u.field_name, value=_coerce_value(u.field_name, u.field_value, context))
            for u in decision._resolved_field_updates()
        ]
    if decision.category == "history_fix":
        if not decision.history_node_id or (decision.history_opinion is None and decision.history_comment is None):
            return []
        return [UpdateApprovalHistoryOpinion(
            node_id=decision.history_node_id, opinion=decision.history_opinion, comment=decision.history_comment,
        )]
    if decision.category == "override_jump":
        return [JumpToNode(target_node_id=decision.target_node_id)] if decision.target_node_id else []
    if decision.category == "override_skip":
        return [SkipCurrentNode()]
    if decision.category == "reassign":
        if not decision.reassign_user_id:
            return []
        if org_data is not None:
            from app.org_knowledge import user_exists

            if not user_exists(org_data, decision.reassign_user_id):
                return []
        return [ReassignApprover(user_id=decision.reassign_user_id)]
    if decision.category == "withdraw":
        return [Withdraw()]
    return []


def _coerce_value(field_name: str, raw: str, context: InstanceContext) -> Any:
    """按字段 component_type 轻量转换（数字字段转 number，其余保持字符串）。"""
    field = next((f for f in context.process.form_fields if f.field_name == field_name), None)
    if field and field.component_type == ComponentType.NUMBER:
        try:
            num = float(raw)
            return int(num) if num.is_integer() else num
        except (TypeError, ValueError):
            return raw
    return raw


def _system_prompt(*, can_submit_decision: bool) -> str:
    submit_decision_block = (
        """- submit_decision：**当前处理人**（就是正在跟你对话的这个人）要对手上这个环节给结论性
  意见（同意/不同意）——这是**办理**，不是 override。你要做的是**读这张单子的实际内容**（表单值、
  金额、天数、材料是否齐全等）+ 适用的规则，判断该推荐同意还是不同意，给 opinion；如果要驳回或
  需要补充材料，把理由/要补什么写进 comment。**目标环节不用你算**——代码会拿你给的 opinion 按
  这个环节真实的提交路径条件（下面"当前环节提交路径"里给你的）确定性求出，你给错 opinion 也不会
  走错地方，最多是条件本身没有唯一匹配、代码会报错重来。用户明确说"同意/批准/通过"就是 opinion=
  同意；说"不同意/驳回/拒绝"或指出材料/内容有问题需要打回，就是 opinion=不同意 + 写清楚 comment。
"""
        if can_submit_decision
        else ""
    )
    priority_rule = (
        "办理优先于排障：只要用户是在对当前环节给结论（同意/不同意/批准/驳回），就该是 "
        "submit_decision，不要因为单子内容有点风险提示就跳去 override 或 deny——风险提示是"
        "给你判断该不该推荐同意的依据，不是必须走排障的信号。"
        if can_submit_decision
        else "当前处理人不是这个环节的结论性意见给出者（比如发起人在查看自己发起的单子进度），"
        "不要输出 submit_decision——那不是这个人该做的动作。"
    )
    return f"""你是企业 OA 流程的在办副驾，帮当前处理人处理手上这条任务——覆盖"办理"（正常给结论、
推进流程）和"排障"（这单卡住/需要破例/数据填错）两类诉求，二者共用同一套决策空间，不用你自己先猜
用户这句话属于哪一类。给你：①这张单子的确定性诊断（为什么卡，如果卡的话）+ 当前环节的真实内容
（表单值、提交路径、条件）；②用户的诉求；③适用的规则。

你要输出一个决策（category）：
{submit_decision_block}- data_fix：**非核心字段**填错（不影响审批结论/金额/路径走向的辅助字段，如联系电话、
  事由说明），改对即可 → 用 field_updates 给出**要改的字段列表**（每项 field_name + field_value）。
  **可以一次改多个字段**，不要因为"要改多个"而反问。改数据不改流程走向、风险低。（发起人点确认即可
  执行，你不用自己在 reply 里要求确认）
- history_fix：用户要订正的是**已经走完的某个环节**当初留下的审批意见记录本身（结论性意见录错了、
  或说明文字要补充/改写）——不是当前环节的办理，也不会重新触发路由，该环节早就放行到下一步，改的
  只是"当时记的这条历史记录"。用 history_node_id 指定要订正哪个环节（下面"历史审批记录"里会给你
  真实存在的 node_id 列表，只能选那里面有的，不能编）；history_opinion / history_comment 按需给，
  只改结论就只给 history_opinion，只改说明文字就只给 history_comment。风险低，发起人/运维确认
  即可执行，不改变流程当前所在环节这一事实。
- withdraw：**核心字段**填错（如请假天数、报销金额、请假类型、开始/结束日期这类会改变审批结论或
  路径判断的字段）、或用户单纯想撤回重填，**且单据当前在部门主管/部门总经理这类非领导层级环节**
  → 撤回到起草，让发起人改完重新走一遍完整审批。风险低，发起人确认即可执行，**跟发起人是不是当前
  环节的处理人无关**——自己发起的单子，任何时候都能提出撤回。
- override_jump / override_skip：破例跳到某个真实环节。**target_node_id 一定填"要跳去的那个环节"，
  不是无脑填起草**——先想清楚跳去哪：
  · **覆盖漏洞并档**（如天数落在无路径区间、卡在部门总经理）——按规则"并入最近的更高一档路径"，
    target 填那个**更高一档的审批环节**（如条线分管领导审批），是往**前**跳，不是退回起草；
  · **用户明确点名要跳到某个真实环节**——就填用户说的那个环节 node_id；
  · **只有"核心字段填错、且已流转到领导层级环节、需推翻重来"**这一种，才 target 填**起草环节**的 node_id。
  下面"全部环节"里标了哪个是起草环节、各环节真实 node_id。这类动流程走向、属高风险，**会自动转
  流程负责人授权**——你只需给出方案 + 引用规则。**你 reply/rationale 里说要跳到哪个环节，target_node_id
  就必须是同一个环节，不许嘴上说 A、动作填 B。**
- reassign：因选不到人（审批人解析为空）卡住、且规则允许改派 → 给 reassign_user_id。（同样转授权）
- deny：用户的诉求被规则**明令禁止**（如要求跳过合规硬性环节）→ 直接拒绝。这是有明确答案的终局，
  不需要升级给人，你在 reply 里说清"按XX规则不能这么做"、rationale 引用该规则。
- escalate：规则**没有覆盖**这个诉求、或你**拿不准**、给不出具体方案 → 升级人工接手。已进入合规
  审核/法务/财务复核等合规硬性环节的撤回诉求也走这里，不自助也不由你自行授权放行。
  （deny 和 escalate 的区别：deny 是"规则说不行"有答案；escalate 是"没规则/不确定"没答案。）
- no_issue：没发现需要处理的异常，且用户也没提出订正诉求。
- clarify：**只在用户诉求确实不清楚时**反问（如没说清要改成什么，或该同意还是不同意都判断不出来，
  或分不清用户说的字段是不是核心字段）。诉求清楚就直接给对应类别，别用 clarify 当"确认"用——系统
  会用结构化的确认按钮让用户确认。

铁律：
- {priority_rule}
- **"对话者是不是当前环节的处理人"只影响能不能给 submit_decision（代处理人下结论），不影响任何
  其他类别**。data_fix / withdraw / override_jump / override_skip / reassign 这些排障类动作，
  只要诉求本身合理（比如发起人要求撤回/订正自己的单子），跟对话者是不是当前处理人无关，不要因为
  "你不是当前环节处理人"就拒绝或转为"只能等处理人操作"——那是误判，会把发起人的正常排障诉求错误
  升级成"你没权限"。
- override/reassign 必须在 rationale 里引用给你的规则，**无规则支撑就 escalate，规则明确禁止就 deny，绝不自己编 override**。
- 只用给你的诊断和规则判断，不臆造流程里没有的环节/字段/人。
- target_node_id 只能是诊断里出现的真实环节 id。
- 提到具体某人一律用他的**中文姓名**（下面信息里已经给你姓名），绝不能把 user_id 这种内部编号
  （如 u_xxx）直接写进 reply——用户看不懂系统内部 id。
- 提到具体环节一律用它的**中文环节名称**（"全部环节"里已经给你名称），绝不能把 node_id 这种
  内部编号（如 line_leader）直接写进 reply/rationale。
- reply/rationale 全篇（包括逐字引用规则原文的部分）不出现"override"这个英文单词，一律替换成
  "破例处置"。例如规则原文是"此为 override，须流程负责人授权"，你在 reply/rationale 里必须写成
  "此为破例处置，须流程负责人授权"——即使外面加了引号表示引用规则原文，引号内的文字也要替换，
  不允许说"规则原文如此，所以照抄"；规则库原始文本本身允许保留英文（那是给别的场景用的原文
  展示），但**你自己输出给用户看的这段话，不能出现这个英文单词**。
- 简洁中文，不用英文双引号。"""


def _user_prompt(
    context: InstanceContext, user_message: str, ops_rules: str, conversation_context: str = "",
    *, can_submit_decision: bool = False, org_data: dict[str, Any] | None = None,
) -> str:
    from app.org_knowledge import user_name

    proc = context.process
    name_of = (lambda uid: user_name(org_data, uid)) if org_data is not None else (lambda uid: uid)
    nodes = "\n".join(
        f"  - {n.node_id} | {n.node_name}"
        + ("（起草环节）" if n.is_draft else "")
        + (f" | 处理角色：{n.handler.role}" if n.handler and n.handler.role else "")
        for n in proc.flow_nodes
    )
    # 枚举类字段（下拉单选/多选、单选按钮）把 options 带上——不然像"印章类型还有什么可选"
    # 这种问题，明明 process 里就有完整选项列表，却因为没拼进 prompt 而答不出来（真实复现过）。
    fields = "\n".join(
        f"  - {f.field_name}（{f.component_type.value}）= {context.form_values.get(f.field_name)!r}"
        + (f"；可选值：{f.options}" if f.options else "")
        for f in proc.form_fields
    )
    blocked = context.blocked_paths()
    blocked_text = "\n".join(f"  - 路径「{p.path_name}」→ {p.target_node_id}：{p.reason}" for p in blocked) or "  （无被卡路径）"
    approver = context.approver_resolution
    approver_text = (
        "无审批环节" if approver is None
        else ("审批人解析为空：" + (approver.reason or "") if approver.is_empty
              else f"审批人已解析：{[name_of(uid) for uid in approver.resolved_user_ids]}")
    )
    stuck = "当前无任何可前进路径（被卡住）" if not context.has_any_matched_path() else "有可前进路径"
    history_records_text = "\n".join(
        f"  - {h.node_id}｜{h.node_name}" + (f"（{n2.opinion_label}）" if (n2 := proc.get_node_by_id(h.node_id)) and n2.opinion_label else "")
        + f" | 审批人：{name_of(h.actor_user_id) if h.actor_user_id else '未知'}"
        + (f" | 结论：{h.opinion}" if h.opinion else "")
        + (f" | 意见内容：{h.comment}" if h.comment else "")
        for h in context.history
    ) or "  （无历史审批记录）"
    history_text = conversation_context.strip() or "（无历史对话）"
    current_node = proc.get_node_by_id(context.current_node_id)
    current_paths = (
        "\n".join(f"  - 「{p.path_name}」条件：{p.condition or '无条件'} → 目标：{p.target_node_id}" for p in current_node.submit_paths)
        if current_node and current_node.submit_paths else "  （当前环节无提交路径）"
    )
    role_note = (
        "你正在跟当前处理人对话，可以给 submit_decision；排障类动作（data_fix/withdraw/override 等）同样可以提。"
        if can_submit_decision
        else "跟你对话的不是当前环节的处理人（比如发起人在查看自己单子的进度），不要给 submit_decision——但"
        "data_fix / withdraw / override_jump 等排障类动作不受此限制，发起人随时可以提撤回/订正自己的单子。"
    )
    return f"""流程：{proc.meta.process_name}
当前环节：{context.current_node_id}（{current_node.node_name if current_node else "未知"}）
发起人：{name_of(context.initiator_user_id)}
诊断结论：{stuck}；{approver_text}
{role_note}

全部环节（标了哪个是起草环节，供 override_jump 撤回到起草时取真实 node_id）：
{nodes}

当前环节提交路径（submit_decision 的目标由代码按这里的条件对你给的 opinion 求值算出，不用你自己指定）：
{current_paths}

当前表单值：
{fields}

被卡路径及原因：
{blocked_text}

历史审批记录（已走完的环节留下的意见；history_fix 订正时 history_node_id 只能填这里出现的 node_id）：
{history_records_text}

适用的规则：
{ops_rules or "（未检索到专门规则）"}

本轮对话历史（供理解"确认""就这个""上面说的"等指代；最后一条才是本次要处理的诉求）：
{history_text}

用户本次诉求：{user_message}

请据此给出决策。"""


class OpsCopilotAgent:
    def __init__(self, model: Any | None = None) -> None:
        self.model = model or create_bedrock_chat_model()
        self.structured_model = (
            None
            if is_legacy_text_generator(self.model)
            else self.model.with_structured_output(OpsDecision, method="function_calling", include_raw=True)
        )

    def decide(
        self, context: InstanceContext, user_message: str, *, ops_rules: str = "", conversation_context: str = "",
        can_submit_decision: bool = False, org_data: dict[str, Any] | None = None,
    ) -> OpsDecision | None:
        if self.structured_model is None:
            return None
        response = self.structured_model.invoke(
            [
                {"role": "system", "content": _system_prompt(can_submit_decision=can_submit_decision)},
                {"role": "user", "content": _user_prompt(
                    context, user_message, ops_rules, conversation_context,
                    can_submit_decision=can_submit_decision, org_data=org_data,
                )},
            ]
        )
        return _coerce(response)


def _coerce(response: Any) -> OpsDecision | None:
    parsed: Any = None
    parsing_error: Any = None
    if isinstance(response, dict):
        parsed = response.get("parsed")
        parsing_error = response.get("parsing_error")
    elif isinstance(response, OpsDecision):
        parsed = response
    else:
        parsed = getattr(response, "parsed", None)
        parsing_error = getattr(response, "parsing_error", None)
    if parsing_error or parsed is None:
        return None
    if not isinstance(parsed, OpsDecision):
        try:
            parsed = OpsDecision.model_validate(parsed)
        except Exception:  # noqa: BLE001
            return None
    return parsed
