from __future__ import annotations

from app.io_utils import load_process_definition
from app.rag.compliance import (
    check_compliance,
    check_deterministic_compliance,
    deterministic_findings_for_draft,
    infer_domains,
    new_qualitative_findings_for_edit,
)
from app.rag.rules import load_atomic_rules, rules_for_process
from app.reporting.process_diff import diff_process_definitions
from data.schema import AtomicRule, CheckKind, ProcessDefinition, RuleProvenance, RuleRequirement

LEAVE = "data/cases/leave_request/standard/target.json"


def _rule(kind: CheckKind, params: dict, rule_id="r", severity="high") -> AtomicRule:
    return AtomicRule.model_validate({
        "rule_id": rule_id,
        "dimension": "company_policy",
        "statement": "测试规则",
        "applies_to_processes": ["LEAVE-001"],
        "applies_to_domains": ["leave"],
        "applicability_condition": None,
        "check_type": "deterministic",
        "requirement": {"kind": kind.value, "params": params},
        "provenance": {"source_doc": "leave_management_policy", "clause": "x"},
        "severity": severity,
    })


def _proc(nodes, fields=None, attachments=None) -> ProcessDefinition:
    return ProcessDefinition.model_validate({
        "meta": {"process_id": "LEAVE-001", "process_name": "测试流程", "version": "V1", "responsible_dept": "d", "description": "x", "applicant_scope": "全员", "entry_point": "OA"},
        "form_fields": fields or [],
        "flow_nodes": nodes,
        "attachments": attachments,
        "roles": None,
    })


def _node(node_id, name, is_draft=False, role=None, mode=None, paths=None):
    handler = None if role is None else {"mode": mode or "单选-单人处理", "source": "本部门", "role": role, "source_field": None}
    return {"node_id": node_id, "node_name": name, "is_draft": is_draft, "handler": handler, "opinion": None,
            "opinion_label": None, "time_limit_days": None, "submit_paths": paths or []}


def _field(name, component="单行文本", required=None):
    return {"seq": 1, "field_name": name, "required_stages": required or [], "visible_stages": ["all"],
            "editable_stages": ["draft"], "component_type": component, "logic_description": None,
            "default_value": None, "options": None, "placeholder": None, "max_length": None}


def test_has_draft_and_end_pass_and_fail() -> None:
    rule = _rule(CheckKind.HAS_DRAFT_AND_END, {})
    ok = _proc([_node("draft", "起草", is_draft=True, paths=[{"path_name": "结束", "condition": None, "target_node_id": "END"}])])
    assert check_deterministic_compliance(ok, [rule]) == []
    bad = _proc([_node("draft", "起草", is_draft=True, paths=[{"path_name": "送审", "condition": None, "target_node_id": "a"}])])
    findings = check_deterministic_compliance(bad, [rule])
    assert len(findings) == 1 and "END" in findings[0].detail


def test_must_have_node_by_role_or_name() -> None:
    rule = _rule(CheckKind.MUST_HAVE_NODE, {"role_keywords": ["条线分管领导"], "name_keywords": ["条线分管领导"]})
    ok = _proc([_node("ll", "条线分管领导审批", role="条线分管领导")])
    assert check_deterministic_compliance(ok, [rule]) == []
    bad = _proc([_node("gm", "部门总经理审批", role="部门总经理")])
    assert len(check_deterministic_compliance(bad, [rule])) == 1


def _seal_rule() -> AtomicRule:
    return _rule(
        CheckKind.MUST_ROUTE_THROUGH_NODE,
        {
            "role_keywords": ["合规", "法务", "法律"],
            "name_keywords": ["合规", "法务", "法律"],
            "case_contexts": [{"印章类型": "公章"}, {"印章类型": "合同章"}],
            "approval_context": {"结论性意见": "同意"},
        },
    )


def _seal_nodes(draft_paths):
    """用印流程骨架：起草→(draft_paths)→法务/行政→印章执行→END。draft_paths 决定公章怎么走。"""
    return [
        _node("draft", "起草", is_draft=True, paths=draft_paths),
        _node("legal_review", "法务审核", role="法务审核", paths=[
            {"path_name": "送行政审批", "condition": "结论性意见=同意", "target_node_id": "admin_approve"},
            {"path_name": "退回起草", "condition": "结论性意见=不同意", "target_node_id": "DRAFT"},
        ]),
        _node("admin_approve", "行政审批", role="行政审批", paths=[
            {"path_name": "送印章执行", "condition": "结论性意见=同意", "target_node_id": "seal_execute"},
            {"path_name": "退回起草", "condition": "结论性意见=不同意", "target_node_id": "DRAFT"},
        ]),
        _node("seal_execute", "印章管理员用印", role="印章管理员", paths=[
            {"path_name": "流程结束", "condition": "结论性意见=同意", "target_node_id": "END"},
            {"path_name": "退回起草", "condition": "结论性意见=不同意", "target_node_id": "DRAFT"},
        ]),
    ]


def test_must_route_through_node_passes_when_all_seals_go_through_legal() -> None:
    """所有用印都无条件先过法务——合规，不报。"""
    ok = _proc(_seal_nodes([{"path_name": "送法务审核", "condition": None, "target_node_id": "legal_review"}]))
    assert check_deterministic_compliance(ok, [_seal_rule()]) == []


def test_must_route_through_node_flags_conditional_bypass_of_gate() -> None:
    """真实踩过的漏洞：法务节点仍在，但起草处加一条"印章类型=公章→直送行政审批"绕过法务。
    存在性检查(must_have_node)会放行；条件可达性检查必须逮住这条绕过路径。"""
    bypass = _proc(_seal_nodes([
        {"path_name": "送法务审核", "condition": "印章类型≠公章", "target_node_id": "legal_review"},
        {"path_name": "送行政审批（公章）", "condition": "印章类型=公章", "target_node_id": "admin_approve"},
    ]))
    findings = check_deterministic_compliance(bypass, [_seal_rule()])
    assert len(findings) == 1
    assert "公章" in findings[0].detail and "绕过" in findings[0].detail
    # 精确性：只逮住真被开了口子的公章，没被改道的合同章不误报
    assert "合同章" not in findings[0].detail


def test_must_route_through_node_flags_when_gate_node_entirely_missing() -> None:
    """连法务节点都不存在时，退化为"缺失必经环节"，同样报（比被绕过更严重）。"""
    no_gate = _proc([
        _node("draft", "起草", is_draft=True, paths=[{"path_name": "送行政审批", "condition": None, "target_node_id": "admin_approve"}]),
        _node("admin_approve", "行政审批", role="行政审批", paths=[{"path_name": "流程结束", "condition": "结论性意见=同意", "target_node_id": "END"}]),
    ])
    findings = check_deterministic_compliance(no_gate, [_seal_rule()])
    assert len(findings) == 1 and "缺少" in findings[0].detail


def test_attachment_required_condition() -> None:
    rule = _rule(CheckKind.ATTACHMENT_REQUIRED_CONDITION, {"attachment_keywords": ["病假", "证明"], "condition_keywords": ["病假", "3"]})
    ok = _proc([_node("d", "起草", is_draft=True)], attachments=[{"attachment_type": "病假证明", "upload_stages": ["draft"], "required_stages": ["draft"], "required_condition": "请假类型=病假 且 请假天数>=3"}])
    assert check_deterministic_compliance(ok, [rule]) == []
    no_cond = _proc([_node("d", "起草", is_draft=True)], attachments=[{"attachment_type": "证明材料", "upload_stages": ["draft"], "required_stages": [], "required_condition": None}])
    assert len(check_deterministic_compliance(no_cond, [rule])) == 1
    no_att = _proc([_node("d", "起草", is_draft=True)], attachments=None)
    assert len(check_deterministic_compliance(no_att, [rule])) == 1


def test_field_required_at_draft() -> None:
    rule = _rule(CheckKind.FIELD_REQUIRED_AT_DRAFT, {"field_keywords": ["请假天数", "请假事由"]})
    ok = _proc([_node("d", "起草", is_draft=True)], fields=[_field("请假天数", "数字", ["draft"]), _field("请假事由", "多行文本", ["draft"])])
    assert check_deterministic_compliance(ok, [rule]) == []
    bad = _proc([_node("d", "起草", is_draft=True)], fields=[_field("请假天数", "数字", [])])  # 缺事由 + 天数未必填
    findings = check_deterministic_compliance(bad, [rule])
    assert len(findings) == 1 and "请假事由" in findings[0].detail and "请假天数" in findings[0].detail


def test_handler_mode_for_node() -> None:
    rule = _rule(CheckKind.HANDLER_MODE_FOR_NODE, {"node_keywords": ["会签"], "mode_keywords": ["并行"]})
    ok = _proc([_node("c", "部门会签处理", role="会签专员", mode="多选-并行处理")])
    assert check_deterministic_compliance(ok, [rule]) == []
    bad = _proc([_node("c", "部门会签处理", role="会签专员", mode="单选-单人处理")])
    assert len(check_deterministic_compliance(bad, [rule])) == 1


def test_readonly_autofill_field() -> None:
    rule = _rule(CheckKind.READONLY_AUTOFILL_FIELD, {"field_keywords": ["申请人"]})
    ok = _proc([_node("d", "起草", is_draft=True)], fields=[_field("申请人", "只读文本")])
    assert check_deterministic_compliance(ok, [rule]) == []
    bad = _proc([_node("d", "起草", is_draft=True)], fields=[_field("申请人", "单行文本")])
    assert len(check_deterministic_compliance(bad, [rule])) == 1


def test_findings_sorted_high_severity_first() -> None:
    high = _rule(CheckKind.MUST_HAVE_NODE, {"role_keywords": ["缺失角色"], "name_keywords": []}, rule_id="h", severity="high")
    med = _rule(CheckKind.MUST_HAVE_NODE, {"role_keywords": ["也缺失"], "name_keywords": []}, rule_id="m", severity="medium")
    bad = _proc([_node("d", "起草", is_draft=True)])
    findings = check_deterministic_compliance(bad, [med, high])
    assert [f.severity for f in findings] == ["high", "medium"]


def test_real_leave_case_flags_sick_certificate_gap() -> None:
    process = load_process_definition(LEAVE)
    rules = rules_for_process(load_atomic_rules(), "LEAVE-001", {"leave", "hr"})
    findings = check_deterministic_compliance(process, rules)
    ids = {f.rule_id for f in findings}
    # 请假 gold 的附件没设 required_condition → 病假证明规则应被检出，且带出处引用
    assert "leave.sick_leave_certificate" in ids
    finding = next(f for f in findings if f.rule_id == "leave.sick_leave_certificate")
    assert finding.source_doc == "leave_management_policy" and finding.clause


class _StubJudge:
    def __init__(self, verdicts):
        from app.agents.compliance_judge_agent import RuleVerdict
        self._verdicts = [RuleVerdict.model_validate(v) for v in verdicts]
        self.structured_model = object()

    def judge(self, process, rules):
        valid = {r.rule_id for r in rules}
        return [v for v in self._verdicts if v.rule_id in valid]


def test_check_compliance_orchestrates_deterministic_plus_judge() -> None:
    process = load_process_definition(LEAVE)
    rules = rules_for_process(load_atomic_rules(), "LEAVE-001", {"leave", "hr"})
    judge = _StubJudge([{
        "rule_id": "design.node_naming_role_action",
        "violations": [{"node_id": "gm", "detail": "命名不规范"}],
    }])
    report = check_compliance(process, process_id="LEAVE-001", domains={"leave", "hr"}, rules=rules, judge=judge)
    assert report.applicable_rule_count == len(rules)
    assert any(f.rule_id == "leave.sick_leave_certificate" for f in report.deterministic_findings)
    assert any(f.rule_id == "design.node_naming_role_action" for f in report.qualitative_findings)
    assert report.total_findings >= 2


# ——— 智能门控：定性规则（LLM-judge）编辑时的"新引入违规"检测——只在 diff 真碰到
# 这条规则的地盘时才为它打一次 LLM，且只报"改前合规、改后不合规"的（老问题不算这次账）———

class _SequencedStubJudge:
    """按调用顺序弹出预设 violations——用于区分 before/after 两次 judge() 调用返回不同结果。
    verdicts_by_call 每个元素是 rule_id -> [(node_id, detail), ...] 的映射；某条规则不在
    映射里或列表为空＝该规则在这次调用里完全合规（无 violations）。"""

    def __init__(self, verdicts_by_call: list[dict[str, list[tuple[str, str]]]]):
        self._verdicts_by_call = verdicts_by_call
        self.calls: list[list[str]] = []
        self.structured_model = object()

    def judge(self, process, rules):
        from app.agents.compliance_judge_agent import RuleVerdict, RuleViolation

        self.calls.append([r.rule_id for r in rules])
        index = min(len(self.calls) - 1, len(self._verdicts_by_call) - 1)
        mapping = self._verdicts_by_call[index]
        return [
            RuleVerdict(
                rule_id=r.rule_id,
                violations=[RuleViolation(node_id=nid, detail=detail) for nid, detail in mapping.get(r.rule_id, [])],
            )
            for r in rules
        ]


def _naming_rule() -> AtomicRule:
    return AtomicRule.model_validate({
        "rule_id": "design.node_naming_role_action",
        "dimension": "design_standard",
        "statement": "审批环节应以处理角色加动作方式命名。",
        "applies_to_processes": [], "applies_to_domains": ["design"],
        "applicability_condition": None, "check_type": "llm_judge", "requirement": None,
        "trigger_attributes": ["node_name"],
        "provenance": {"source_doc": "process_element_design_standard", "clause": "x"}, "severity": "medium",
    })


def test_diff_may_trigger_skips_unrelated_attribute_change() -> None:
    from app.rag.compliance import _diff_may_trigger_qualitative_rule

    rule = _naming_rule()  # 只关心 node_name
    diff_touching_time_limit = {"flow_nodes": {"added": [], "removed": [], "changed": [
        {"key": "n", "changes": [{"attribute": "time_limit_days", "before": None, "after": 3}]}]}}
    assert _diff_may_trigger_qualitative_rule(rule, diff_touching_time_limit) is False


def test_diff_may_trigger_fires_on_declared_attribute() -> None:
    from app.rag.compliance import _diff_may_trigger_qualitative_rule

    rule = _naming_rule()
    diff_touching_name = {"flow_nodes": {"added": [], "removed": [], "changed": [
        {"key": "n", "changes": [{"attribute": "node_name", "before": "A", "after": "B"}]}]}}
    assert _diff_may_trigger_qualitative_rule(rule, diff_touching_name) is True


def test_diff_may_trigger_fires_on_node_add_or_remove_regardless_of_attributes() -> None:
    from app.rag.compliance import _diff_may_trigger_qualitative_rule

    rule = _naming_rule()
    assert _diff_may_trigger_qualitative_rule(rule, {"flow_nodes": {"added": ["x"], "removed": [], "changed": []}}) is True
    assert _diff_may_trigger_qualitative_rule(rule, {"flow_nodes": {"added": [], "removed": ["x"], "changed": []}}) is True


def test_diff_may_trigger_fires_on_submit_paths_change_when_declared() -> None:
    from app.rag.compliance import _diff_may_trigger_qualitative_rule

    rule = AtomicRule.model_validate({
        "rule_id": "r", "dimension": "design_standard", "statement": "s",
        "applies_to_processes": [], "applies_to_domains": [], "applicability_condition": None,
        "check_type": "llm_judge", "requirement": None, "trigger_attributes": ["submit_paths"],
        "provenance": {"source_doc": "d", "clause": "c"}, "severity": "medium",
    })
    diff = {"flow_nodes": {"added": [], "removed": [], "changed": [
        {"key": "n", "changes": [], "submit_paths": {"added": [], "removed": [], "changed": [{"key": "p", "changes": []}]}}]}}
    assert _diff_may_trigger_qualitative_rule(rule, diff) is True


def test_diff_may_trigger_defaults_to_true_without_declared_attributes() -> None:
    """没声明 trigger_attributes 时保守起见照判，不敢随便跳过。"""
    from app.rag.compliance import _diff_may_trigger_qualitative_rule

    rule = AtomicRule.model_validate({
        "rule_id": "r", "dimension": "design_standard", "statement": "s",
        "applies_to_processes": [], "applies_to_domains": [], "applicability_condition": None,
        "check_type": "llm_judge", "requirement": None,
        "provenance": {"source_doc": "d", "clause": "c"}, "severity": "medium",
    })
    diff = {"flow_nodes": {"added": [], "removed": [], "changed": [
        {"key": "n", "changes": [{"attribute": "time_limit_days", "before": None, "after": 3}]}]}}
    assert _diff_may_trigger_qualitative_rule(rule, diff) is True


def test_new_qualitative_findings_skips_llm_call_when_diff_does_not_trigger_any_rule() -> None:
    """智能门控的核心收益：跟这次编辑无关的定性规则不该被判——也不该调 LLM。"""
    from app.rag.compliance import new_qualitative_findings_for_edit

    before = _proc([_node("n", "起草", is_draft=True, paths=[{"path_name": "p", "condition": None, "target_node_id": "END"}])])
    after = _proc([_node("n", "起草", is_draft=True, paths=[{"path_name": "p", "condition": None, "target_node_id": "END"}])])
    after.flow_nodes[0].time_limit_days = 3  # 只改了处理期限，跟命名/handler/submit_paths 无关
    diff = diff_process_definitions(before, after)
    judge = _SequencedStubJudge([{}])
    findings = new_qualitative_findings_for_edit(
        before=before, after=after, diff=diff, rules=[_naming_rule()], judge=judge,
    )
    assert findings == []
    assert judge.calls == []  # 没触发就不该调 judge


def test_new_qualitative_findings_reports_newly_introduced_violation() -> None:
    """改前合规、改后不合规——报；这正是"公司领导批示"→"公司领导"丢动作词的场景。"""
    from app.rag.compliance import new_qualitative_findings_for_edit

    before = _proc([_node("n", "公司领导批示", role="公司领导")])
    after = _proc([_node("n", "公司领导", role="公司领导")])
    diff = diff_process_definitions(before, after)
    judge = _SequencedStubJudge([
        {"design.node_naming_role_action": []},                      # before：环节 n 合规，无违规
        {"design.node_naming_role_action": [("n", "缺动作词")]},  # after：环节 n 新增违规
    ])
    findings = new_qualitative_findings_for_edit(
        before=before, after=after, diff=diff, process_id="P", rules=[_naming_rule()], judge=judge,
    )
    assert len(findings) == 1 and findings[0].rule_id == "design.node_naming_role_action"
    assert "n" in findings[0].detail and "缺动作词" in findings[0].detail
    assert len(judge.calls) == 2  # before 一次、after 一次


def test_new_qualitative_findings_does_not_report_preexisting_violation() -> None:
    """改前就已经不合规、这次编辑没让它变好也没变坏——不算这次编辑新引入的账，不报。
    真实场景关键点：流程里*别的*环节（n1）本来就有违规，这次编辑只是改了 n2 的名字
    但 n2 改前改后都不合规——按*环节*（不是整条规则）取差集，n2 不该被算作新违规。"""
    from app.rag.compliance import new_qualitative_findings_for_edit

    before = _proc([_node("n1", "起草", is_draft=True), _node("n2", "分发", role="专员")])  # 本来就不合规的命名
    after = _proc([_node("n1", "起草", is_draft=True), _node("n2", "转发", role="专员")])  # 改了但依然不合规
    diff = diff_process_definitions(before, after)
    judge = _SequencedStubJudge([
        {"design.node_naming_role_action": [("n1", "缺角色"), ("n2", "本来就不合规")]},  # before：n1/n2 都违规
        {"design.node_naming_role_action": [("n1", "缺角色"), ("n2", "还是不合规")]},    # after：还是这两个环节
    ])
    findings = new_qualitative_findings_for_edit(
        before=before, after=after, diff=diff, process_id="P", rules=[_naming_rule()], judge=judge,
    )
    assert findings == []  # n1/n2 都是改前就已违规的环节，没有新环节加入违规列表，不算这次编辑的账


def test_new_qualitative_findings_isolates_new_violation_amid_preexisting_ones() -> None:
    """真实生产环境复现的场景（EOA140"公司领导批示"→"公司领导"那次）：流程里本来就有
    跟这次编辑无关的老违规（draft、申请人分发），这次编辑单独让"公司领导"这个环节从
    合规变成不合规——必须准确报出"公司领导"这一条，不能因为规则整体改前/改后都是
    "不合规"就被粗粒度地整体判定成"没有新增"而漏报（这正是只按整条规则 true/false
    对比会踩的坑，也是改成按环节列表取差集要解决的问题）。"""
    from app.rag.compliance import new_qualitative_findings_for_edit

    before = _proc([_node("draft", "起草", is_draft=True), _node("leader", "公司领导批示", role="公司领导")])
    after = _proc([_node("draft", "起草", is_draft=True), _node("leader", "公司领导", role="公司领导")])
    diff = diff_process_definitions(before, after)
    judge = _SequencedStubJudge([
        {"design.node_naming_role_action": [("draft", "仅有动作未体现角色")]},  # before：只有老问题 draft
        {"design.node_naming_role_action": [("draft", "仅有动作未体现角色"), ("leader", "缺动作词")]},  # after：老问题还在 + leader 新增
    ])
    findings = new_qualitative_findings_for_edit(
        before=before, after=after, diff=diff, process_id="P", rules=[_naming_rule()], judge=judge,
    )
    assert len(findings) == 1
    assert "leader" in findings[0].detail and "draft" not in findings[0].detail  # 只报新的那个环节，老问题不重复报


# ——— 主动提醒：这次编辑碰到了哪些制度规则（不判违规，纯确定性子串匹配）———

def _rule_with_governs(rule_id: str, governs: list[str], dimension="company_policy") -> AtomicRule:
    return AtomicRule.model_validate({
        "rule_id": rule_id, "dimension": dimension, "statement": f"{rule_id} 的表述",
        "applies_to_processes": [], "applies_to_domains": ["procurement"], "applicability_condition": None,
        "check_type": "deterministic", "requirement": {"kind": "must_have_node", "params": {}},
        "governs": governs,
        "provenance": {"source_doc": "doc", "clause": "x"}, "severity": "medium",
    })


def test_rules_touched_by_edit_matches_condition_text_on_governed_term() -> None:
    """用户的真实场景：改路由条件里的金额阈值（采购金额>=500000 → >=100000），
    该条件文本命中规则管辖的'采购金额'——应被提醒，哪怕这次改动本身不违规。"""
    from app.rag.compliance import rules_touched_by_edit

    rule = _rule_with_governs("auth.procurement_committee_for_large", ["采购金额", "采购委员会", "会签"])
    before = _proc([_node("intake", "采购受理", role="采购受理人", paths=[
        {"path_name": "送采购委员会会签", "condition": "结论性意见=同意 且 采购金额>=500000", "target_node_id": "committee"}])])
    after = _proc([_node("intake", "采购受理", role="采购受理人", paths=[
        {"path_name": "送采购委员会会签", "condition": "结论性意见=同意 且 采购金额>=100000", "target_node_id": "committee"}])])
    diff = diff_process_definitions(before, after)
    touched = rules_touched_by_edit([rule], diff)
    assert [r.rule_id for r in touched] == ["auth.procurement_committee_for_large"]


def test_rules_touched_by_edit_ignores_unrelated_edit() -> None:
    """改一个跟采购金额/委员会都不沾边的字段说明——不该提醒采购委员会规则。"""
    from app.rag.compliance import rules_touched_by_edit

    rule = _rule_with_governs("auth.procurement_committee_for_large", ["采购金额", "采购委员会", "会签"])
    before = _proc([_node("d", "起草", is_draft=True)], fields=[_field("联系电话")])
    after_fields = [dict(_field("联系电话"), logic_description="填手机号")]
    after = _proc([_node("d", "起草", is_draft=True)], fields=after_fields)
    diff = diff_process_definitions(before, after)
    assert rules_touched_by_edit([rule], diff) == []


def test_rules_touched_by_edit_excludes_design_standard_rules() -> None:
    """设计规范类（命名/只读这种普适格式规则）不进'碰到制度'提醒——否则每次改环节名都刷屏。"""
    from app.rag.compliance import rules_touched_by_edit

    design_rule = _rule_with_governs("design.node_naming_role_action", ["审批"], dimension="design_standard")
    before = _proc([_node("a", "部门审批", role="部门主管")])
    after = _proc([_node("a", "部门主管审批", role="部门主管")])  # 改环节名，含"审批"
    diff = diff_process_definitions(before, after)
    assert rules_touched_by_edit([design_rule], diff) == []


def test_rules_touched_falls_back_to_requirement_keywords_without_governs() -> None:
    """没显式 governs 时，回退用 requirement.params 里的 *_keywords 兜底。"""
    from app.rag.compliance import rules_touched_by_edit

    rule = AtomicRule.model_validate({
        "rule_id": "leave.handover", "dimension": "company_policy", "statement": "须指定工作交接人",
        "applies_to_processes": [], "applies_to_domains": ["leave"], "applicability_condition": None,
        "check_type": "deterministic", "requirement": {"kind": "field_required_at_draft", "params": {"field_keywords": ["工作交接人"]}},
        "provenance": {"source_doc": "doc", "clause": "x"}, "severity": "medium",
    })
    before = _proc([_node("d", "起草", is_draft=True)], fields=[_field("请假事由")])
    after = _proc([_node("d", "起草", is_draft=True)], fields=[_field("请假事由"), _field("工作交接人", "员工选择")])
    diff = diff_process_definitions(before, after)
    assert [r.rule_id for r in rules_touched_by_edit([rule], diff)] == ["leave.handover"]


# ——— 回归：域关键词推断不能靠"员工"/"金额"这类通用词，否则请假/大额报销规则会
# 误配到无关流程（真实复现过：费用报销、用印流程被塞进 5 条请假合规发现；
# 采购流程被塞进"大额报销需财务负责人审批"）———

def _expense_like_process() -> ProcessDefinition:
    field = {
        "seq": 1, "field_name": "报销总额", "required_stages": ["all"], "visible_stages": ["all"],
        "editable_stages": ["draft"], "component_type": "数字",
    }
    return ProcessDefinition.model_validate({
        "meta": {"process_id": "EXPENSE-TEST", "process_name": "费用报销流程",
                 "description": "员工发起费用报销申请，经部门主管审批。", "version": "V1",
                 "responsible_dept": "d", "applicant_scope": "全员", "entry_point": "OA"},
        "form_fields": [field],
        "flow_nodes": [_node("d", "起草", is_draft=True), _node("mgr", "部门主管审批", role="部门主管")],
        "attachments": None, "roles": None,
    })


def _procurement_like_process() -> ProcessDefinition:
    field = {
        "seq": 1, "field_name": "采购金额", "required_stages": ["all"], "visible_stages": ["all"],
        "editable_stages": ["draft"], "component_type": "数字",
    }
    return ProcessDefinition.model_validate({
        "meta": {"process_id": "PROCUREMENT-TEST", "process_name": "采购申请流程",
                 "description": "员工发起采购申请，经采购受理人处理。", "version": "V1",
                 "responsible_dept": "d", "applicant_scope": "全员", "entry_point": "OA"},
        "form_fields": [field],
        "flow_nodes": [_node("d", "起草", is_draft=True), _node("agent", "采购受理", role="采购受理人")],
        "attachments": None, "roles": None,
    })


def test_infer_domains_does_not_tag_generic_employee_mention_as_leave() -> None:
    domains = infer_domains(_expense_like_process())
    assert "leave" not in domains and "hr" not in domains


def test_deterministic_findings_for_draft_does_not_leak_leave_rules_into_expense_flow() -> None:
    findings = deterministic_findings_for_draft(_expense_like_process(), process_id="EXPENSE-TEST")
    assert not any(f.rule_id.startswith("leave.") for f in findings)


def test_infer_domains_does_not_tag_amount_field_as_finance() -> None:
    """"金额"字段名太通用——采购金额、合同金额都带"金额"，不能因此打上 finance 域标签。"""
    domains = infer_domains(_procurement_like_process())
    assert "finance" not in domains


def test_deterministic_findings_for_draft_does_not_leak_finance_head_rule_into_procurement_flow() -> None:
    findings = deterministic_findings_for_draft(_procurement_like_process(), process_id="PROCUREMENT-TEST")
    assert not any(f.rule_id == "auth.finance_head_for_large_expense" for f in findings)
