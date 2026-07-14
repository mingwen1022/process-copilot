"""对话式修改的结构化编辑工具。

设计原则（与 §7 harness 一致）：LLM 只负责把自然语言指令解析成这里定义的
typed 操作；操作能否应用、怎么应用到 ProcessDefinition 上，是纯确定性代码，
不再经过任何 LLM 判断。找不到引用的字段/环节/路径时报错，不静默臆造。

范围限制（v1，简化 identity 语义，避免"部分更新"里 None 的歧义）：
- 更新操作不支持改名（field_name / node_id / path_name / role_name /
  attachment_type）；改名请用 remove + add 表达。
- 更新操作里某个属性传 None 表示"本次不改"，不是"清空"；需要显式清空的
  少数字段（如 time_limit_days、condition、department）用专门的
  clear_* 布尔标记表达，避免"没提到"和"清空"混淆。
"""

from __future__ import annotations

from typing import Annotated, Any, Literal, Union

from pydantic import BaseModel, Field

from data.schema import (
    AttachmentConfig,
    ComponentType,
    FlowNode,
    FormField,
    HandlerConfig,
    OpinionConfig,
    ProcessDefinition,
    RoleConfig,
    RoleType,
    SubmitPath,
)


# ──────────────────────────────────────────────
# 局部更新模型（字段为 None 表示不改；clear_* 用于显式清空）
# ──────────────────────────────────────────────


class FormFieldUpdate(BaseModel):
    required_stages: list[str] | None = None
    visible_stages: list[str] | None = None
    editable_stages: list[str] | None = None
    component_type: ComponentType | None = None
    logic_description: str | None = None
    default_value: str | None = None
    options: list[str] | None = None
    placeholder: str | None = None
    max_length: int | None = None


class FlowNodeUpdate(BaseModel):
    node_name: str | None = None
    handler: HandlerConfig | None = None
    opinion: OpinionConfig | None = None
    opinion_label: str | None = None
    time_limit_days: int | None = None
    clear_time_limit_days: bool = Field(default=False, description="显式清空处理期限（设为不限时）")


class SubmitPathUpdate(BaseModel):
    condition: str | None = None
    clear_condition: bool = Field(default=False, description="显式清空条件（设为无条件路径）")
    target_node_id: str | None = None
    new_path_name: str | None = Field(
        default=None,
        description="给这条路径改名时填新名字（用 UpdateSubmitPath.path_name 定位旧名、这里给新名）；不改名就留空。",
    )


class AttachmentUpdate(BaseModel):
    upload_stages: list[str] | None = None
    required_stages: list[str] | None = None
    required_condition: str | None = None
    clear_required_condition: bool = Field(default=False, description="显式清空必传条件（改为只按 required_stages 判断）")


class RoleUpdate(BaseModel):
    role_type: RoleType | None = None
    members: list[str] | None = None
    department: str | None = None
    clear_department: bool = Field(default=False, description="显式清空所属部门")


class MetaUpdate(BaseModel):
    process_name: str | None = None
    responsible_dept: str | None = None
    description: str | None = None
    applicant_scope: str | None = None
    entry_point: str | None = None


# ──────────────────────────────────────────────
# 编辑操作（判别联合，op 字段区分类型）
# ──────────────────────────────────────────────


class AddFormField(BaseModel):
    op: Literal["add_form_field"] = "add_form_field"
    field: FormField = Field(description="要新增的完整表单字段定义（field.seq 会被忽略——落库时按插入位置自动重新编号，不用自己算 seq）")
    after_field_name: str | None = Field(
        default=None,
        description=(
            "把新字段插到哪个现有字段之后（用于「在 A 和 B 之间插一个字段」——填 A 的 field_name，"
            "新字段就排在 A 后面，而不是追加到末尾）。留空＝追加到末尾。找不到该字段名时也追加到末尾。"
        ),
    )


class UpdateFormField(BaseModel):
    op: Literal["update_form_field"] = "update_form_field"
    field_name: str = Field(description="要修改的字段名（必须与现有字段完全一致）")
    updates: FormFieldUpdate


class RemoveFormField(BaseModel):
    op: Literal["remove_form_field"] = "remove_form_field"
    field_name: str


class AddFlowNode(BaseModel):
    op: Literal["add_flow_node"] = "add_flow_node"
    node: FlowNode = Field(description="要新增的完整流程环节定义（含 submit_paths）")
    after_node_id: str | None = Field(
        default=None,
        description=(
            "把新环节插到哪个现有环节之后（用于「在 A 和 B 之间插一环」——填 A 的 node_id，"
            "新环节就排在列表里 A 后面，而不是默认追加到末尾）。环节列表顺序只影响界面展示"
            "顺序，不影响实际流转（流转由各环节 submit_paths 的 target_node_id 决定）；插到正确"
            "位置只是让界面环节编号跟流程直觉一致。留空＝追加到末尾。找不到该 node_id 时也追加到末尾。"
        ),
    )


class UpdateFlowNode(BaseModel):
    op: Literal["update_flow_node"] = "update_flow_node"
    node_id: str = Field(description="要修改的环节 node_id")
    updates: FlowNodeUpdate


class RemoveFlowNode(BaseModel):
    op: Literal["remove_flow_node"] = "remove_flow_node"
    node_id: str


class AddSubmitPath(BaseModel):
    op: Literal["add_submit_path"] = "add_submit_path"
    node_id: str = Field(description="路径所属环节的 node_id")
    path: SubmitPath


class UpdateSubmitPath(BaseModel):
    op: Literal["update_submit_path"] = "update_submit_path"
    node_id: str
    path_name: str = Field(description="要修改的路径名（必须与现有路径完全一致）")
    updates: SubmitPathUpdate


class RemoveSubmitPath(BaseModel):
    op: Literal["remove_submit_path"] = "remove_submit_path"
    node_id: str
    path_name: str


class AddAttachment(BaseModel):
    op: Literal["add_attachment"] = "add_attachment"
    attachment: AttachmentConfig


class UpdateAttachment(BaseModel):
    op: Literal["update_attachment"] = "update_attachment"
    attachment_type: str
    updates: AttachmentUpdate


class RemoveAttachment(BaseModel):
    op: Literal["remove_attachment"] = "remove_attachment"
    attachment_type: str


class AddRole(BaseModel):
    op: Literal["add_role"] = "add_role"
    role: RoleConfig


class UpdateRole(BaseModel):
    op: Literal["update_role"] = "update_role"
    role_name: str
    updates: RoleUpdate


class RemoveRole(BaseModel):
    op: Literal["remove_role"] = "remove_role"
    role_name: str


class UpdateMeta(BaseModel):
    op: Literal["update_meta"] = "update_meta"
    updates: MetaUpdate


EditOperation = Annotated[
    Union[
        AddFormField,
        UpdateFormField,
        RemoveFormField,
        AddFlowNode,
        UpdateFlowNode,
        RemoveFlowNode,
        AddSubmitPath,
        UpdateSubmitPath,
        RemoveSubmitPath,
        AddAttachment,
        UpdateAttachment,
        RemoveAttachment,
        AddRole,
        UpdateRole,
        RemoveRole,
        UpdateMeta,
    ],
    Field(discriminator="op"),
]


class AppliedChange(BaseModel):
    op: str
    summary: str


class EditApplyResult(BaseModel):
    process: ProcessDefinition
    applied: list[AppliedChange] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)


def apply_edit_operations(process: ProcessDefinition, operations: list[Any]) -> EditApplyResult:
    """在 process 的深拷贝上依次应用 operations；单条操作失败只记错误，不影响其它操作。"""
    working = process.model_copy(deep=True)
    applied: list[AppliedChange] = []
    errors: list[str] = []
    for op in operations:
        try:
            summary = _apply_one(working, op)
            applied.append(AppliedChange(op=op.op, summary=summary))
        except ValueError as exc:
            errors.append(f"{op.op}: {exc}")
    return EditApplyResult(process=working, applied=applied, errors=errors)


def _apply_one(process: ProcessDefinition, op: Any) -> str:
    if isinstance(op, AddFormField):
        if any(f.field_name == op.field.field_name for f in process.form_fields):
            raise ValueError(f"字段“{op.field.field_name}”已存在，不能重复新增")
        # 插入位置：after_field_name 指定则插到该字段之后（"在 A 和 B 之间插一个字段"），
        # 否则追加到末尾。seq 不依赖 LLM 算对——插入后统一按列表顺序重新编号，
        # 不会出现"新字段和现有字段 seq 撞车"（真实踩过：LLM 想把后续字段 seq 都后移一位，
        # 但 FormFieldUpdate 当时没有 seq 字段，那两条 update 全变成 no-op，新字段自己
        # 又用了一个撞车的 seq）。
        if op.after_field_name:
            idx = next(
                (i for i, f in enumerate(process.form_fields) if f.field_name == op.after_field_name),
                None,
            )
            if idx is not None:
                process.form_fields.insert(idx + 1, op.field)
                _renumber_form_field_seq(process)
                return f"在“{op.after_field_name}”之后新增字段“{op.field.field_name}”"
        process.form_fields.append(op.field)
        _renumber_form_field_seq(process)
        return f"新增字段“{op.field.field_name}”"

    if isinstance(op, UpdateFormField):
        field = next((f for f in process.form_fields if f.field_name == op.field_name), None)
        if field is None:
            raise ValueError(f"未找到字段“{op.field_name}”")
        changed = _apply_plain_updates(field, op.updates)
        return f"更新字段“{op.field_name}”：{changed}" if changed else f"字段“{op.field_name}”无变化"

    if isinstance(op, RemoveFormField):
        before = len(process.form_fields)
        process.form_fields = [f for f in process.form_fields if f.field_name != op.field_name]
        if len(process.form_fields) == before:
            raise ValueError(f"未找到字段“{op.field_name}”")
        _renumber_form_field_seq(process)  # 删除后收拢 seq 缺口，跟插入时一样不留给 LLM 管
        return f"删除字段“{op.field_name}”"

    if isinstance(op, AddFlowNode):
        if process.get_node_by_id(op.node.node_id) is not None:
            raise ValueError(f"环节“{op.node.node_id}”已存在，不能重复新增")
        # 插入位置：after_node_id 指定则插到该环节之后（"在 A 和 B 之间插一环"），否则追加到末尾。
        # 列表顺序只影响界面展示顺序，不影响实际流转（流转看 submit_paths 的 target_node_id）。
        if op.after_node_id:
            idx = next(
                (i for i, n in enumerate(process.flow_nodes) if n.node_id == op.after_node_id),
                None,
            )
            if idx is not None:
                process.flow_nodes.insert(idx + 1, op.node)
                return f"在“{op.after_node_id}”之后新增环节“{op.node.node_name}”"
        process.flow_nodes.append(op.node)
        return f"新增环节“{op.node.node_name}”"

    if isinstance(op, UpdateFlowNode):
        node = process.get_node_by_id(op.node_id)
        if node is None:
            raise ValueError(f"未找到环节“{op.node_id}”")
        changed = _apply_plain_updates(node, op.updates)
        if op.updates.clear_time_limit_days:
            node.time_limit_days = None
            changed = f"{changed}、time_limit_days(清除)" if changed else "time_limit_days(清除)"
        return f"更新环节“{node.node_name}”：{changed}" if changed else f"环节“{node.node_name}”无变化"

    if isinstance(op, RemoveFlowNode):
        node = process.get_node_by_id(op.node_id)
        if node is None:
            raise ValueError(f"未找到环节“{op.node_id}”")
        process.flow_nodes = [n for n in process.flow_nodes if n.node_id != op.node_id]
        return f"删除环节“{node.node_name}”"

    if isinstance(op, AddSubmitPath):
        node = process.get_node_by_id(op.node_id)
        if node is None:
            raise ValueError(f"未找到环节“{op.node_id}”")
        if any(p.path_name == op.path.path_name for p in node.submit_paths):
            raise ValueError(f"环节“{node.node_name}”已存在路径“{op.path.path_name}”")
        node.submit_paths.append(op.path)
        return f"为“{node.node_name}”新增路径“{op.path.path_name}”"

    if isinstance(op, UpdateSubmitPath):
        node = process.get_node_by_id(op.node_id)
        if node is None:
            raise ValueError(f"未找到环节“{op.node_id}”")
        path = next((p for p in node.submit_paths if p.path_name == op.path_name), None)
        if path is None:
            raise ValueError(f"环节“{node.node_name}”未找到路径“{op.path_name}”")
        changed = _apply_plain_updates(path, op.updates, skip={"clear_condition", "new_path_name"})
        if op.updates.clear_condition:
            path.condition = None
            changed = f"{changed}、condition(清除)" if changed else "condition(清除)"
        # 路径改名：path_name 是定位用的主键（不能靠 _apply_plain_updates 直接写），单独处理。
        # 名字确实变了、且没跟本环节其它路径重名，才改；否则不算变更（跟其它属性一个待遇）。
        new_name = op.updates.new_path_name
        if new_name and new_name != path.path_name:
            if any(p is not path and p.path_name == new_name for p in node.submit_paths):
                raise ValueError(f"环节“{node.node_name}”已存在路径“{new_name}”，不能改成重名")
            path.path_name = new_name
            changed = f"{changed}、改名为“{new_name}”" if changed else f"改名为“{new_name}”"
        return f"更新“{node.node_name}”路径“{op.path_name}”：{changed}" if changed else "无变化"

    if isinstance(op, RemoveSubmitPath):
        node = process.get_node_by_id(op.node_id)
        if node is None:
            raise ValueError(f"未找到环节“{op.node_id}”")
        before = len(node.submit_paths)
        node.submit_paths = [p for p in node.submit_paths if p.path_name != op.path_name]
        if len(node.submit_paths) == before:
            raise ValueError(f"环节“{node.node_name}”未找到路径“{op.path_name}”")
        return f"删除“{node.node_name}”路径“{op.path_name}”"

    if isinstance(op, AddAttachment):
        attachments = list(process.attachments or [])
        if any(a.attachment_type == op.attachment.attachment_type for a in attachments):
            raise ValueError(f"附件类型“{op.attachment.attachment_type}”已存在")
        attachments.append(op.attachment)
        process.attachments = attachments
        return f"新增附件类型“{op.attachment.attachment_type}”"

    if isinstance(op, UpdateAttachment):
        attachment = next((a for a in (process.attachments or []) if a.attachment_type == op.attachment_type), None)
        if attachment is None:
            raise ValueError(f"未找到附件类型“{op.attachment_type}”")
        changed = _apply_plain_updates(attachment, op.updates, skip={"clear_required_condition"})
        if op.updates.clear_required_condition:
            attachment.required_condition = None
            changed = f"{changed}、required_condition(清除)" if changed else "required_condition(清除)"
        return f"更新附件“{op.attachment_type}”：{changed}" if changed else f"附件“{op.attachment_type}”无变化"

    if isinstance(op, RemoveAttachment):
        attachments = process.attachments or []
        before = len(attachments)
        process.attachments = [a for a in attachments if a.attachment_type != op.attachment_type] or None
        if before == len(process.attachments or []):
            raise ValueError(f"未找到附件类型“{op.attachment_type}”")
        return f"删除附件类型“{op.attachment_type}”"

    if isinstance(op, AddRole):
        roles = list(process.roles or [])
        if any(r.role_name == op.role.role_name for r in roles):
            raise ValueError(f"角色“{op.role.role_name}”已存在")
        roles.append(op.role)
        process.roles = roles
        return f"新增角色“{op.role.role_name}”"

    if isinstance(op, UpdateRole):
        role = next((r for r in (process.roles or []) if r.role_name == op.role_name), None)
        if role is None:
            raise ValueError(f"未找到角色“{op.role_name}”")
        changed = _apply_plain_updates(role, op.updates, skip={"clear_department"})
        if op.updates.clear_department:
            role.department = None
            changed = f"{changed}、department(清除)" if changed else "department(清除)"
        return f"更新角色“{op.role_name}”：{changed}" if changed else f"角色“{op.role_name}”无变化"

    if isinstance(op, RemoveRole):
        roles = process.roles or []
        before = len(roles)
        process.roles = [r for r in roles if r.role_name != op.role_name] or None
        if before == len(process.roles or []):
            raise ValueError(f"未找到角色“{op.role_name}”")
        return f"删除角色“{op.role_name}”"

    if isinstance(op, UpdateMeta):
        changed = _apply_plain_updates(process.meta, op.updates)
        return f"更新流程信息：{changed}" if changed else "流程信息无变化"

    raise ValueError(f"未知编辑操作：{getattr(op, 'op', op)!r}")


def _renumber_form_field_seq(process: ProcessDefinition) -> None:
    """seq 是纯粹的展示顺序，跟列表顺序永远保持一致——加/删字段后统一重新编号，
    不指望 LLM 自己算对该给哪个数字（真实踩过：LLM 想把插入点之后的字段 seq 都
    后移一位，但当时的 update 操作压根不支持改 seq，导致新旧字段 seq 撞车）。"""
    for index, field in enumerate(process.form_fields):
        field.seq = index + 1


def _apply_plain_updates(target: BaseModel, updates: BaseModel, *, skip: set[str] | None = None) -> str:
    """把 updates 里非 None 且与当前值不同的字段写入 target，返回真正变更的字段名列表（顿号分隔）。

    与当前值相同时不计入"变更"，避免指令是无效操作（如"改成必填"但本就必填）
    时，摘要/diff 却错误地报告发生了变化。
    """
    skip = skip or set()
    changed: list[str] = []
    for name in type(updates).model_fields:
        if name in skip or name.startswith("clear_"):
            continue
        value = getattr(updates, name)
        if value is None:
            continue
        if getattr(target, name) == value:
            continue
        setattr(target, name, value)
        changed.append(name)
    return "、".join(changed)
