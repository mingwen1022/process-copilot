"""ProcessDefinition → 飞书原生审批定义（确定性翻译）。

飞书审批 create-definition API 的形状（已核对官方文档）：
- 顶层：approval_name / approval_code? / description? / form / node_list / viewers / i18n_resources
- form.form_content：一段 **JSON 字符串**，内容是控件数组 [{id,type,name,required,...}]
- node_list：固定 START 开头、END 结尾，中间是审批节点
    审批节点：{id, name, node_type(AND/OR/SEQUENTIAL), approver:[{type,...}]}
- 文案走 i18n：name 用 "@i18n@key"，再在 i18n_resources 里给 zh-CN 文案

约束（写死在翻译里，不猜）：
- **只做线性**：flow_nodes 按顺序展开成审批链；条件分支/退回路径被忽略并记 note
- 控件类型按 _COMPONENT_TYPE_MAP 映射；飞书没有的（只读/文号/文字链接）降级为 input 并记 note
- 审批人默认用「直属主管逐级」(Supervisor level 1,2,3…)，因为 demo 租户没有真实组织映射；
  可用 approver_overrides 注入真实 user_id（type=Personal）覆盖某个 node
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from data.schema import (
    ComponentType,
    HandlerMode,
    ProcessDefinition,
)

# 表单控件类型映射：我们的 ComponentType → 飞书 form control type 字符串。
# 下面这些 type 及其"创建定义时的必填子字段"都是对着真实飞书 API 探针实测出来的
# （见 _CONTROL_EXTRA_FIELDS），不是照文档猜——飞书控件子格式文档查不到。
_COMPONENT_TYPE_MAP: dict[ComponentType, str] = {
    ComponentType.TEXT: "input",
    ComponentType.TEXTAREA: "textarea",
    ComponentType.NUMBER: "number",
    ComponentType.DATE: "date",
    ComponentType.DATETIME: "date",
    ComponentType.EMPLOYEE: "contact",
    ComponentType.DEPARTMENT: "department",
    ComponentType.ATTACHMENT: "attachmentV2",
}
# 各飞书控件在"创建定义"时的必填额外字段（探针实测：缺了会报 xxx value nil / parse fail）。
_CONTROL_EXTRA_FIELDS: dict[str, dict[str, Any]] = {
    # date 的 value 是**日期格式串**不是空串——读回真实日期控件对比发现：value:"" 能创建
    # 但渲染成点不动的死控件；须给 "YYYY-MM-DD"。widget_default_value:"0" 与真机一致。
    "date": {"value": "YYYY-MM-DD", "widget_default_value": "0"},
    "contact": {"value": {}},   # value:"" 会 parse fail，须为对象
}
# 飞书基础控件里没有直接对应、降级为普通输入框的类型（会记 note）。
_DEGRADE_TO_INPUT = {
    ComponentType.READONLY: "只读文本",
    ComponentType.FILE_NUMBER: "文号",
    ComponentType.LINK: "文字链接",
}
# 单选/多选：radioV2 的 option 子格式暂未攻克（探针试了 15 种都报 'radio format err'），
# 先降级为输入框；接入方式是读回一个真实带单选的定义拿准确格式（见设计文档）。
_SELECT_TYPES = {ComponentType.SELECT_SINGLE, ComponentType.SELECT_MULTI, ComponentType.RADIO}

# 处理人方式 → 飞书节点类型：并行=会签(AND)，抢办/单人=或签(OR)。
_NODE_TYPE_MAP: dict[HandlerMode, str] = {
    HandlerMode.SINGLE: "OR",
    HandlerMode.MULTI_PARALLEL: "AND",
    HandlerMode.MULTI_PREEMPT: "OR",
    HandlerMode.ALL_PARALLEL: "AND",
    HandlerMode.ALL_PREEMPT: "OR",
}


@dataclass
class FeishuTranslation:
    """翻译结果：可直接 POST 的定义体 + 有损映射说明（notes）。"""

    definition: dict[str, Any]
    notes: list[str] = field(default_factory=list)
    linearized: bool = False  # 是否把条件分支/多路径线性化了


def _i18n(key: str) -> str:
    return f"@i18n@{key}"


def _field_required_at_submit(required_stages: list[str], draft_node_id: str | None) -> bool:
    """飞书表单的 required 是"提交时是否必填"。我们的 required_stages 是按环节的，
    提交=起草环节，所以起草环节必填 或 全环节必填 即为飞书必填。"""
    return "all" in required_stages or (draft_node_id is not None and draft_node_id in required_stages)


def translate_to_feishu_approval(
    process: ProcessDefinition,
    *,
    approver_overrides: dict[str, list[dict[str, Any]]] | None = None,
    name_override: str | None = None,
    locale: str = "zh-CN",
    viewer_type: str = "TENANT",
) -> FeishuTranslation:
    """把 ProcessDefinition 翻译成飞书审批 create-definition 请求体。

    approver_overrides: {node_id: [{"type":"Personal","user_id":"ou_xxx"}]}，
    给某个节点指定真实审批人（覆盖默认的逐级主管）。
    name_override: 覆盖审批定义名称（飞书原生已有"请假"等，需避免重名）。

    控件保真：date/number/员工/部门/附件/多行/单行都已按探针实测的准确格式落地；
    只有单选/多选（radioV2 option 子格式未攻克）仍降级为输入框，并记 note。
    """
    approver_overrides = approver_overrides or {}
    notes: list[str] = []
    i18n_texts: list[dict[str, str]] = []

    def add_text(key: str, value: str) -> str:
        i18n_texts.append({"key": _i18n(key), "value": value})
        return _i18n(key)

    draft_node = next((n for n in process.flow_nodes if n.is_draft), None)
    draft_node_id = draft_node.node_id if draft_node else None

    # ---- 表单控件 ----
    form_controls: list[dict[str, Any]] = []
    for f in process.form_fields:
        if f.component_type in _DEGRADE_TO_INPUT:
            ftype = "input"
            notes.append(f"字段「{f.field_name}」（{_DEGRADE_TO_INPUT[f.component_type]}）飞书无对应控件，降级为单行文本")
        elif f.component_type in _SELECT_TYPES:
            ftype = "input"
            notes.append(f"字段「{f.field_name}」（单选/多选）暂降级为输入框（飞书 radioV2 option 子格式未接入）")
        elif f.component_type in _COMPONENT_TYPE_MAP:
            ftype = _COMPONENT_TYPE_MAP[f.component_type]
        else:  # 兜底，理论上枚举已覆盖
            ftype = "input"
            notes.append(f"字段「{f.field_name}」组件类型未知，降级为单行文本")

        widget_id = f"field_{f.seq}"
        control: dict[str, Any] = {
            "id": widget_id,
            "type": ftype,
            "name": add_text(f"{widget_id}_name", f.field_name),
            "required": _field_required_at_submit(f.required_stages, draft_node_id),
        }
        control.update(_CONTROL_EXTRA_FIELDS.get(ftype, {}))  # date→value:"", contact→value:{}
        form_controls.append(control)

    # ---- 审批节点（线性）----
    approval_nodes = [n for n in process.flow_nodes if not n.is_draft]
    node_list: list[dict[str, Any]] = [{"id": "START"}]

    for level, node in enumerate(approval_nodes, start=1):
        # 分支检测：若该环节有多条通向不同「真实后继环节」的路径，说明被线性化了
        forward_targets = {
            p.target_node_id
            for p in node.submit_paths
            if p.target_node_id not in ("END", "DRAFT", node.node_id)
        }
        if len(forward_targets) > 1 or any(p.condition for p in node.submit_paths if p.target_node_id not in ("DRAFT",)):
            notes.append(f"环节「{node.node_name}」的条件/分支路径被线性化（飞书原生审批 API 只支持线性）")

        node_type = _NODE_TYPE_MAP.get(node.handler.mode, "OR") if node.handler else "OR"
        approver = approver_overrides.get(node.node_id) or [{"type": "Supervisor", "level": str(level)}]
        if node.node_id not in approver_overrides:
            notes.append(f"环节「{node.node_name}」审批人默认映射为第 {level} 级直属主管（可用 approver_overrides 指定真实人）")

        node_list.append(
            {
                "id": node.node_id,
                "name": add_text(f"node_{node.node_id}", node.node_name),
                "node_type": node_type,
                "approver": approver,
            }
        )

    node_list.append({"id": "END"})

    linearized = any("线性化" in n for n in notes)

    definition = {
        "approval_name": add_text("approval_name", name_override or process.meta.process_name),
        "description": add_text("approval_desc", process.meta.description or process.meta.process_name),
        "viewers": [{"viewer_type": viewer_type}],
        "form": {"form_content": json.dumps(form_controls, ensure_ascii=False)},
        "node_list": node_list,
        "i18n_resources": [{"locale": locale, "texts": i18n_texts, "is_default": True}],
    }

    return FeishuTranslation(definition=definition, notes=notes, linearized=linearized)
