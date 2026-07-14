"""规则合规校验（模块3 · Phase 3 · 确定性部分）。

拿原子规则查 ProcessDefinition：确定性规则（有限的 CheckKind）由代码逐条查、
违规即报并引用规则子句（provenance）；定性规则（llm_judge）不在这里判，交给
compliance_judge_agent。

设计时口径：规则的 applicability_condition 描述"什么情况下这条要求生效"，但设计
校验查的是**流程定义有没有为此提供机制**（如附件设了 required_condition、升级
环节存在），而不是对某个具体案例求值——那是运行时/分析侧 conformance 的事。
所以确定性检查只看 ProcessDefinition 结构，不需要案例属性。
"""

from __future__ import annotations

from typing import Callable

from pydantic import BaseModel, Field

from app.runtime.condition_eval import evaluate_condition
from data.schema import AtomicRule, CheckKind, ProcessDefinition, RuleDimension

# 从流程内容推断业务域——真实设计草稿会被分配一个生成的 process_id（不等于
# LEAVE-001 这种基准 id），所以适用规则不能只靠 process_id 精确匹配，要按内容
# 推断域。设计规范类规则对所有流程普适、不需域匹配（见 _select_applicable_rules）。
_DOMAIN_KEYWORDS: dict[str, list[str]] = {
    # "hr" 域曾用 ["请假","考勤","员工","交接"] 做关键词推断，但"员工"/"交接"太通用，
    # 报销、用印等流程描述里提"员工"/字段名带"交接"就会误命中，把请假专属规则(leave.*)
    # 错配进无关流程。leave.* 规则已用更精确的 "leave" 域标注，"hr" 域对它们是冗余项，
    # 直接去掉，不再靠这类宽泛关键词做域推断。
    "leave": ["请假", "年假", "病假", "事假", "婚假", "产假"],
    "governance": ["子公司", "重大事项", "备案", "高管"],
    "countersign": ["会签"],
    # "金额" 曾在这里，但任何带金额字段的流程（采购金额、合同金额…）都会命中，导致
    # auth.finance_head_for_large_expense（一条专属"大额报销"的规则）误配到采购等
    # 无关流程。去掉"金额"；"finance" 域仍保留，供规则 RAG 按"财务"主题广度检索用。
    "finance": ["费用", "报销", "财务"],
    "expense": ["报销", "费用"],
    "seal": ["印章", "用印", "盖章", "合同章", "公章"],
    "procurement": ["采购"],
}


def infer_domains(process: ProcessDefinition) -> set[str]:
    text = " ".join(
        [process.meta.process_name, process.meta.description]
        + [f.field_name for f in process.form_fields]
        + [n.node_name for n in process.flow_nodes]
    )
    return {domain for domain, kws in _DOMAIN_KEYWORDS.items() if any(kw in text for kw in kws)}


def _select_applicable_rules(
    all_rules: list[AtomicRule], process: ProcessDefinition, process_id: str | None, extra_domains: set[str] | None
) -> list[AtomicRule]:
    domains = (extra_domains or set()) | infer_domains(process)
    pid = process_id or process.meta.process_id
    selected: list[AtomicRule] = []
    for rule in all_rules:
        if rule.dimension == RuleDimension.DESIGN_STANDARD:  # 设计规范普适
            selected.append(rule)
        elif pid in rule.applies_to_processes:
            selected.append(rule)
        elif domains & set(rule.applies_to_domains):
            selected.append(rule)
    return selected


class ComplianceFinding(BaseModel):
    rule_id: str
    severity: str
    statement: str = Field(description="被违反的规则表述")
    source_doc: str
    clause: str = Field(description="规则出处子句，用于引用")
    detail: str = Field(description="该流程具体哪里不满足")


def _has_draft_and_end(rule: AtomicRule, process: ProcessDefinition) -> ComplianceFinding | None:
    has_draft = any(node.is_draft for node in process.flow_nodes)
    has_end = any(path.target_node_id == "END" for node in process.flow_nodes for path in node.submit_paths)
    if has_draft and has_end:
        return None
    missing = []
    if not has_draft:
        missing.append("起草环节")
    if not has_end:
        missing.append("通向流程结束（END）的路径")
    return _finding(rule, f"流程缺少{'、'.join(missing)}")


def _must_have_node(rule: AtomicRule, process: ProcessDefinition) -> ComplianceFinding | None:
    role_kws = rule.requirement.params.get("role_keywords", [])
    name_kws = rule.requirement.params.get("name_keywords", [])
    for node in process.flow_nodes:
        role = (node.handler.role if node.handler else "") or ""
        if any(kw in role for kw in role_kws) or any(kw in node.node_name for kw in name_kws):
            return None
    want = "、".join(dict.fromkeys([*role_kws, *name_kws]))
    return _finding(rule, f"流程缺少应有的审批环节（应包含处理角色/环节：{want}）")


def _must_route_through_node(rule: AtomicRule, process: ProcessDefinition) -> ComplianceFinding | None:
    """条件可达性：某类高风险情形（如 印章类型=公章）必须流经某个"必经环节"（如法务/合规），
    不能被改道绕过。比 must_have_node 强——后者只看该环节"存在"，管不住"环节还在、但把
    高风险案子改条件绕开它"这种规避（真实踩过：agent 加一条 印章类型=公章→直送行政审批 的
    路由，法务节点仍在，存在性检查放行，但公章实际不再过法务）。

    做法：对每个高风险情形（case_contexts 里各给一份上下文），用运行时同一套 condition_eval
    在流程图上模拟——从起草出发，只走该上下文下条件成立、且不进入"必经环节"的边，若仍能走到
    结束（END），说明这类案子存在绕过必经环节的完成路径 → 违规。找不到必经环节则退化为
    must_have_node 语义（连节点都没有，直接违规）。
    """
    params = rule.requirement.params
    role_kws = params.get("role_keywords", [])
    name_kws = params.get("name_keywords", [])
    case_contexts: list[dict] = params.get("case_contexts") or [{}]
    approval_context: dict = params.get("approval_context") or {}

    gate_ids = {
        node.node_id
        for node in process.flow_nodes
        if any(kw in ((node.handler.role if node.handler else "") or "") for kw in role_kws)
        or any(kw in node.node_name for kw in name_kws)
    }
    want = "、".join(dict.fromkeys([*role_kws, *name_kws]))
    if not gate_ids:  # 连必经环节都不存在——比"被绕过"更严重，直接按缺失报
        return _finding(rule, f"流程缺少应有的必经环节（应包含处理角色/环节：{want}）")

    start = next((n for n in process.flow_nodes if n.is_draft), None)
    if start is None:
        start = process.flow_nodes[0] if process.flow_nodes else None
    if start is None:
        return None

    for case in case_contexts:
        ctx = {**approval_context, **case}
        if _can_reach_end_avoiding_gate(process, start.node_id, gate_ids, ctx):
            desc = "、".join(f"{k}={v}" for k, v in case.items()) or "该情形"
            return _finding(
                rule,
                f"高风险情形（{desc}）存在绕过必经环节的路径：可不经过“{want}”直达用印/结束",
            )
    return None


def _can_reach_end_avoiding_gate(
    process: ProcessDefinition, start_id: str, gate_ids: set[str], ctx: dict
) -> bool:
    """从 start 出发，只沿 ctx 下条件成立的提交路径前进，且把通向"必经环节(gate)"的边剪掉——
    若这样仍能到达 END，则该情形存在一条绕过 gate 的完成路径。退回起草(DRAFT)不算"完成"，剪掉。"""
    from collections import deque

    node_by_id = {n.node_id: n for n in process.flow_nodes}
    visited: set[str] = set()
    queue: deque[str] = deque([start_id])
    while queue:
        nid = queue.popleft()
        if nid in visited:
            continue
        visited.add(nid)
        node = node_by_id.get(nid)
        if node is None:
            continue
        for path in node.submit_paths:
            if not evaluate_condition(path.condition, ctx).matched:
                continue  # 该情形下这条边走不通
            tgt = path.target_node_id
            if tgt == "END":
                return True  # 到达结束，且沿途没进任何 gate（进 gate 的边下面被剪了）
            if tgt in gate_ids or tgt == "DRAFT":
                continue  # 经过必经环节的分支合规、退回起草非完成——都不算绕过路径
            queue.append(tgt)
    return False


def _attachment_required_condition(rule: AtomicRule, process: ProcessDefinition) -> ComplianceFinding | None:
    att_kws = rule.requirement.params.get("attachment_keywords", [])
    cond_kws = rule.requirement.params.get("condition_keywords", [])
    matched = [
        a for a in (process.attachments or [])
        if any(kw in a.attachment_type for kw in att_kws)
    ]
    if not matched:
        return _finding(rule, f"流程缺少应按条件必传的附件（关键词：{'、'.join(att_kws)}）")
    for att in matched:
        cond = att.required_condition or ""
        if cond and all(kw in cond for kw in cond_kws):
            return None
    return _finding(
        rule,
        f"附件“{matched[0].attachment_type}”未设置按业务条件必传（required_condition 缺失或不含条件 {'、'.join(cond_kws)}）",
    )


def _field_required_at_draft(rule: AtomicRule, process: ProcessDefinition) -> ComplianceFinding | None:
    field_kws = rule.requirement.params.get("field_keywords", [])
    missing: list[str] = []
    for kw in field_kws:
        field = next((f for f in process.form_fields if kw in f.field_name), None)
        if field is None:
            missing.append(f"{kw}（字段缺失）")
        elif "draft" not in field.required_stages and "all" not in field.required_stages:
            missing.append(f"{kw}（未在起草环节必填）")
    if not missing:
        return None
    return _finding(rule, f"以下字段未满足起草必填：{'；'.join(missing)}")


def _handler_mode_for_node(rule: AtomicRule, process: ProcessDefinition) -> ComplianceFinding | None:
    node_kws = rule.requirement.params.get("node_keywords", [])
    mode_kws = rule.requirement.params.get("mode_keywords", [])
    offenders: list[str] = []
    for node in process.flow_nodes:
        if not any(kw in node.node_name for kw in node_kws):
            continue
        mode = (node.handler.mode.value if node.handler and node.handler.mode else "") or ""
        if not any(kw in mode for kw in mode_kws):
            offenders.append(f"{node.node_name}（当前：{mode or '未设置'}）")
    if not offenders:
        return None
    return _finding(rule, f"以下环节处理人方式应为“{'/'.join(mode_kws)}”：{'；'.join(offenders)}")


def _readonly_autofill_field(rule: AtomicRule, process: ProcessDefinition) -> ComplianceFinding | None:
    field_kws = rule.requirement.params.get("field_keywords", [])
    offenders: list[str] = []
    for kw in field_kws:
        field = next((f for f in process.form_fields if kw in f.field_name), None)
        if field is not None and field.component_type.value != "只读文本":
            offenders.append(f"{field.field_name}（当前：{field.component_type.value}）")
    if not offenders:
        return None
    return _finding(rule, f"以下自动带出字段应为只读文本：{'；'.join(offenders)}")


_CHECKERS: dict[CheckKind, Callable[[AtomicRule, ProcessDefinition], ComplianceFinding | None]] = {
    CheckKind.HAS_DRAFT_AND_END: _has_draft_and_end,
    CheckKind.MUST_HAVE_NODE: _must_have_node,
    CheckKind.MUST_ROUTE_THROUGH_NODE: _must_route_through_node,
    CheckKind.ATTACHMENT_REQUIRED_CONDITION: _attachment_required_condition,
    CheckKind.FIELD_REQUIRED_AT_DRAFT: _field_required_at_draft,
    CheckKind.HANDLER_MODE_FOR_NODE: _handler_mode_for_node,
    CheckKind.READONLY_AUTOFILL_FIELD: _readonly_autofill_field,
}


def _finding(rule: AtomicRule, detail: str) -> ComplianceFinding:
    return ComplianceFinding(
        rule_id=rule.rule_id,
        severity=rule.severity,
        statement=rule.statement,
        source_doc=rule.provenance.source_doc,
        clause=rule.provenance.clause,
        detail=detail,
    )


def check_deterministic_compliance(process: ProcessDefinition, rules: list[AtomicRule]) -> list[ComplianceFinding]:
    """对给定的（已按适用性筛过的）规则集，跑所有确定性检查，返回违规发现。"""
    findings: list[ComplianceFinding] = []
    severity_rank = {"high": 0, "medium": 1}
    for rule in rules:
        if rule.check_type != "deterministic" or rule.requirement is None:
            continue
        checker = _CHECKERS.get(rule.requirement.kind)
        if checker is None:
            continue
        finding = checker(rule, process)
        if finding is not None:
            findings.append(finding)
    findings.sort(key=lambda f: severity_rank.get(f.severity, 9))
    return findings


class ComplianceReport(BaseModel):
    process_name: str
    applicable_rule_count: int
    deterministic_findings: list[ComplianceFinding] = Field(default_factory=list)
    qualitative_findings: list[ComplianceFinding] = Field(default_factory=list)
    llm_available: bool = True

    @property
    def total_findings(self) -> int:
        return len(self.deterministic_findings) + len(self.qualitative_findings)


def deterministic_findings_for_draft(
    process: ProcessDefinition,
    *,
    process_id: str | None = None,
    domains: set[str] | None = None,
) -> list[ComplianceFinding]:
    """只跑确定性规则、不建 LLM-judge（不打 Bedrock）——供对话式编辑链路每轮
    增量提醒用：编辑一次、快速查一次，不能让每句话都多等一次 LLM 调用。"""
    from app.rag.rules import load_atomic_rules

    applicable = _select_applicable_rules(load_atomic_rules(), process, process_id, domains)
    deterministic_rules = [r for r in applicable if r.check_type == "deterministic"]
    return check_deterministic_compliance(process, deterministic_rules)


def check_compliance(
    process: ProcessDefinition,
    *,
    process_id: str | None = None,
    domains: set[str] | None = None,
    rules: list[AtomicRule] | None = None,
    judge: object | None = None,
) -> ComplianceReport:
    """完整合规校验：结构化预筛适用规则 → 确定性检查 + 定性规则 LLM-judge。

    rules 缺省从知识库加载并按 process_id/domains 预筛；judge 为定性规则的
    ComplianceJudgeAgent（可注入；None 且有定性规则时惰性构造，无定性规则则不建）。
    """
    from app.rag.rules import load_atomic_rules

    if rules is None:
        rules = _select_applicable_rules(load_atomic_rules(), process, process_id, domains)

    deterministic = check_deterministic_compliance(process, rules)

    qualitative_rules = [r for r in rules if r.check_type == "llm_judge"]
    qualitative_findings: list[ComplianceFinding] = []
    llm_available = True
    if qualitative_rules:
        if judge is None:
            from app.agents.compliance_judge_agent import ComplianceJudgeAgent

            judge = ComplianceJudgeAgent()
        verdicts = judge.judge(process, qualitative_rules)
        llm_available = bool(verdicts) or _judge_available(judge)
        rule_by_id = {r.rule_id: r for r in qualitative_rules}
        for verdict in verdicts:
            if verdict.violations and verdict.rule_id in rule_by_id:
                qualitative_findings.append(_finding(rule_by_id[verdict.rule_id], _format_violations(verdict.violations)))

    return ComplianceReport(
        process_name=process.meta.process_name,
        applicable_rule_count=len(rules),
        deterministic_findings=deterministic,
        qualitative_findings=qualitative_findings,
        llm_available=llm_available,
    )


def _judge_available(judge: object) -> bool:
    return getattr(judge, "structured_model", True) is not None


def _rule_governed_terms(rule: AtomicRule) -> set[str]:
    """规则'管辖'哪些词——优先用显式的 governs 标注；没标注时回退到 requirement.params
    里的各种 *_keywords 兜底（大部分确定性规则本来就声明了它关心哪些角色/字段/条件词）。"""
    if rule.governs:
        return {t for t in rule.governs if t}
    terms: set[str] = set()
    params = (rule.requirement.params if rule.requirement else None) or {}
    for value in params.values():
        if isinstance(value, list):
            terms.update(str(v) for v in value if v)
    return terms


def _edit_touched_surface(diff: dict[str, Any]) -> set[str]:
    """这次编辑'碰过'的所有文本——改动过的字段名/环节名/路径名、以及改动的条件/取值文本。
    主动提醒就靠它跟规则管辖词做子串匹配：只提醒真被这次编辑碰到的规则，不是把所有适用
    规则每次都刷出来。"""
    surface: set[str] = set()

    def _add(value: Any) -> None:
        if isinstance(value, str) and value.strip():
            surface.add(value)
        elif isinstance(value, list):
            for item in value:
                _add(item)

    def _collect_named_list(section: dict[str, Any]) -> None:
        for name in (section.get("added") or []):
            _add(name)
        for name in (section.get("removed") or []):
            _add(name)
        for item in (section.get("changed") or []):
            _add(item.get("key"))
            for change in (item.get("changes") or []):
                _add(change.get("before"))
                _add(change.get("after"))
            nested = item.get("submit_paths")
            if isinstance(nested, dict):
                _collect_named_list(nested)

    for section_key in ("form_fields", "flow_nodes", "attachments", "roles"):
        section = diff.get(section_key)
        if isinstance(section, dict):
            _collect_named_list(section)
    for change in (diff.get("meta") or []):
        _add(change.get("before"))
        _add(change.get("after"))
    return surface


def rules_touched_by_edit(rules: list[AtomicRule], diff: dict[str, Any]) -> list[AtomicRule]:
    """主动提醒：这次编辑碰到了哪些制度规则——纯确定性子串匹配，不判违规、不打 LLM。
    设计规范类（命名/只读这种格式规则）不进提醒——它们是普适格式约束、不是'碰到某条业务
    制度'的那种值得提醒的东西，另有合规检查/命名 judge 管，混进来只会刷屏。"""
    surface = _edit_touched_surface(diff)
    if not surface:
        return []
    touched: list[AtomicRule] = []
    for rule in rules:
        if rule.dimension == RuleDimension.DESIGN_STANDARD:
            continue
        terms = _rule_governed_terms(rule)
        # 只认"规则管辖的词出现在这次改动过的文本里"这一个方向——反过来（改动文本是管辖词的
        # 子串）会被"会""同意"这种短片段引发误命中，不要。
        if any(term in s for term in terms for s in surface):
            touched.append(rule)
    return touched


def _diff_may_trigger_qualitative_rule(rule: AtomicRule, diff: dict[str, Any]) -> bool:
    """智能门控：这次编辑的 diff 值不值得为这条定性规则打一次 LLM-judge——不是每次编辑
    都判所有定性规则（那样命名规范、回避这类规则会让改处理期限这种毫不相关的编辑也
    平白多等一次 LLM），只在 diff 真的碰到这条规则关心的地盘时才判。"""
    flow_diff = diff.get("flow_nodes") or {}
    if flow_diff.get("added") or flow_diff.get("removed"):
        return True  # 环节增删总是可能影响命名/相邻关系，不受 trigger_attributes 限制
    trigger_attrs = set(rule.trigger_attributes)
    if not trigger_attrs:
        return True  # 没声明触发条件时保守起见照判，不敢随便跳过
    for changed_node in flow_diff.get("changed") or []:
        for change in changed_node.get("changes") or []:
            if change.get("attribute") in trigger_attrs:
                return True
        if "submit_paths" in trigger_attrs:
            paths_diff = changed_node.get("submit_paths") or {}
            if paths_diff.get("added") or paths_diff.get("removed") or paths_diff.get("changed"):
                return True
    return False


def new_qualitative_findings_for_edit(
    *,
    before: ProcessDefinition,
    after: ProcessDefinition,
    diff: dict[str, Any],
    process_id: str | None = None,
    domains: set[str] | None = None,
    rules: list[AtomicRule] | None = None,
    judge: object | None = None,
) -> list[ComplianceFinding]:
    """编辑门里查定性规则（LLM-judge）版的"这次新引入的违规"——跟已有的确定性版
    （generate_draft 里的 findings_before/after 取差集）同一个语义：只报"这次编辑新
    造成某个环节从合规变成不合规"的，草稿本来就有的老问题（哪怕跟这条规则同名）不算
    这次编辑的账。

    这里必须按**环节**（RuleVerdict.violations 里的 node_id）取差集，不能只看整条规则
    一个 compliant 布尔值——真实踩过的 bug：一条流程本来就有好几个环节命名不规范，这次
    编辑只是让另一个本来合规的环节也变得不规范，若只比"整条规则改前/改后是否合规"，
    改前改后都是「不合规」（老问题一直在），会把这次新引入的问题错误地当成"不是新的"
    而放过。必须比"改前这个具体环节在不在违规列表里"。

    智能门控：先用 diff 做便宜的确定性预判，只有真被碰到的定性规则才值得为它调用
    LLM——命中的规则集合为空时直接返回 []，不打一次 LLM。命中时对 before/after 各
    判一次（只判命中的这几条，不是全部定性规则）。

    rules 缺省从知识库加载并按 process_id/domains 预筛（跟 check_compliance 同一个
    参数，可注入方便测试，不用每次都依赖知识库文件当前内容）。
    """
    if rules is None:
        from app.rag.rules import load_atomic_rules

        rules = _select_applicable_rules(load_atomic_rules(), after, process_id, domains)
    triggered = [r for r in rules if r.check_type == "llm_judge" and _diff_may_trigger_qualitative_rule(r, diff)]
    if not triggered:
        return []

    if judge is None:
        from app.agents.compliance_judge_agent import ComplianceJudgeAgent

        judge = ComplianceJudgeAgent()

    before_violation_nodes = {
        v.rule_id: {item.node_id for item in v.violations} for v in judge.judge(before, triggered)
    }
    after_verdicts = judge.judge(after, triggered)
    rule_by_id = {r.rule_id: r for r in triggered}

    findings: list[ComplianceFinding] = []
    for verdict in after_verdicts:
        if not verdict.violations or verdict.rule_id not in rule_by_id:
            continue
        already_violating = before_violation_nodes.get(verdict.rule_id, set())  # 没判到这条规则=之前没有任何违规环节
        new_items = [item for item in verdict.violations if item.node_id not in already_violating]
        if new_items:
            findings.append(_finding(rule_by_id[verdict.rule_id], _format_violations(new_items)))
    return findings


def _format_violations(items: list[Any]) -> str:
    return "；".join(f"「{item.node_id}」{item.detail}" for item in items)
