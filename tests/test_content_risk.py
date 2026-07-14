from __future__ import annotations

from app.copilots.content_risk import check_content_risk, check_expense_total_consistency
from data.schema import AtomicRule, CheckKind, RuleDimension, RuleProvenance, RuleRequirement


def _rule(**params) -> AtomicRule:  # noqa: ANN003
    cond = params.pop("condition", None)
    return AtomicRule(
        rule_id="expense.hotel_standard",
        title="酒店住宿标准",
        dimension=RuleDimension.COMPANY_POLICY,
        statement="酒店住宿每晚不超过标准",
        check_type="deterministic",
        applicability_condition=cond,
        requirement=RuleRequirement(kind=CheckKind.VALUE_THRESHOLD, params=params),
        provenance=RuleProvenance(source_doc="差旅费报销标准", clause="第3条"),
    )


def test_over_threshold_flags_violation() -> None:
    v = check_content_risk({"酒店每晚": 1000}, [_rule(field="酒店每晚", op="<=", threshold=800, message="酒店超差旅标准 800/晚")])
    assert len(v) == 1
    assert v[0]["value"] == 1000 and v[0]["threshold"] == 800
    assert "差旅费报销标准" in v[0]["clause"]


def test_within_threshold_no_violation() -> None:
    assert check_content_risk({"酒店每晚": 700}, [_rule(field="酒店每晚", op="<=", threshold=800)]) == []


def test_applicability_condition_gates_rule() -> None:
    # 一线城市阈值 1000：二线出差(条件不满足)→本规则不适用；一线且超 1000→违规
    r = _rule(field="酒店每晚", op="<=", threshold=1000, condition="出差城市=一线")
    assert check_content_risk({"酒店每晚": 950, "出差城市": "二线"}, [r]) == []
    assert len(check_content_risk({"酒店每晚": 1200, "出差城市": "一线"}, [r])) == 1


def test_missing_value_skipped() -> None:
    assert check_content_risk({}, [_rule(field="酒店每晚", op="<=", threshold=800)]) == []


def test_non_threshold_rule_ignored() -> None:
    """非 VALUE_THRESHOLD 的规则不参与内容风险检查。"""
    r = AtomicRule(
        rule_id="x", dimension=RuleDimension.COMPANY_POLICY, statement="s", check_type="deterministic",
        requirement=RuleRequirement(kind=CheckKind.MUST_HAVE_NODE, params={}),
        provenance=RuleProvenance(source_doc="d", clause="c"),
    )
    assert check_content_risk({"酒店每晚": 9999}, [r]) == []


# ——— 报销总额 vs 费用明细合计一致性（确定性核对）———

def test_total_matches_itemized_sum_no_violation() -> None:
    fv = {"费用明细": "酒店 3000 元；机票 4200 元；市内交通 1400 元", "报销总额": 8600}
    assert check_expense_total_consistency(fv) == []


def test_total_exceeds_itemized_flags_violation() -> None:
    fv = {"费用明细": "酒店 3000 元；机票 4200 元；市内交通 1400 元", "报销总额": 9200}
    v = check_expense_total_consistency(fv)
    assert len(v) == 1
    assert v[0]["rule_id"] == "expense.total_matches_itemized"
    assert v[0]["value"] == 9200 and v[0]["threshold"] == 8600
    assert "高于" in v[0]["message"] and "600" in v[0]["message"]


def test_total_below_itemized_flags_violation() -> None:
    fv = {"费用明细": "酒店 3000 元；机票 4200 元", "报销总额": 6000}
    v = check_expense_total_consistency(fv)
    assert len(v) == 1 and "低于" in v[0]["message"]


def test_non_amount_numbers_not_summed() -> None:
    """"3 晚"这类不带"元"的数字不算金额，不参与求和。"""
    fv = {"费用明细": "酒店 3 晚 3000 元；机票往返 4200 元", "报销总额": 7200}
    assert check_expense_total_consistency(fv) == []  # 3000+4200=7200，"3 晚"不计入


def test_no_itemized_amounts_or_missing_total_skipped() -> None:
    assert check_expense_total_consistency({"费用明细": "办公用品若干", "报销总额": 500}) == []
    assert check_expense_total_consistency({"费用明细": "酒店 3000 元"}) == []  # 缺总额
