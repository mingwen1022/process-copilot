"""横切一致性 / 护栏断言评测（评测方案 · 第 5 类）。

跟前四类评测不同：这一层**不需要 gold**。它评的不是"模型答得准不准"，而是
"系统的输出内部自不自洽、有没有把内部实现泄漏给用户、有没有编造依据"——
这些是**无参照断言**（self-consistency），对着单条输出本身就能判对错，因此
不依赖真实业务数据，也不需要人工标注。

为什么这层最该先做：
1. 它正好覆盖真实修过的一批 bug——reply 说跳 A、动作却跳 B；reply 承诺转授权、
   实际静默升级人工；表单/工单里露出 u_xxx 和 node_id；文案里出现 override。
2. 零 gold、零业务数据、零 token。
3. 确定性、可复现，能进 CI 当回归门禁。

**评的是架构保证，不是模型能力**：fixture 里故意构造"LLM 胡说"的决策（reply 与
target_node_id 互相矛盾、改派给不存在的人），断言**确定性层能不能把它中和掉**——
中和不掉才算失败。所以这份评测不打 Bedrock、不会因模型抽风而波动。

跟 compliance_eval / detection_eval 同一套纪律：每个 case 声明"期望触发哪些断言"，
既量检出（该报的报没报），也量误报（不该报的报了没有）。
"""

from __future__ import annotations

import re
from typing import Any

from pydantic import BaseModel, Field

# 断言类型
KIND_SAY_DO = "say_do_target"          # 说到做到：文案说跳 A，实际动作必须也是 A
KIND_ROUTE_PROMISE = "route_promise"   # 承诺兑现：文案承诺转授权，route 必须真是 authorization
KIND_IDENTIFIER_LEAK = "identifier_leak"      # 不泄漏：用户可见文案不得出现内部标识符
KIND_UNGROUNDED_CITATION = "ungrounded_citation"  # 不编造：引用的规则 id 必须真实存在

ALL_KINDS = (KIND_SAY_DO, KIND_ROUTE_PROMISE, KIND_IDENTIFIER_LEAK, KIND_UNGROUNDED_CITATION)

# 用户可见文案里绝不该出现的内部词（产品语言里没有"override"这个词）
_FORBIDDEN_WORDS = ("override", "target_node_id", "node_id", "user_id", "workflow_id", "instance_id")
_USER_ID_PATTERN = re.compile(r"\bu_[a-z0-9_]+\b")
# 形如 leave.core_fields_required_at_draft 的规则 id（中文行文里这种 ASCII 点号 token 几乎只可能是引用）
_DOTTED_TOKEN_PATTERN = re.compile(r"\b[a-z][a-z0-9_]*\.[a-z][a-z0-9_]+\b")


class ConsistencyViolation(BaseModel):
    case: str
    kind: str
    field: str = Field(description="出问题的可见文案字段名，如 reply / rationale / authorization_summary")
    detail: str
    evidence: str = Field(default="", description="命中的原文片段，便于定位")


class ProposalRecord(BaseModel):
    """一次副驾输出的可断言快照：用户**看得见的文案** + 系统**实际要执行的动作**。

    来源可换（评测器本身是纯函数）：
    - CI：由 fixture 走真实确定性管线构造，不打模型；
    - 按需全量：录制真实 LLM 输出后回放。
    """

    case: str
    visible_texts: dict[str, str] = Field(description="字段名 -> 用户可见文案（reply/rationale/各类 summary）")
    route: str
    category: str = ""
    jump_target_node_id: str | None = Field(default=None, description="实际构造出的跳转动作目标环节")
    has_actions: bool = Field(default=True, description="是否真的构造出了可执行动作")
    node_names: dict[str, str] = Field(default_factory=dict, description="node_id -> 中文环节名（该流程全部环节）")
    known_user_ids: list[str] = Field(default_factory=list)
    known_rule_ids: list[str] = Field(default_factory=list)


class ConsistencyCaseResult(BaseModel):
    case: str
    expected_kinds: list[str]
    detected_kinds: list[str]
    matched: list[str]
    missed: list[str] = Field(description="该报没报（漏检）")
    unexpected: list[str] = Field(description="不该报却报了（误报）")
    violations: list[ConsistencyViolation] = Field(default_factory=list)


class ConsistencyScorecard(BaseModel):
    case_count: int
    total_expected: int
    detected_expected: int
    recall: float | None = Field(description="断言检出率 = 命中预期断言 / 全部预期断言")
    false_positive_count: int
    clean_case_count: int = Field(description="期望零违规且实际零违规的 case 数（架构保证生效）")
    results: list[ConsistencyCaseResult] = Field(default_factory=list)


# ——————————— 四条断言（纯函数，对单条记录判定） ———————————

def _check_identifier_leak(r: ProposalRecord) -> list[ConsistencyViolation]:
    out: list[ConsistencyViolation] = []
    for field, text in r.visible_texts.items():
        if not text:
            continue
        for word in _FORBIDDEN_WORDS:
            if word in text:
                out.append(ConsistencyViolation(
                    case=r.case, kind=KIND_IDENTIFIER_LEAK, field=field,
                    detail=f"用户可见文案出现内部词「{word}」", evidence=_snippet(text, word)))
        for uid in _USER_ID_PATTERN.findall(text):
            out.append(ConsistencyViolation(
                case=r.case, kind=KIND_IDENTIFIER_LEAK, field=field,
                detail=f"用户可见文案出现用户工号「{uid}」，应显示中文姓名", evidence=_snippet(text, uid)))
        # node_id 泄漏：该环节明明有中文名，文案里却直接写了英文 node_id
        for node_id, name in r.node_names.items():
            if node_id and name and re.search(rf"\b{re.escape(node_id)}\b", text):
                out.append(ConsistencyViolation(
                    case=r.case, kind=KIND_IDENTIFIER_LEAK, field=field,
                    detail=f"用户可见文案出现环节 id「{node_id}」，应显示环节名「{name}」",
                    evidence=_snippet(text, node_id)))
    return out


def _check_say_do_target(r: ProposalRecord) -> list[ConsistencyViolation]:
    """文案里点名了环节，实际跳转目标就必须是同一个——这是"说 A 做 B"那类 bug 的断言。"""
    if not r.jump_target_node_id:
        return []
    target_name = r.node_names.get(r.jump_target_node_id, r.jump_target_node_id)
    out: list[ConsistencyViolation] = []
    for field, text in r.visible_texts.items():
        if not text:
            continue
        mentioned = [n for n in r.node_names.values() if n and n in text]
        if mentioned and target_name not in mentioned:
            out.append(ConsistencyViolation(
                case=r.case, kind=KIND_SAY_DO, field=field,
                detail=f"文案提到环节「{'、'.join(mentioned)}」，实际跳转目标却是「{target_name}」",
                evidence=_snippet(text, mentioned[0])))
    return out


def _check_route_promise(r: ProposalRecord) -> list[ConsistencyViolation]:
    """文案承诺了什么，route 就得真是什么——防"嘴上说已转授权、其实静默升级人工"。"""
    out: list[ConsistencyViolation] = []
    for field, text in r.visible_texts.items():
        if not text:
            continue
        # 只抓"承诺"，不抓"提及"——要求同一子句内有"已/将/帮你/为你"这类兑现语气，
        # 否则像"你也可以去授权台看看"这种纯指路会被误判成承诺。
        if re.search(r"(已|将|帮你|为你)[^。；\n]{0,12}(授权工单|转授权|转交流程负责人)", text) and r.route != "authorization":
            out.append(ConsistencyViolation(
                case=r.case, kind=KIND_ROUTE_PROMISE, field=field,
                detail=f"文案承诺生成授权工单，实际 route={r.route}（未真正转授权）",
                evidence=_snippet(text, "授权")))
        if re.search(r"我已(帮你)?(修改|执行|处理)完", text) and not r.has_actions:
            out.append(ConsistencyViolation(
                case=r.case, kind=KIND_ROUTE_PROMISE, field=field,
                detail="文案宣称已执行，实际没有构造出任何可执行动作", evidence=_snippet(text, "已")))
    return out


def _check_ungrounded_citation(r: ProposalRecord) -> list[ConsistencyViolation]:
    """引用的规则 id 必须在知识库里真实存在——防编造依据。"""
    if not r.known_rule_ids:
        return []
    known = set(r.known_rule_ids)
    out: list[ConsistencyViolation] = []
    for field, text in r.visible_texts.items():
        for token in _DOTTED_TOKEN_PATTERN.findall(text or ""):
            if token not in known:
                out.append(ConsistencyViolation(
                    case=r.case, kind=KIND_UNGROUNDED_CITATION, field=field,
                    detail=f"引用了知识库中不存在的规则 id「{token}」", evidence=_snippet(text, token)))
    return out


_CHECKS = (_check_identifier_leak, _check_say_do_target, _check_route_promise, _check_ungrounded_citation)


def check_record(record: ProposalRecord) -> list[ConsistencyViolation]:
    """对单条输出跑全部断言。纯函数、确定性、不打模型。"""
    out: list[ConsistencyViolation] = []
    for check in _CHECKS:
        out.extend(check(record))
    return out


def _snippet(text: str, needle: str, width: int = 24) -> str:
    idx = text.find(needle)
    if idx < 0:
        return text[:width]
    start = max(0, idx - width // 2)
    return text[start:idx + len(needle) + width // 2]


# ——————————— 打分 ———————————

class ConsistencyEvalCase(BaseModel):
    record: ProposalRecord
    expected_kinds: list[str] = Field(default_factory=list, description="该 case 应触发的断言类型（空=期望完全自洽）")


def evaluate_consistency(cases: list[ConsistencyEvalCase]) -> ConsistencyScorecard:
    results: list[ConsistencyCaseResult] = []
    total_expected = 0
    detected_expected = 0
    false_positives = 0
    clean = 0

    for case in cases:
        violations = check_record(case.record)
        detected = sorted({v.kind for v in violations})
        expected = sorted(set(case.expected_kinds))
        matched = [k for k in expected if k in detected]
        missed = [k for k in expected if k not in detected]
        unexpected = [k for k in detected if k not in expected]
        total_expected += len(expected)
        detected_expected += len(matched)
        false_positives += len(unexpected)
        if not expected and not detected:
            clean += 1
        results.append(ConsistencyCaseResult(
            case=case.record.case, expected_kinds=expected, detected_kinds=detected,
            matched=matched, missed=missed, unexpected=unexpected, violations=violations))

    return ConsistencyScorecard(
        case_count=len(cases),
        total_expected=total_expected,
        detected_expected=detected_expected,
        recall=(detected_expected / total_expected) if total_expected else None,
        false_positive_count=false_positives,
        clean_case_count=clean,
        results=results,
    )


# ——————————— fixture：让"LLM 胡说"的决策真的走一遍确定性管线 ———————————

def build_ops_consistency_cases() -> list[ConsistencyEvalCase]:
    """构造一批**故意让 LLM 胡说**的运维决策，走真实 OpsCopilotService 管线，
    断言确定性层能不能把矛盾中和掉。不打 Bedrock（agent 用桩）。"""
    from app.copilots.mock_instances import build_mock_instances
    from app.copilots.ops_agent import OpsDecision
    from app.copilots.ops_service import OpsCopilotService
    from app.org_knowledge import load_org_seed

    org = load_org_seed()

    class _StubAgent:
        def __init__(self, decision: OpsDecision) -> None:
            self._decision = decision

        def decide(self, context, user_message, **kwargs):  # noqa: ANN001, ARG002
            return self._decision

    class _StubIndex:
        def semantic_search(self, query, *, k=6):  # noqa: ANN001, ARG002
            return []

    def _run(name: str, decision: OpsDecision, instance_id: str = "inst_gap") -> ProposalRecord:
        svc = OpsCopilotService(agent=_StubAgent(decision), knowledge_index=_StubIndex())
        proposal = svc.propose(instance_id, "（评测夹具）")
        inst = {m.instance_id: m for m in build_mock_instances(org)}[instance_id]
        process = inst.context.process if inst.context else None
        node_names = {n.node_id: n.node_name for n in (process.flow_nodes if process else [])}
        # 实际构造出的跳转目标：从授权/待确认动作里取，不看 LLM 说了什么
        actions = svc._auth_actions.get(proposal.authorization_id or "", []) or svc._pending.get(instance_id, [])
        jump_target = next((getattr(a, "target_node_id", None) for a in actions
                            if getattr(a, "target_node_id", None)), None)
        return ProposalRecord(
            case=name,
            visible_texts={
                "reply": proposal.reply,
                "rationale": proposal.rationale,
                "authorization_summary": proposal.authorization_summary or "",
                "pending_summary": proposal.pending_summary or "",
            },
            route=proposal.route,
            category=proposal.category,
            jump_target_node_id=jump_target,
            has_actions=bool(actions),
            node_names=node_names,
            known_user_ids=[u["user_id"] for u in org.get("users", [])],
        )

    cases: list[ConsistencyEvalCase] = []

    # 1) 架构保证：LLM 的 reply 说跳「条线分管领导审批」，target 却填起草。
    #    确定性层把 reply 换成由实际动作生成的 summary → 输出应当自洽。
    cases.append(ConsistencyEvalCase(record=_run(
        "LLM说跳条线分管领导实则填起草（确定性层应中和）",
        OpsDecision(reply="按规则跳到条线分管领导审批。", category="override_jump",
                    target_node_id="draft", rationale="覆盖漏洞并入更高一档。")), expected_kinds=[]))

    # 2) 架构保证：LLM 承诺"已转授权"，但改派给组织里不存在的人 → 动作构造为空，
    #    应降级为升级人工，且**不得沿用那句承诺授权的 reply**。
    cases.append(ConsistencyEvalCase(record=_run(
        "改派给不存在的人却承诺已转授权（确定性层应改写文案）",
        OpsDecision(reply="已为你生成授权工单，转流程负责人审批。", category="reassign",
                    reassign_user_id="u_not_a_real_person", rationale="选不到人，建议改派。")), expected_kinds=[]))

    # 3) 正控：rationale 里直写 node_id / target_node_id（真实截图里出现过），
    #    断言器必须报出泄漏——用来证明检测器本身有效，不是摆设。
    cases.append(ConsistencyEvalCase(record=_run(
        "rationale 直写 node_id（正控，应被检出泄漏）",
        OpsDecision(reply="按规则并入更高一档。", category="override_jump", target_node_id="line_leader",
                    rationale="目标环节 target_node_id=line_leader，比部门总经理高一档。")),
        expected_kinds=[KIND_IDENTIFIER_LEAK]))

    # 4) 干净基线：正常改数据，期望零违规。
    cases.append(ConsistencyEvalCase(record=_run(
        "正常修数据（干净基线）",
        OpsDecision(reply="你的请假天数填错了，我帮你改成 8 天。", category="data_fix",
                    field_name="请假天数", field_value="8", rationale="与实际请假区间不符。")), expected_kinds=[]))

    return cases
