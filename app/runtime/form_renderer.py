from __future__ import annotations

from typing import Any

from data.schema import ProcessDefinition


def render_form_schema(process: ProcessDefinition, *, node_id: str = "draft") -> list[dict[str, Any]]:
    fields: list[dict[str, Any]] = []
    for field in process.form_fields:
        fields.append(
            {
                "seq": field.seq,
                "field_name": field.field_name,
                "component_type": field.component_type.value,
                "required": _stage_matches(field.required_stages, node_id),
                "visible": _stage_matches(field.visible_stages, node_id),
                "editable": _stage_matches(field.editable_stages, node_id),
                "default_value": field.default_value,
                "options": field.options or [],
                "placeholder": field.placeholder,
                "max_length": field.max_length,
                "logic_description": field.logic_description,
            }
        )
    return fields


def _stage_matches(stages: list[str], node_id: str) -> bool:
    return "all" in stages or node_id in stages
