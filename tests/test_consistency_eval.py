"""横切一致性断言评测的单测。

断言器本身是纯函数，可以脱离管线直接喂构造好的记录测；再加一组"走真实确定性管线"
的夹具测，保证架构保证（确定性层中和 LLM 矛盾）没被回退掉。全程不打 Bedrock。
"""

from __future__ import annotations

from app.eval.consistency_eval import (
    KIND_IDENTIFIER_LEAK,
    KIND_ROUTE_PROMISE,
    KIND_SAY_DO,
    KIND_UNGROUNDED_CITATION,
    ConsistencyEvalCase,
    ProposalRecord,
    build_ops_consistency_cases,
    check_record,
    evaluate_consistency,
)


def _record(**kw) -> ProposalRecord:
    base = dict(
        case="t", visible_texts={}, route="terminal",
        node_names={"line_leader": "条线分管领导审批", "gm": "部门总经理审批"},
    )
    base.update(kw)
    return ProposalRecord(**base)


def _kinds(record: ProposalRecord) -> set[str]:
    return {v.kind for v in check_record(record)}


# ——— 说到做到 ———

def test_say_do_flags_reply_naming_a_different_node_than_the_action() -> None:
    r = _record(visible_texts={"reply": "按规则跳到条线分管领导审批。"}, jump_target_node_id="gm")
    assert KIND_SAY_DO in _kinds(r)


def test_say_do_passes_when_reply_and_action_agree() -> None:
    r = _record(visible_texts={"reply": "按规则跳到条线分管领导审批。"}, jump_target_node_id="line_leader")
    assert KIND_SAY_DO not in _kinds(r)


def test_say_do_ignores_replies_that_name_no_node() -> None:
    r = _record(visible_texts={"reply": "已按规则并入更高一档。"}, jump_target_node_id="line_leader")
    assert KIND_SAY_DO not in _kinds(r)


# ——— 承诺兑现 ———

def test_route_promise_flags_authorization_promise_without_authorization_route() -> None:
    r = _record(visible_texts={"reply": "已为你生成授权工单，转流程负责人审批。"}, route="terminal")
    assert KIND_ROUTE_PROMISE in _kinds(r)


def test_route_promise_allows_merely_pointing_at_the_console() -> None:
    """纯指路（"可以去授权台看看"）不是承诺，不该误报。"""
    r = _record(visible_texts={"reply": "这类高风险动作要由流程负责人在运维授权台处理。"}, route="terminal")
    assert KIND_ROUTE_PROMISE not in _kinds(r)


def test_route_promise_flags_claiming_done_with_no_actions() -> None:
    r = _record(visible_texts={"reply": "我已帮你修改完了。"}, route="terminal", has_actions=False)
    assert KIND_ROUTE_PROMISE in _kinds(r)


# ——— 标识符泄漏 ———

def test_identifier_leak_flags_raw_user_id() -> None:
    r = _record(visible_texts={"reply": "工作交接人是 u_it_line_leader。"},
                known_user_ids=["u_it_line_leader"])
    assert KIND_IDENTIFIER_LEAK in _kinds(r)


def test_identifier_leak_flags_node_id_when_a_chinese_name_exists() -> None:
    r = _record(visible_texts={"rationale": "目标环节 target_node_id=line_leader。"})
    assert KIND_IDENTIFIER_LEAK in _kinds(r)


def test_identifier_leak_flags_the_word_override() -> None:
    r = _record(visible_texts={"reply": "需要 override 一下这个环节。"})
    assert KIND_IDENTIFIER_LEAK in _kinds(r)


def test_identifier_leak_clean_on_chinese_only_text() -> None:
    r = _record(visible_texts={"reply": "已跳转到条线分管领导审批，由赵文杰处理。"})
    assert KIND_IDENTIFIER_LEAK not in _kinds(r)


# ——— 编造引用 ———

def test_ungrounded_citation_flags_unknown_rule_id() -> None:
    r = _record(visible_texts={"rationale": "依据 leave.made_up_rule 处理。"},
                known_rule_ids=["leave.core_fields_required_at_draft"])
    assert KIND_UNGROUNDED_CITATION in _kinds(r)


def test_ungrounded_citation_passes_on_real_rule_id() -> None:
    r = _record(visible_texts={"rationale": "依据 leave.core_fields_required_at_draft 处理。"},
                known_rule_ids=["leave.core_fields_required_at_draft"])
    assert KIND_UNGROUNDED_CITATION not in _kinds(r)


def test_ungrounded_citation_skipped_when_no_rule_set_given() -> None:
    r = _record(visible_texts={"rationale": "依据 leave.whatever 处理。"}, known_rule_ids=[])
    assert KIND_UNGROUNDED_CITATION not in _kinds(r)


# ——— 打分 ———

def test_scorecard_counts_recall_and_false_positives() -> None:
    cases = [
        ConsistencyEvalCase(record=_record(case="干净", visible_texts={"reply": "已跳转到条线分管领导审批。"},
                                           jump_target_node_id="line_leader"), expected_kinds=[]),
        ConsistencyEvalCase(record=_record(case="该报泄漏", visible_texts={"reply": "去 override 一下。"}),
                            expected_kinds=[KIND_IDENTIFIER_LEAK]),
    ]
    sc = evaluate_consistency(cases)
    assert sc.case_count == 2
    assert sc.recall == 1.0
    assert sc.false_positive_count == 0
    assert sc.clean_case_count == 1


# ——— 架构保证回归门禁（走真实确定性管线，不打模型）———

def test_deterministic_layer_neutralizes_contradictory_llm_decisions() -> None:
    """LLM 说跳 A 却填 B、承诺授权却给了不存在的人——确定性层都必须把输出拉回自洽。
    这条挂了说明确定性护栏被回退了，是真回归。"""
    sc = evaluate_consistency(build_ops_consistency_cases())
    assert sc.recall == 1.0, f"正控没被检出：{[r.missed for r in sc.results]}"
    assert sc.false_positive_count == 0, f"误报：{[(r.case, r.unexpected) for r in sc.results if r.unexpected]}"
    assert sc.clean_case_count == 3
