"""对话式编辑前后 ProcessDefinition 的人可读 diff。

与 app/reporting/comparison.py 的 gold-vs-actual 语义不同：这里是对称的
"编辑前 vs 编辑后"对比，用于把一次对话编辑的实际影响展示给用户确认。
"""

from __future__ import annotations

from typing import Any, Callable, Iterable

from data.schema import ProcessDefinition

_FORM_FIELD_ATTRS = [
    "seq",
    "component_type",
    "required_stages",
    "visible_stages",
    "editable_stages",
    "logic_description",
    "default_value",
    "options",
    "placeholder",
    "max_length",
]
_FLOW_NODE_ATTRS = ["node_name", "handler", "opinion", "opinion_label", "time_limit_days"]
_SUBMIT_PATH_ATTRS = ["condition", "target_node_id"]
_ATTACHMENT_ATTRS = ["upload_stages", "required_stages", "required_condition"]
_ROLE_ATTRS = ["role_type", "members", "department"]
_META_ATTRS = ["process_name", "responsible_dept", "description", "applicant_scope", "entry_point"]


def diff_process_definitions(before: ProcessDefinition, after: ProcessDefinition) -> dict[str, Any]:
    diff = {
        "meta": _diff_attrs(before.meta, after.meta, _META_ATTRS),
        "form_fields": _diff_named_list(
            before.form_fields, after.form_fields, key=lambda item: item.field_name, attrs=_FORM_FIELD_ATTRS
        ),
        "flow_nodes": _diff_flow_nodes(before.flow_nodes, after.flow_nodes),
        "attachments": _diff_named_list(
            before.attachments or [], after.attachments or [], key=lambda item: item.attachment_type, attrs=_ATTACHMENT_ATTRS
        ),
        "roles": _diff_named_list(
            before.roles or [], after.roles or [], key=lambda item: item.role_name, attrs=_ROLE_ATTRS
        ),
    }
    diff["has_changes"] = any(
        diff["meta"]
        or section["added"]
        or section["removed"]
        or section["changed"]
        for section in (diff["form_fields"], diff["flow_nodes"], diff["attachments"], diff["roles"])
    ) or bool(diff["meta"])
    return diff


def _diff_attrs(before: Any, after: Any, attrs: list[str]) -> list[dict[str, Any]]:
    changes: list[dict[str, Any]] = []
    for attr in attrs:
        before_value = _jsonable(getattr(before, attr, None))
        after_value = _jsonable(getattr(after, attr, None))
        if before_value != after_value:
            changes.append({"attribute": attr, "before": before_value, "after": after_value})
    return changes


def _diff_named_list(
    before_items: list[Any],
    after_items: list[Any],
    *,
    key: Callable[[Any], str],
    attrs: list[str],
) -> dict[str, Any]:
    before_by_key = {key(item): item for item in before_items}
    after_by_key = {key(item): item for item in after_items}
    added = [name for name in after_by_key if name not in before_by_key]
    removed = [name for name in before_by_key if name not in after_by_key]
    # 纯换位置（没有任何属性变化、也没有增删）本来会被漏掉——按 key 比对属性，从不
    # 看列表顺序本身。这里不新增排序字段（大部分类型 schema 里也没有），改成比较
    # "改前/改后紧挨在这个 key 前面的是谁"：只有这个"上一个邻居"真的变了才算它被
    # 移动过；纯粹因为前面插入/删除了别的项而被动往后挪的下标，邻居关系没变，不算。
    before_predecessor = _predecessor_map(before_by_key.keys())
    after_predecessor = _predecessor_map(after_by_key.keys())
    changed = []
    for name, after_item in after_by_key.items():
        before_item = before_by_key.get(name)
        if before_item is None:
            continue
        attr_changes = _diff_attrs(before_item, after_item, attrs)
        if before_predecessor[name] != after_predecessor[name]:
            attr_changes.append({"attribute": "_order", "before": before_predecessor[name], "after": after_predecessor[name]})
        if attr_changes:
            changed.append({"key": name, "changes": attr_changes})
    return {"added": added, "removed": removed, "changed": changed}


def _predecessor_map(keys: Iterable[str]) -> dict[str, str | None]:
    ordered = list(keys)
    return {k: (ordered[i - 1] if i > 0 else None) for i, k in enumerate(ordered)}


def _diff_flow_nodes(before_nodes: list[Any], after_nodes: list[Any]) -> dict[str, Any]:
    result = _diff_named_list(before_nodes, after_nodes, key=lambda node: node.node_id, attrs=_FLOW_NODE_ATTRS)
    before_by_id = {node.node_id: node for node in before_nodes}
    after_by_id = {node.node_id: node for node in after_nodes}
    for node_id, after_node in after_by_id.items():
        before_node = before_by_id.get(node_id)
        if before_node is None:
            continue
        path_diff = _diff_named_list(
            before_node.submit_paths, after_node.submit_paths, key=lambda path: path.path_name, attrs=_SUBMIT_PATH_ATTRS
        )
        if path_diff["added"] or path_diff["removed"] or path_diff["changed"]:
            entry = next((item for item in result["changed"] if item["key"] == node_id), None)
            if entry is None:
                entry = {"key": node_id, "changes": []}
                result["changed"].append(entry)
            entry["submit_paths"] = path_diff
    return result


def _jsonable(value: Any) -> Any:
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    if hasattr(value, "value"):  # Enum
        return value.value
    return value


def render_diff_markdown(diff: dict[str, Any]) -> str:
    if not diff.get("has_changes"):
        return "本次未产生流程定义变更。"
    lines: list[str] = []
    if diff["meta"]:
        for change in diff["meta"]:
            lines.append(f"- 流程信息 {change['attribute']}：{change['before']!r} → {change['after']!r}")
    _append_section(lines, "字段", diff["form_fields"])
    _append_section(lines, "环节", diff["flow_nodes"], nested_key="submit_paths", nested_label="路径")
    _append_section(lines, "附件", diff["attachments"])
    _append_section(lines, "角色", diff["roles"])
    return "\n".join(lines)


def _append_section(
    lines: list[str],
    label: str,
    section: dict[str, Any],
    *,
    nested_key: str | None = None,
    nested_label: str | None = None,
) -> None:
    for name in section["added"]:
        lines.append(f"- 新增{label}“{name}”")
    for name in section["removed"]:
        lines.append(f"- 删除{label}“{name}”")
    for item in section["changed"]:
        for change in item.get("changes", []):
            lines.append(f"- 更新{label}“{item['key']}” {change['attribute']}：{change['before']!r} → {change['after']!r}")
        if nested_key and nested_key in item:
            nested = item[nested_key]
            for path_name in nested["added"]:
                lines.append(f"- {label}“{item['key']}”新增{nested_label}“{path_name}”")
            for path_name in nested["removed"]:
                lines.append(f"- {label}“{item['key']}”删除{nested_label}“{path_name}”")
            for path_change in nested["changed"]:
                for change in path_change.get("changes", []):
                    lines.append(
                        f"- {label}“{item['key']}”{nested_label}“{path_change['key']}” {change['attribute']}："
                        f"{change['before']!r} → {change['after']!r}"
                    )
