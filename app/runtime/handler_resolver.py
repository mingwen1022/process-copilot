from __future__ import annotations

from typing import Any

from app.org_knowledge import ResolvedUser, resolve_role
from data.schema import FlowNode, HandlerMode, ProcessDefinition


def resolve_node_assignees(
    org_data: dict[str, Any],
    process: ProcessDefinition,
    node: FlowNode,
    *,
    initiator_user_id: str,
    form_values: dict[str, Any],
) -> list[ResolvedUser]:
    if node.is_draft or node.handler is None:
        return resolve_role(org_data, "起草人", applicant_user_id=initiator_user_id)

    handler = node.handler
    if handler.source_field:
        from_field = _resolve_user_from_form_field(org_data, form_values.get(handler.source_field))
        if from_field:
            return [from_field]

    users = resolve_role(org_data, handler.role, applicant_user_id=initiator_user_id)
    return _apply_handler_mode(users, handler.mode)


def _apply_handler_mode(users: list[ResolvedUser], mode: HandlerMode) -> list[ResolvedUser]:
    unique = _dedupe_users(users)
    if not unique:
        return []
    if mode == HandlerMode.SINGLE or "单选" in str(mode):
        return [unique[0]]
    return unique


def _dedupe_users(users: list[ResolvedUser]) -> list[ResolvedUser]:
    result: list[ResolvedUser] = []
    seen: set[str] = set()
    for user in users:
        if user.user_id and user.user_id not in seen:
            result.append(user)
            seen.add(user.user_id)
    return result


def _resolve_user_from_form_field(org_data: dict[str, Any], raw_value: Any) -> ResolvedUser | None:
    if not raw_value:
        return None
    values = raw_value if isinstance(raw_value, list) else [raw_value]
    users = org_data.get("users", [])
    for value in values:
        text = str(value)
        for user in users:
            if text in {user.get("user_id"), user.get("name"), user.get("employee_no")}:
                return resolve_role(org_data, "起草人", applicant_user_id=user["user_id"])[0]
    return None
