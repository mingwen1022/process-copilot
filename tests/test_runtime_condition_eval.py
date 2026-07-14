from __future__ import annotations

from app.runtime.condition_eval import evaluate_condition


def test_condition_eval_matches_opinion_and_number_with_loose_field_name() -> None:
    context = {"结论性意见": "同意", "报销金额（元）": 60000}

    assert evaluate_condition("结论性意见=同意 且 报销金额≥50000元", context).matched
    assert not evaluate_condition("结论性意见=同意 且 报销金额＜50000元", context).matched


def test_condition_eval_matches_text_or_options_and_last_task_flags() -> None:
    context = {"印章类型": "合同章", "is_last_task": False}

    assert evaluate_condition("印章类型=公章或合同章", context).matched
    assert evaluate_condition("非最后一人", context).matched
    assert not evaluate_condition("最后一人", context).matched


def test_condition_eval_supports_membership_sets() -> None:
    context = {"请假类型": "病假", "请假天数": 2}

    assert evaluate_condition("请假类型∈{事假,病假}", context).matched
    assert not evaluate_condition("请假类型∈{婚假,产假,陪产假}", context).matched
    assert evaluate_condition("请假类型∉{婚假,产假,陪产假}", context).matched
    assert evaluate_condition("结论性意见=同意 且 请假类型∈{事假,病假} 且 请假天数≤3天", {**context, "结论性意见": "同意"}).matched


def test_condition_eval_supports_negation_group() -> None:
    short_sick = {"结论性意见": "同意", "请假类型": "病假", "请假天数": 2}
    long_annual = {"结论性意见": "同意", "请假类型": "年假", "请假天数": 5}

    condition = "结论性意见=同意 且 非(请假类型∈{事假,病假} 且 请假天数≤3天)"
    assert not evaluate_condition(condition, short_sick).matched
    assert evaluate_condition(condition, long_annual).matched


def test_condition_eval_supports_paren_or_group_and_day_unit() -> None:
    condition = "结论性意见=同意 且 (请假天数>7天 或 请假类型∈{婚假,产假,陪产假})"

    assert evaluate_condition(condition, {"结论性意见": "同意", "请假类型": "事假", "请假天数": 10}).matched
    assert evaluate_condition(condition, {"结论性意见": "同意", "请假类型": "婚假", "请假天数": 3}).matched
    assert not evaluate_condition(condition, {"结论性意见": "同意", "请假类型": "事假", "请假天数": 5}).matched
    assert not evaluate_condition(condition, {"结论性意见": "不同意", "请假类型": "婚假", "请假天数": 10}).matched

