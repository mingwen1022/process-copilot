from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any

from data.schema import ComponentType, HandlerMode, HandlerSource, ProcessDefinition


CONVENTIONS_PATH = Path(__file__).resolve().parents[1] / "data" / "config" / "process_design_conventions.json"


@lru_cache(maxsize=1)
def load_process_design_conventions() -> dict[str, Any]:
    return json.loads(CONVENTIONS_PATH.read_text(encoding="utf-8"))


def validate_convention_config(conventions: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """Validate that convention values are compatible with the local schema enums."""
    data = conventions or load_process_design_conventions()
    allowed_components = {item.value for item in ComponentType}
    allowed_handler_sources = {item.value for item in HandlerSource}
    errors: list[dict[str, Any]] = []

    global_rules = data.get("global_rules", {})
    preferred = global_rules.get("component_type_policy", {}).get("enum_field_preferred_components", [])
    for value in preferred:
        if value not in allowed_components:
            errors.append({"loc": ["global_rules", "component_type_policy"], "msg": f"unknown component type: {value}"})

    for case_key, case_rules in data.get("case_rules", {}).items():
        for index, rule in enumerate(case_rules.get("field_rules", [])):
            value = rule.get("expected_component_type")
            if value and value not in allowed_components:
                errors.append({"loc": ["case_rules", case_key, "field_rules", index], "msg": f"unknown component type: {value}"})
        for index, rule in enumerate(case_rules.get("flow_node_rules", [])):
            value = rule.get("handler_source")
            if value and value not in allowed_handler_sources:
                errors.append({"loc": ["case_rules", case_key, "flow_node_rules", index], "msg": f"unknown handler source: {value}"})
    return errors


def render_conventions_for_prompt(conventions: dict[str, Any] | None = None) -> str:
    """流程系统支持的"组件目录/口径"（与具体流程无关的通用词表）。

    只描述系统能力（组件类型、处理人来源/方式、条件短语、口径），
    不含任何案例级答案（节点、handler、字段类型的具体取值），以免向抽取
    agent 泄漏 gold。案例级规范留在 ``validate_process_conventions`` 做事后校验。
    """
    data = conventions or load_process_design_conventions()
    global_rules = data.get("global_rules", {})
    component_policy = global_rules.get("component_type_policy", {})
    required_policy = global_rules.get("required_stage_policy", {})
    condition_policy = global_rules.get("condition_policy", {})

    component_values = "、".join(item.value for item in ComponentType)
    handler_source_values = "、".join(item.value for item in HandlerSource)
    handler_mode_values = "、".join(item.value for item in HandlerMode)
    preferred = "、".join(component_policy.get("enum_field_preferred_components", []))
    canonical = "、".join(condition_policy.get("canonical_phrases", []))
    unsupported = "、".join(condition_policy.get("unsupported_actions", []))

    return f"""流程设计输出规范（系统支持的组件与口径，强约束，优先于自由表述；以下为系统能力词表，不针对具体流程）：

【组件类型词表】form_fields.component_type 只能取以下枚举值：{component_values}。
- 业务枚举字段优先使用 {preferred}；{component_policy.get('radio_usage', '')}
- {component_policy.get('options_policy', '')}

【处理人配置词表】flow_nodes.handler.source 只能取：{handler_source_values}；handler.mode 只能取：{handler_mode_values}。
- 当 handler.source=表单字段指定 时，必须给出 handler.source_field（指明从哪个表单字段取处理人）。

【必填环节口径】{required_policy.get('meaning', '')}{required_policy.get('auto_field_policy', '')}{required_policy.get('draft_required_policy', '')}

【路径条件口径】submit_paths.condition 优先使用固定短语：{canonical}。
- {condition_policy.get('phrase_glossary', '')}
- 当前 demo 不支持的动作（{unsupported}）：{condition_policy.get('unsupported_action_policy', '')}
"""


def validate_process_conventions(process: ProcessDefinition) -> list[dict[str, Any]]:
    """Return non-blocking convention warnings for deterministic validator reports."""
    conventions = load_process_design_conventions()
    warnings: list[dict[str, Any]] = []
    warnings.extend(_validate_global_component_options(process))

    case_rules = _applicable_case_rules(process, conventions)
    if not case_rules:
        return warnings

    warnings.extend(_validate_case_field_rules(process, case_rules))
    warnings.extend(_validate_case_node_rules(process, case_rules))
    warnings.extend(_validate_unsupported_paths(process, conventions))
    return warnings


def _applicable_case_rules(process: ProcessDefinition, conventions: dict[str, Any]) -> dict[str, Any] | None:
    activation = conventions.get("activation", {})
    keywords = activation.get("process_name_keywords", [])
    haystack = " ".join(
        [
            process.meta.process_id or "",
            process.meta.process_name or "",
            process.meta.description or "",
        ]
    )
    if any(keyword and keyword in haystack for keyword in keywords):
        return conventions.get("case_rules", {}).get("EOA140_subsidiary_major_matter")
    return None


def _validate_global_component_options(process: ProcessDefinition) -> list[dict[str, Any]]:
    warnings: list[dict[str, Any]] = []
    option_components = {ComponentType.SELECT_SINGLE.value, ComponentType.SELECT_MULTI.value, ComponentType.RADIO.value}
    for index, field in enumerate(process.form_fields):
        if field.component_type.value in option_components and not field.options:
            warnings.append(
                _warning(
                    loc=["form_fields", index, "options"],
                    msg=f"{field.field_name} 使用 {field.component_type.value}，但 options 为空",
                    convention_id="component_options_required",
                    expected="非空 options",
                    actual=field.options,
                )
            )
    return warnings


def _validate_case_field_rules(process: ProcessDefinition, case_rules: dict[str, Any]) -> list[dict[str, Any]]:
    fields_by_name = {field.field_name: (index, field) for index, field in enumerate(process.form_fields)}
    warnings: list[dict[str, Any]] = []
    for rule in case_rules.get("field_rules", []):
        field_name = rule.get("field_name")
        if field_name not in fields_by_name:
            continue
        index, field = fields_by_name[field_name]
        expected_component = rule.get("expected_component_type")
        if expected_component and field.component_type.value != expected_component:
            warnings.append(
                _warning(
                    loc=["form_fields", index, "component_type"],
                    msg=f"{field_name} 组件类型应为 {expected_component}，实际为 {field.component_type.value}",
                    convention_id="field_component_type",
                    expected=expected_component,
                    actual=field.component_type.value,
                )
            )
        if "expected_required_stages" in rule and field.required_stages != rule["expected_required_stages"]:
            warnings.append(
                _warning(
                    loc=["form_fields", index, "required_stages"],
                    msg=f"{field_name} 必填环节应为 {rule['expected_required_stages']}，实际为 {field.required_stages}",
                    convention_id="field_required_stages",
                    expected=rule["expected_required_stages"],
                    actual=field.required_stages,
                )
            )
        if "expected_editable_stages" in rule and field.editable_stages != rule["expected_editable_stages"]:
            warnings.append(
                _warning(
                    loc=["form_fields", index, "editable_stages"],
                    msg=f"{field_name} 可编辑环节应为 {rule['expected_editable_stages']}，实际为 {field.editable_stages}",
                    convention_id="field_editable_stages",
                    expected=rule["expected_editable_stages"],
                    actual=field.editable_stages,
                )
            )
        if rule.get("options_required") and not field.options:
            warnings.append(
                _warning(
                    loc=["form_fields", index, "options"],
                    msg=f"{field_name} 是枚举字段，必须写入 options",
                    convention_id="field_options_required",
                    expected="非空 options",
                    actual=field.options,
                )
            )
    return warnings


def _validate_case_node_rules(process: ProcessDefinition, case_rules: dict[str, Any]) -> list[dict[str, Any]]:
    nodes_by_name = {node.node_name: (index, node) for index, node in enumerate(process.flow_nodes)}
    warnings: list[dict[str, Any]] = []
    for rule in case_rules.get("flow_node_rules", []):
        node_name = rule.get("node_name")
        if node_name not in nodes_by_name:
            warnings.append(
                _warning(
                    loc=["flow_nodes"],
                    msg=f"缺少规范环节：{node_name}",
                    convention_id="missing_convention_node",
                    expected=node_name,
                    actual=None,
                )
            )
            continue
        index, node = nodes_by_name[node_name]
        expected_node_id = rule.get("node_id")
        if expected_node_id and node.node_id != expected_node_id:
            warnings.append(
                _warning(
                    loc=["flow_nodes", index, "node_id"],
                    msg=f"{node_name} 推荐 node_id 为 {expected_node_id}，实际为 {node.node_id}",
                    convention_id="node_id",
                    expected=expected_node_id,
                    actual=node.node_id,
                )
            )
        expected_source = rule.get("handler_source")
        if expected_source and node.handler and node.handler.source.value != expected_source:
            warnings.append(
                _warning(
                    loc=["flow_nodes", index, "handler", "source"],
                    msg=f"{node_name} 处理人来源应为 {expected_source}，实际为 {node.handler.source.value}",
                    convention_id="handler_source",
                    expected=expected_source,
                    actual=node.handler.source.value,
                )
            )
        expected_source_field = rule.get("source_field")
        if expected_source_field and node.handler and node.handler.source_field != expected_source_field:
            warnings.append(
                _warning(
                    loc=["flow_nodes", index, "handler", "source_field"],
                    msg=f"{node_name} source_field 应为 {expected_source_field}，实际为 {node.handler.source_field}",
                    convention_id="handler_source_field",
                    expected=expected_source_field,
                    actual=node.handler.source_field,
                )
            )
    return warnings


def _validate_unsupported_paths(process: ProcessDefinition, conventions: dict[str, Any]) -> list[dict[str, Any]]:
    unsupported = conventions.get("global_rules", {}).get("condition_policy", {}).get("unsupported_actions", [])
    warnings: list[dict[str, Any]] = []
    for node_index, node in enumerate(process.flow_nodes):
        for path_index, path in enumerate(node.submit_paths):
            text = " ".join([path.path_name or "", path.condition or ""])
            if any(keyword and keyword in text for keyword in unsupported):
                warnings.append(
                    _warning(
                        loc=["flow_nodes", node_index, "submit_paths", path_index],
                        msg=f"{node.node_name} 的路径包含当前 demo 不支持的高级动作，应进入待确认项或 schema gap：{path.path_name}",
                        convention_id="unsupported_action_path",
                        expected="不作为普通 submit_path 输出",
                        actual={"path_name": path.path_name, "condition": path.condition},
                    )
                )
    return warnings


def _warning(*, loc: list[Any], msg: str, convention_id: str, expected: Any, actual: Any) -> dict[str, Any]:
    return {
        "loc": loc,
        "msg": msg,
        "type": "process_design_convention",
        "severity": "warning",
        "convention_id": convention_id,
        "expected": expected,
        "actual": actual,
    }
