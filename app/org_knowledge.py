from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any


ORG_ROOT = Path("data/org")
KNOWLEDGE_ROOT = Path("data/knowledge")
DEFAULT_ORG_SEED = ORG_ROOT / "org_seed.json"


@dataclass(frozen=True, slots=True)
class OrgKnowledgeIssue:
    scope: str
    severity: str
    message: str


@dataclass(frozen=True, slots=True)
class ResolvedUser:
    user_id: str
    name: str
    dept_id: str | None
    dept_name: str | None
    position_code: str | None
    title: str | None
    source: str


def load_org_seed(path: str | Path = DEFAULT_ORG_SEED) -> dict[str, Any]:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def validate_org_seed(path: str | Path = DEFAULT_ORG_SEED) -> list[OrgKnowledgeIssue]:
    data = load_org_seed(path)
    issues: list[OrgKnowledgeIssue] = []

    users = _index_by_id(data.get("users", []), "user_id")
    lines = _index_by_id(data.get("lines", []), "line_id")
    departments = _index_by_id(data.get("departments", []), "dept_id")
    positions = {item.get("position_code") for item in data.get("positions", [])}
    assignments = data.get("position_assignments", [])
    role_aliases = data.get("role_aliases", [])

    issues.extend(_validate_unique_ids(data.get("users", []), "user_id", "users"))
    issues.extend(_validate_unique_ids(data.get("lines", []), "line_id", "lines"))
    issues.extend(_validate_unique_ids(data.get("departments", []), "dept_id", "departments"))
    issues.extend(_validate_unique_ids(data.get("role_aliases", []), "role_alias", "role_aliases"))

    for line in data.get("lines", []):
        leader = line.get("leader_user_id")
        if leader and leader not in users:
            issues.append(OrgKnowledgeIssue("org", "error", f"line {line.get('line_id')} references missing leader {leader}"))

    for dept in data.get("departments", []):
        dept_id = dept.get("dept_id")
        line_id = dept.get("line_id")
        parent = dept.get("parent_dept_id")
        manager = dept.get("manager_user_id")
        if dept.get("dept_type") != "治理层" and line_id not in lines:
            issues.append(OrgKnowledgeIssue("org", "error", f"department {dept_id} missing valid line_id"))
        if parent and parent not in departments:
            issues.append(OrgKnowledgeIssue("org", "error", f"department {dept_id} references missing parent {parent}"))
        if manager and manager not in users:
            issues.append(OrgKnowledgeIssue("org", "error", f"department {dept_id} references missing manager {manager}"))

    primary_by_user: dict[str, list[dict[str, Any]]] = {user_id: [] for user_id in users}
    for assignment in assignments:
        user_id = assignment.get("user_id")
        dept_id = assignment.get("dept_id")
        position = assignment.get("position_code")
        if user_id not in users:
            issues.append(OrgKnowledgeIssue("org", "error", f"assignment references missing user {user_id}"))
        if dept_id not in departments:
            issues.append(OrgKnowledgeIssue("org", "error", f"assignment references missing department {dept_id}"))
        if position not in positions:
            issues.append(OrgKnowledgeIssue("org", "error", f"assignment references missing position {position}"))
        if assignment.get("is_primary") and user_id in primary_by_user:
            primary_by_user[user_id].append(assignment)

    for user_id, user in users.items():
        if not primary_by_user[user_id]:
            issues.append(OrgKnowledgeIssue("org", "error", f"user {user_id}/{user.get('name')} has no primary assignment"))

    for alias in role_aliases:
        rule = alias.get("resolver_rule", {})
        for member in rule.get("members", []):
            if member not in users:
                issues.append(OrgKnowledgeIssue("org", "error", f"role {alias.get('role_alias')} references missing member {member}"))
        target_dept = rule.get("target_dept_id")
        if target_dept and target_dept not in departments:
            issues.append(OrgKnowledgeIssue("org", "error", f"role {alias.get('role_alias')} references missing department {target_dept}"))
        for position in rule.get("position_codes", []):
            if position not in positions:
                issues.append(OrgKnowledgeIssue("org", "error", f"role {alias.get('role_alias')} references missing position {position}"))

    return issues


def parse_knowledge_documents(root: str | Path = KNOWLEDGE_ROOT) -> list[dict[str, Any]]:
    """递归加载知识库语料（新版按维度分子目录）。仅做通用加载，不做旧版那套
    硬编码流程覆盖校验——语料的原子规则化与检索由模块3的新组件负责。"""
    docs: list[dict[str, Any]] = []
    for path in sorted(Path(root).rglob("*.md")):
        text = path.read_text(encoding="utf-8")
        metadata, body = _parse_frontmatter(text)
        # 只把带 doc_id frontmatter 的当作语料；README、评测 scorecard 等无 frontmatter
        # 的 md 一律跳过，避免污染知识库。
        if not metadata.get("doc_id"):
            continue
        docs.append({"path": str(path), "metadata": metadata, "body": body})
    return docs


def validate_org_knowledge(
    org_path: str | Path = DEFAULT_ORG_SEED,
    knowledge_root: str | Path = KNOWLEDGE_ROOT,  # noqa: ARG001 - 保留签名兼容；知识校验已移交模块3
) -> list[OrgKnowledgeIssue]:
    """只校验组织主数据。旧版的知识文档硬编码校验（含已退役的 EXPENSE/SEAL/PROC
    流程覆盖检查）已废弃——新知识库的校验在模块3的原子规则层。"""
    return validate_org_seed(org_path)


def resolve_role(
    data: dict[str, Any],
    role_alias: str,
    *,
    applicant_user_id: str,
) -> list[ResolvedUser]:
    role_index = {item["role_alias"]: item for item in data.get("role_aliases", [])}
    alias = role_index.get(role_alias)
    if not alias:
        return []
    rule = alias.get("resolver_rule", {})
    rule_type = rule.get("type")

    if rule_type == "direct_members":
        return [_resolved_from_user(data, user_id, "direct_members") for user_id in rule.get("members", [])]

    if rule_type == "applicant_self":
        return [_resolved_from_user(data, applicant_user_id, rule_type)]

    if rule_type == "applicant_department_position":
        dept_id = _primary_dept_id(data, applicant_user_id)
        if not dept_id:
            return []
        if rule.get("scope") in {"nearest", "parent_or_current"}:
            candidates = _find_position_in_dept_chain(data, dept_id, rule.get("position_codes", []), include_current=True)
        else:
            candidates = _find_position_in_dept_chain(data, dept_id, rule.get("position_codes", []), include_current=False)
        return [_resolved_from_assignment(data, item, rule_type) for item in candidates]

    if rule_type == "applicant_line_leader":
        dept = _department(data, _primary_dept_id(data, applicant_user_id))
        if not dept:
            return []
        line = _line(data, dept.get("line_id"))
        leader = line.get("leader_user_id") if line else None
        return [_resolved_from_user(data, leader, rule_type)] if leader else []

    if rule_type == "specific_department_position":
        candidates = _assignments_for_department_positions(
            data,
            rule.get("target_dept_id"),
            rule.get("position_codes", []),
        )
        return [_resolved_from_assignment(data, item, rule_type) for item in candidates]

    return []


def user_exists(data: dict[str, Any], user_id: str | None) -> bool:
    """user_id 是不是组织里真实注册的用户——改派/授权这类"把处理人换成某个具体人"的操作，
    落地前必须过这道确定性校验，不能信 LLM 给的 id 就是真的（结构化输出偶尔会吐占位符，
    如 "<UNKNOWN>"，不校验就会把审批人静默改派成一个不存在的人）。"""
    if not user_id:
        return False
    return any(u.get("user_id") == user_id for u in data.get("users", []))


def user_name(data: dict[str, Any], user_id: str | None) -> str:
    """user_id → 中文姓名，查不到就原样返回 id——供面向用户的文案（审批人展示、副驾回复）
    用，不能把内部 user_id 这种系统编号直接甩给用户看。确定性字典查找，不猜。"""
    if not user_id:
        return ""
    for u in data.get("users", []):
        if u.get("user_id") == user_id:
            return u.get("name") or user_id
    return user_id


def get_user_assignments(data: dict[str, Any], user_id: str) -> dict[str, list[dict[str, Any]]]:
    assignments = [item for item in data.get("position_assignments", []) if item.get("user_id") == user_id]
    return {
        "primary": [item for item in assignments if item.get("is_primary")],
        "secondary": [item for item in assignments if not item.get("is_primary")],
    }


def detect_same_actor_escalations(
    data: dict[str, Any],
    role_sequence: list[str],
    *,
    applicant_user_id: str,
) -> list[dict[str, Any]]:
    resolved = [(role, resolve_role(data, role, applicant_user_id=applicant_user_id)) for role in role_sequence]
    issues: list[dict[str, Any]] = []
    for index in range(1, len(resolved)):
        previous_role, previous_users = resolved[index - 1]
        current_role, current_users = resolved[index]
        previous_ids = {item.user_id for item in previous_users}
        current_ids = {item.user_id for item in current_users}
        overlap = sorted(previous_ids & current_ids)
        if overlap:
            issues.append(
                {
                    "previous_role": previous_role,
                    "current_role": current_role,
                    "overlap_user_ids": overlap,
                    "recommendation": "需跳过或升级，不在 V2.1 自动执行",
                }
            )
    return issues


def _index_by_id(items: list[dict[str, Any]], key: str) -> dict[str, dict[str, Any]]:
    return {item[key]: item for item in items if key in item}


def _validate_unique_ids(items: list[dict[str, Any]], key: str, scope: str) -> list[OrgKnowledgeIssue]:
    issues: list[OrgKnowledgeIssue] = []
    seen: set[str] = set()
    for item in items:
        value = item.get(key)
        if not value:
            issues.append(OrgKnowledgeIssue("org", "error", f"{scope} item missing {key}"))
        elif value in seen:
            issues.append(OrgKnowledgeIssue("org", "error", f"{scope} has duplicate {key}: {value}"))
        else:
            seen.add(value)
    return issues


def _parse_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    if not text.startswith("---\n"):
        return {}, text
    end = text.find("\n---", 4)
    if end == -1:
        return {}, text
    raw = text[4:end].strip()
    body = text[end + len("\n---") :].strip()
    metadata: dict[str, Any] = {}
    for line in raw.splitlines():
        if not line.strip() or ":" not in line:
            continue
        key, value = line.split(":", 1)
        metadata[key.strip()] = _parse_metadata_value(value.strip())
    return metadata, body


def _parse_metadata_value(value: str) -> Any:
    if value.startswith("[") and value.endswith("]"):
        inner = value[1:-1].strip()
        if not inner:
            return []
        return [item.strip().strip("\"'") for item in inner.split(",")]
    return value.strip("\"'")


def _primary_dept_id(data: dict[str, Any], user_id: str) -> str | None:
    for assignment in data.get("position_assignments", []):
        if assignment.get("user_id") == user_id and assignment.get("is_primary"):
            return assignment.get("dept_id")
    return None


def _department(data: dict[str, Any], dept_id: str | None) -> dict[str, Any] | None:
    if not dept_id:
        return None
    return _index_by_id(data.get("departments", []), "dept_id").get(dept_id)


def _line(data: dict[str, Any], line_id: str | None) -> dict[str, Any] | None:
    if not line_id:
        return None
    return _index_by_id(data.get("lines", []), "line_id").get(line_id)


def _assignments_for_department_positions(data: dict[str, Any], dept_id: str | None, positions: list[str]) -> list[dict[str, Any]]:
    return [
        assignment
        for assignment in data.get("position_assignments", [])
        if assignment.get("dept_id") == dept_id and assignment.get("position_code") in positions
    ]


def _find_position_in_dept_chain(
    data: dict[str, Any],
    dept_id: str,
    positions: list[str],
    *,
    include_current: bool,
) -> list[dict[str, Any]]:
    current = dept_id if include_current else (_department(data, dept_id) or {}).get("parent_dept_id")
    while current:
        candidates = _assignments_for_department_positions(data, current, positions)
        if candidates:
            return candidates
        current = (_department(data, current) or {}).get("parent_dept_id")
    return []


def _resolved_from_user(data: dict[str, Any], user_id: str | None, source: str) -> ResolvedUser:
    users = _index_by_id(data.get("users", []), "user_id")
    user = users.get(user_id or "", {})
    primary = next(
        (item for item in data.get("position_assignments", []) if item.get("user_id") == user_id and item.get("is_primary")),
        {},
    )
    return _resolved_from_assignment(data, primary, source) if primary else ResolvedUser(
        user_id=user_id or "",
        name=user.get("name", ""),
        dept_id=None,
        dept_name=None,
        position_code=None,
        title=None,
        source=source,
    )


def _resolved_from_assignment(data: dict[str, Any], assignment: dict[str, Any], source: str) -> ResolvedUser:
    users = _index_by_id(data.get("users", []), "user_id")
    departments = _index_by_id(data.get("departments", []), "dept_id")
    user = users.get(assignment.get("user_id"), {})
    dept = departments.get(assignment.get("dept_id"), {})
    return ResolvedUser(
        user_id=assignment.get("user_id", ""),
        name=user.get("name", ""),
        dept_id=assignment.get("dept_id"),
        dept_name=dept.get("dept_name"),
        position_code=assignment.get("position_code"),
        title=assignment.get("title"),
        source=source,
    )


def extract_amount_boundaries(text: str) -> list[str]:
    return re.findall(r"[<>＜＞≥≤]?\s*\d+(?:\.\d+)?\s*(?:万|万元|元)", text)
