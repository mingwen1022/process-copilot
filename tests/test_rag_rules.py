from __future__ import annotations

from app.rag.rules import load_atomic_rules, rules_for_process, validate_rules
from data.schema import AtomicRule, CheckKind, RuleProvenance, RuleRequirement


def test_authored_rules_load_and_validate_clean() -> None:
    rules = load_atomic_rules()
    assert len(rules) >= 12
    assert validate_rules(rules) == []


def test_anchor_cases_have_applicable_rules() -> None:
    rules = load_atomic_rules()
    leave = {r.rule_id for r in rules_for_process(rules, "LEAVE-001", {"leave"})}
    eoa = {r.rule_id for r in rules_for_process(rules, "EOA140", {"governance"})}
    assert "leave.sick_leave_certificate" in leave
    assert "leave.line_leader_for_long_or_special" in leave
    assert "eoa140.company_leader_instruction" in eoa
    # 设计规范锚到两个案例，都应命中
    assert "design.has_draft_and_end" in leave and "design.has_draft_and_end" in eoa


def test_domain_rules_surface_without_process_anchor() -> None:
    """广度域规则（如费用金额边界）没绑流程号，但按域检索应能命中。"""
    rules = load_atomic_rules()
    finance = {r.rule_id for r in rules_for_process(rules, "NONEXISTENT", {"finance"})}
    assert "auth.finance_head_for_large_expense" in finance


def _rule(**overrides) -> AtomicRule:
    base = dict(
        rule_id="x.rule",
        dimension="company_policy",
        statement="测试规则",
        applies_to_processes=["LEAVE-001"],
        applies_to_domains=["leave"],
        applicability_condition=None,
        check_type="deterministic",
        requirement=RuleRequirement(kind=CheckKind.HAS_DRAFT_AND_END, params={}),
        provenance=RuleProvenance(source_doc="leave_management_policy", clause="x"),
        severity="medium",
    )
    base.update(overrides)
    return AtomicRule.model_validate(base)


def test_validate_flags_duplicate_ids() -> None:
    rules = [_rule(rule_id="dup"), _rule(rule_id="dup")]
    issues = validate_rules(rules)
    assert any("重复" in i for i in issues)


def test_validate_flags_missing_provenance_doc() -> None:
    rules = [_rule(provenance=RuleProvenance(source_doc="does_not_exist", clause="x"))]
    issues = validate_rules(rules)
    assert any("不存在于知识库" in i for i in issues)


def test_validate_flags_deterministic_without_requirement() -> None:
    rules = [_rule(check_type="deterministic", requirement=None)]
    issues = validate_rules(rules)
    assert any("缺 requirement" in i for i in issues)


def test_validate_flags_llm_judge_with_requirement() -> None:
    rules = [_rule(check_type="llm_judge", requirement=RuleRequirement(kind=CheckKind.HAS_DRAFT_AND_END, params={}))]
    issues = validate_rules(rules)
    assert any("却带了 requirement" in i for i in issues)


def test_applicability_condition_is_evaluable_by_condition_eval() -> None:
    """适用条件与流程路由条件同一套表达式，应能被 condition_eval 求值。"""
    from app.runtime.condition_eval import evaluate_condition

    rules = load_atomic_rules()
    sick = next(r for r in rules if r.rule_id == "leave.sick_leave_certificate")
    # 病假3天 → 适用
    assert evaluate_condition(sick.applicability_condition, {"请假类型": "病假", "请假天数": 3}).matched
    # 年假3天 → 不适用
    assert not evaluate_condition(sick.applicability_condition, {"请假类型": "年假", "请假天数": 3}).matched
