"""原子规则加载 + 完整性校验（模块3 · Phase 1）。

从 data/knowledge/rules/*.json 加载原子规则，做结构性校验：id 唯一、出处指向
真实存在的知识文档、确定性/定性规则与 requirement 的搭配正确、适用条件可被
condition_eval 求值（不崩）。检查执行（拿规则查 ProcessDefinition）在 Phase 3。
"""

from __future__ import annotations

from pathlib import Path

from app.org_knowledge import parse_knowledge_documents
from app.runtime.condition_eval import evaluate_condition
from data.schema import AtomicRule, RuleSet

PROJECT_ROOT = Path(__file__).resolve().parents[2]
KNOWLEDGE_ROOT = PROJECT_ROOT / "data/knowledge"
RULES_ROOT = KNOWLEDGE_ROOT / "rules"


def load_atomic_rules(root: str | Path = RULES_ROOT) -> list[AtomicRule]:
    rules: list[AtomicRule] = []
    for path in sorted(Path(root).glob("*.json")):
        rule_set = RuleSet.model_validate_json(path.read_text(encoding="utf-8"))
        rules.extend(rule_set.rules)
    return rules


def validate_rules(
    rules: list[AtomicRule] | None = None,
    knowledge_root: str | Path = KNOWLEDGE_ROOT,
) -> list[str]:
    rules = rules if rules is not None else load_atomic_rules()
    issues: list[str] = []

    seen: set[str] = set()
    for rule in rules:
        if rule.rule_id in seen:
            issues.append(f"重复的 rule_id: {rule.rule_id}")
        seen.add(rule.rule_id)

    doc_ids = {doc["metadata"].get("doc_id") for doc in parse_knowledge_documents(knowledge_root)}
    for rule in rules:
        if not rule.title.strip():
            issues.append(f"{rule.rule_id} 缺少中文标题 title")
        if rule.provenance.source_doc not in doc_ids:
            issues.append(f"{rule.rule_id} 出处 doc {rule.provenance.source_doc!r} 不存在于知识库")
        if rule.check_type == "deterministic" and rule.requirement is None:
            issues.append(f"{rule.rule_id} 是 deterministic 但缺 requirement")
        if rule.check_type == "llm_judge" and rule.requirement is not None:
            issues.append(f"{rule.rule_id} 是 llm_judge 却带了 requirement")
        if rule.applicability_condition:
            try:
                evaluate_condition(rule.applicability_condition, {})
            except Exception as exc:  # noqa: BLE001 - 适用条件应能被安全求值
                issues.append(f"{rule.rule_id} 适用条件无法求值: {exc}")

    return issues


def rules_for_process(rules: list[AtomicRule], process_id: str, domains: set[str] | None = None) -> list[AtomicRule]:
    """结构化预筛：锚定该流程的规则 + 命中给定业务域的规则（Phase 2 检索的确定性前置）。"""
    domains = domains or set()
    result: list[AtomicRule] = []
    for rule in rules:
        if process_id in rule.applies_to_processes or (domains & set(rule.applies_to_domains)):
            result.append(rule)
    return result
