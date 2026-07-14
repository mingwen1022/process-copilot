"""规则合规率评测（模块3 · Phase 5）。

跟分析侧检出率评测同一套纪律：造一批"埋了已知违规"的流程草稿当 gold，量确定性
合规检查能不能把违规查出来、会不会误报。评测对象是确定性合规检查（不含 LLM），
所以确定、可复现。

case 是程序化构造的：从一个"完全合规"的请假流程基线出发，每次只注入一处违规，
标注它应该触发哪条规则。外加一个完全合规的基线（期望 0 违规），用来量误报/过度
告警。
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from app.io_utils import load_process_definition
from app.rag.compliance import _select_applicable_rules, check_deterministic_compliance
from app.rag.rules import load_atomic_rules
from data.schema import ProcessDefinition

LEAVE_TARGET = "data/cases/leave_request/standard/target.json"


class ComplianceEvalCase(BaseModel):
    name: str
    process: ProcessDefinition
    expected_rules: list[str] = Field(description="该 case 应触发的违规规则 id（空=期望完全合规）")


class ComplianceCaseResult(BaseModel):
    name: str
    expected_rules: list[str]
    detected_rules: list[str]
    matched: list[str]
    missed: list[str]
    unexpected: list[str] = Field(description="检出了但不在预期内（误报）")


class ComplianceScorecard(BaseModel):
    case_count: int
    total_expected: int
    detected_expected: int
    recall: float | None = Field(description="违规检出率 = 命中预期违规 / 全部预期违规")
    false_positive_count: int = Field(description="所有 case 累计的误报数（检出非预期违规）")
    results: list[ComplianceCaseResult] = Field(default_factory=list)


def _compliant_leave() -> ProcessDefinition:
    """把请假 gold 修成完全合规基线：给病假证明附件补上 required_condition。"""
    data = load_process_definition(LEAVE_TARGET).model_dump(mode="json")
    data["attachments"] = [
        {"attachment_type": "病假证明", "upload_stages": ["draft"], "required_stages": ["draft"],
         "required_condition": "请假类型=病假 且 请假天数>=3"}
    ]
    return ProcessDefinition.model_validate(data)


def _mutate(base: ProcessDefinition, fn) -> ProcessDefinition:
    data = base.model_dump(mode="json")
    fn(data)
    return ProcessDefinition.model_validate(data)


def build_leave_eval_cases() -> list[ComplianceEvalCase]:
    base = _compliant_leave()
    cases: list[ComplianceEvalCase] = [ComplianceEvalCase(name="合规基线", process=base, expected_rules=[])]

    def drop_node(node_id):
        return lambda d: d.__setitem__("flow_nodes", [n for n in d["flow_nodes"] if n["node_id"] != node_id])

    def make_field_editable(name):
        def apply(d):
            for f in d["form_fields"]:
                if f["field_name"] == name:
                    f["component_type"] = "单行文本"
        return apply

    def unrequire_field(name):
        def apply(d):
            for f in d["form_fields"]:
                if f["field_name"] == name:
                    f["required_stages"] = []
        return apply

    def strip_end_paths(d):
        for n in d["flow_nodes"]:
            n["submit_paths"] = [p for p in n["submit_paths"] if p["target_node_id"] != "END"]

    def clear_attachment_condition(d):
        d["attachments"] = [{"attachment_type": "证明材料", "upload_stages": ["draft"], "required_stages": [], "required_condition": None}]

    cases += [
        ComplianceEvalCase(name="缺条线分管领导审批环节", process=_mutate(base, drop_node("line_leader")),
                           expected_rules=["leave.line_leader_for_long_or_special"]),
        ComplianceEvalCase(name="缺部门总经理审批环节", process=_mutate(base, drop_node("dept_gm")),
                           expected_rules=["leave.gm_approval_present"]),
        ComplianceEvalCase(name="申请人字段可编辑（非只读）", process=_mutate(base, make_field_editable("申请人")),
                           expected_rules=["design.autofill_fields_readonly"]),
        ComplianceEvalCase(name="请假天数未起草必填", process=_mutate(base, unrequire_field("请假天数")),
                           expected_rules=["leave.core_fields_required_at_draft"]),
        ComplianceEvalCase(name="缺通向流程结束的路径", process=_mutate(base, strip_end_paths),
                           expected_rules=["design.has_draft_and_end"]),
        ComplianceEvalCase(name="病假证明未按条件必传", process=_mutate(base, clear_attachment_condition),
                           expected_rules=["leave.sick_leave_certificate"]),
    ]
    return cases


def evaluate_compliance(cases: list[ComplianceEvalCase], process_id: str = "LEAVE-001") -> ComplianceScorecard:
    all_rules = load_atomic_rules()
    results: list[ComplianceCaseResult] = []
    total_expected = 0
    detected_expected = 0
    false_positives = 0

    for case in cases:
        rules = _select_applicable_rules(all_rules, case.process, process_id, None)
        findings = check_deterministic_compliance(case.process, rules)
        detected = [f.rule_id for f in findings]
        expected = set(case.expected_rules)
        matched = sorted(expected & set(detected))
        missed = sorted(expected - set(detected))
        unexpected = sorted(set(detected) - expected)
        total_expected += len(expected)
        detected_expected += len(matched)
        false_positives += len(unexpected)
        results.append(ComplianceCaseResult(
            name=case.name, expected_rules=sorted(expected), detected_rules=sorted(detected),
            matched=matched, missed=missed, unexpected=unexpected,
        ))

    return ComplianceScorecard(
        case_count=len(cases),
        total_expected=total_expected,
        detected_expected=detected_expected,
        recall=round(detected_expected / total_expected, 4) if total_expected else None,
        false_positive_count=false_positives,
        results=results,
    )


def render_compliance_scorecard_markdown(card: ComplianceScorecard) -> str:
    lines = [
        "# 规则合规率评测（确定性合规检查）",
        "",
        f"- **违规检出率**：{_pct(card.recall)}（{card.detected_expected}/{card.total_expected}）",
        f"- 累计误报：{card.false_positive_count}",
        f"- case 数：{card.case_count}",
        "",
        "| case | 预期违规 | 检出 | 命中 | 漏检 | 误报 |",
        "|---|---|---|---|---|---|",
    ]
    for r in card.results:
        lines.append(
            f"| {r.name} | {'、'.join(r.expected_rules) or '（无·合规基线）'} | {'、'.join(r.detected_rules) or '—'} | "
            f"{'✓' if not r.missed and r.expected_rules else ('—' if not r.expected_rules else '✗')} | "
            f"{'、'.join(r.missed) or '—'} | {'、'.join(r.unexpected) or '—'} |"
        )
    return "\n".join(lines)


def _pct(value: float | None) -> str:
    return f"{value * 100:.1f}%" if value is not None else "—"
