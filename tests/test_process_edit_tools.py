from __future__ import annotations

from data.schema import ProcessDefinition
from app.tools.process_edit_tools import (
    AddAttachment,
    AddFlowNode,
    AddFormField,
    AddRole,
    AddSubmitPath,
    AttachmentUpdate,
    FlowNodeUpdate,
    FormFieldUpdate,
    MetaUpdate,
    RemoveAttachment,
    RemoveFlowNode,
    RemoveFormField,
    RemoveRole,
    RemoveSubmitPath,
    RoleUpdate,
    SubmitPathUpdate,
    UpdateAttachment,
    UpdateFlowNode,
    UpdateFormField,
    UpdateMeta,
    UpdateRole,
    UpdateSubmitPath,
    apply_edit_operations,
)


def _process() -> ProcessDefinition:
    return ProcessDefinition.model_validate(
        {
            "meta": {
                "process_id": "TEST-001",
                "process_name": "测试流程",
                "version": "V1.0.0",
                "responsible_dept": "测试部门",
                "description": "测试用流程",
                "applicant_scope": "全员",
                "entry_point": "OA系统",
            },
            "form_fields": [
                {
                    "seq": 1,
                    "field_name": "请假事由",
                    "required_stages": [],
                    "visible_stages": ["all"],
                    "editable_stages": ["draft"],
                    "component_type": "多行文本",
                    "logic_description": None,
                    "default_value": None,
                    "options": None,
                    "placeholder": None,
                    "max_length": None,
                }
            ],
            "flow_nodes": [
                {
                    "node_id": "draft",
                    "node_name": "起草",
                    "is_draft": True,
                    "handler": None,
                    "opinion": None,
                    "opinion_label": None,
                    "time_limit_days": None,
                    "submit_paths": [{"path_name": "送部门主管审批", "condition": None, "target_node_id": "dept_supervisor"}],
                },
                {
                    "node_id": "dept_supervisor",
                    "node_name": "部门主管审批",
                    "is_draft": False,
                    "handler": {"mode": "单选-单人处理", "source": "本部门", "role": "部门主管", "source_field": None},
                    "opinion": {"conclusive_required": True, "detail_required": False, "conclusive_options": ["同意", "不同意"]},
                    "opinion_label": "部门主管意见",
                    "time_limit_days": None,
                    "submit_paths": [
                        {"path_name": "流程结束", "condition": "结论性意见=同意", "target_node_id": "END"},
                        {"path_name": "退回起草", "condition": "结论性意见=不同意", "target_node_id": "DRAFT"},
                    ],
                },
            ],
            "attachments": None,
            "roles": None,
        }
    )


def test_add_update_remove_form_field() -> None:
    process = _process()

    result = apply_edit_operations(
        process,
        [
            AddFormField(
                field={
                    "seq": 2,
                    "field_name": "请假天数",
                    "required_stages": ["draft"],
                    "visible_stages": ["all"],
                    "editable_stages": ["draft"],
                    "component_type": "数字",
                    "logic_description": None,
                    "default_value": None,
                    "options": None,
                    "placeholder": None,
                    "max_length": None,
                }
            )
        ],
    )
    assert not result.errors
    assert {f.field_name for f in result.process.form_fields} == {"请假事由", "请假天数"}

    result2 = apply_edit_operations(
        result.process,
        [UpdateFormField(field_name="请假事由", updates=FormFieldUpdate(required_stages=["draft"], max_length=200))],
    )
    field = next(f for f in result2.process.form_fields if f.field_name == "请假事由")
    assert field.required_stages == ["draft"]
    assert field.max_length == 200
    assert "required_stages" in result2.applied[0].summary

    result3 = apply_edit_operations(result2.process, [RemoveFormField(field_name="请假天数")])
    assert [f.field_name for f in result3.process.form_fields] == ["请假事由"]

    result4 = apply_edit_operations(result3.process, [RemoveFormField(field_name="不存在的字段")])
    assert result4.errors and "未找到字段" in result4.errors[0]


def _field(seq, name, component="单行文本"):
    return {
        "seq": seq, "field_name": name, "required_stages": [], "visible_stages": ["all"],
        "editable_stages": ["draft"], "component_type": component, "logic_description": None,
        "default_value": None, "options": None, "placeholder": None, "max_length": None,
    }


def test_add_form_field_after_field_name_inserts_at_position_and_renumbers_seq() -> None:
    """真实复现的 bug：插入字段带上错误/撞车的 seq（如跟已有字段同一个 seq），导致
    展示顺序和 seq 对不上。插入位置和编号都不该指望 LLM 自己算对——插入后统一按
    列表顺序重新编号。"""
    process = _process()
    result = apply_edit_operations(process, [AddFormField(field=_field(2, "请假天数"))])
    result2 = apply_edit_operations(result.process, [AddFormField(field=_field(2, "工作交接人"))])
    result3 = apply_edit_operations(
        result2.process,
        [AddFormField(field=_field(99, "采购类型"), after_field_name="请假事由")],  # seq 故意给个撞车/错误值
    )
    assert not result3.errors
    names_in_order = [f.field_name for f in result3.process.form_fields]
    assert names_in_order == ["请假事由", "采购类型", "请假天数", "工作交接人"]
    # seq 完全由列表顺序重新编号决定，不用管 LLM 传的是什么
    seqs = [f.seq for f in result3.process.form_fields]
    assert seqs == [1, 2, 3, 4]


def test_add_form_field_without_after_field_name_appends_and_renumbers() -> None:
    process = _process()
    result = apply_edit_operations(process, [AddFormField(field=_field(1, "请假天数"))])  # seq 故意跟已有字段撞车
    assert not result.errors
    assert [f.field_name for f in result.process.form_fields] == ["请假事由", "请假天数"]
    assert [f.seq for f in result.process.form_fields] == [1, 2]


def test_add_form_field_after_unknown_field_falls_back_to_append() -> None:
    process = _process()
    result = apply_edit_operations(process, [AddFormField(field=_field(1, "请假天数"), after_field_name="不存在的字段")])
    assert not result.errors
    assert [f.field_name for f in result.process.form_fields] == ["请假事由", "请假天数"]


def test_remove_form_field_closes_seq_gap() -> None:
    process = _process()
    result = apply_edit_operations(process, [AddFormField(field=_field(2, "请假天数"))])
    result2 = apply_edit_operations(result.process, [AddFormField(field=_field(3, "工作交接人"))])
    result3 = apply_edit_operations(result2.process, [RemoveFormField(field_name="请假天数")])
    assert not result3.errors
    names = [f.field_name for f in result3.process.form_fields]
    seqs = [f.seq for f in result3.process.form_fields]
    assert names == ["请假事由", "工作交接人"]
    assert seqs == [1, 2]  # 删除后收拢缺口，不留 [1, 3] 这种空洞


def test_update_with_same_value_is_not_reported_as_changed() -> None:
    process = _process()
    # 请假事由的 required_stages 已经是 []；再次设为 [] 应视为无变化
    result = apply_edit_operations(
        process,
        [UpdateFormField(field_name="请假事由", updates=FormFieldUpdate(required_stages=[]))],
    )
    assert not result.errors
    assert result.applied[0].summary == "字段“请假事由”无变化"


def test_add_update_remove_flow_node_and_submit_path() -> None:
    process = _process()

    add_result = apply_edit_operations(
        process,
        [
            AddFlowNode(
                node={
                    "node_id": "dept_gm",
                    "node_name": "部门总经理审批",
                    "is_draft": False,
                    "handler": {"mode": "单选-单人处理", "source": "本部门", "role": "部门总经理", "source_field": None},
                    "opinion": {"conclusive_required": True, "detail_required": False, "conclusive_options": ["同意", "不同意"]},
                    "opinion_label": "部门总经理意见",
                    "time_limit_days": None,
                    "submit_paths": [{"path_name": "流程结束", "condition": "结论性意见=同意", "target_node_id": "END"}],
                }
            )
        ],
    )
    assert not add_result.errors
    assert add_result.process.get_node_by_id("dept_gm") is not None

    # 让"部门主管审批"的通过路径改指向新节点
    update_result = apply_edit_operations(
        add_result.process,
        [
            UpdateSubmitPath(
                node_id="dept_supervisor",
                path_name="流程结束",
                updates=SubmitPathUpdate(target_node_id="dept_gm"),
            ),
            AddSubmitPath(
                node_id="dept_supervisor",
                path={"path_name": "退回起草(超额)", "condition": "请假天数>30", "target_node_id": "DRAFT"},
            ),
        ],
    )
    assert not update_result.errors
    supervisor = update_result.process.get_node_by_id("dept_supervisor")
    assert {p.path_name for p in supervisor.submit_paths} == {"流程结束", "退回起草", "退回起草(超额)"}
    assert next(p for p in supervisor.submit_paths if p.path_name == "流程结束").target_node_id == "dept_gm"

    time_limit_result = apply_edit_operations(
        update_result.process,
        [UpdateFlowNode(node_id="dept_gm", updates=FlowNodeUpdate(time_limit_days=2))],
    )
    assert time_limit_result.process.get_node_by_id("dept_gm").time_limit_days == 2

    clear_result = apply_edit_operations(
        time_limit_result.process,
        [UpdateFlowNode(node_id="dept_gm", updates=FlowNodeUpdate(clear_time_limit_days=True))],
    )
    assert clear_result.process.get_node_by_id("dept_gm").time_limit_days is None

    remove_path_result = apply_edit_operations(
        clear_result.process,
        [RemoveSubmitPath(node_id="dept_supervisor", path_name="退回起草(超额)")],
    )
    assert {p.path_name for p in remove_path_result.process.get_node_by_id("dept_supervisor").submit_paths} == {
        "流程结束",
        "退回起草",
    }

    remove_node_result = apply_edit_operations(remove_path_result.process, [RemoveFlowNode(node_id="dept_gm")])
    assert remove_node_result.process.get_node_by_id("dept_gm") is None


def test_add_flow_node_after_node_id_inserts_at_position_not_append() -> None:
    """在两个环节之间插入新环节：after_node_id 指定后，新环节排到该环节之后（不是默认追加到
    末尾）——修复"用户要求插在 A 和 B 之间、结果界面显示在最后"的问题。列表顺序只影响展示。"""
    process = _process()  # 顺序：draft, dept_supervisor
    result = apply_edit_operations(
        process,
        [
            AddFlowNode(
                after_node_id="dept_supervisor",
                node={
                    "node_id": "dept_vp",
                    "node_name": "部门副总经理审批",
                    "is_draft": False,
                    "handler": {"mode": "单选-单人处理", "source": "本部门", "role": "部门副总经理", "source_field": None},
                    "opinion": {"conclusive_required": True, "detail_required": False, "conclusive_options": ["同意", "不同意"]},
                    "opinion_label": "副总经理意见",
                    "time_limit_days": None,
                    "submit_paths": [{"path_name": "流程结束", "condition": "结论性意见=同意", "target_node_id": "END"}],
                },
            )
        ],
    )
    assert not result.errors
    # dept_vp 排在 dept_supervisor 之后，而不是追加到列表末尾
    assert [n.node_id for n in result.process.flow_nodes] == ["draft", "dept_supervisor", "dept_vp"]
    assert "在“dept_supervisor”之后新增环节" in result.applied[0].summary


def test_add_flow_node_without_after_node_id_appends() -> None:
    """不指定 after_node_id 时保持原行为——追加到列表末尾。"""
    process = _process()
    result = apply_edit_operations(
        process,
        [
            AddFlowNode(
                node={
                    "node_id": "tail", "node_name": "末环节", "is_draft": False,
                    "handler": {"mode": "单选-单人处理", "source": "本部门", "role": "某角色", "source_field": None},
                    "opinion": {"conclusive_required": True, "detail_required": False, "conclusive_options": ["同意", "不同意"]},
                    "opinion_label": "意见", "time_limit_days": None,
                    "submit_paths": [{"path_name": "流程结束", "condition": None, "target_node_id": "END"}],
                }
            )
        ],
    )
    assert [n.node_id for n in result.process.flow_nodes] == ["draft", "dept_supervisor", "tail"]


def test_add_flow_node_after_unknown_node_falls_back_to_append() -> None:
    """after_node_id 指向不存在的环节时兜底追加到末尾，不报错。"""
    process = _process()
    result = apply_edit_operations(
        process,
        [
            AddFlowNode(
                after_node_id="does_not_exist",
                node={
                    "node_id": "x", "node_name": "X", "is_draft": False,
                    "handler": {"mode": "单选-单人处理", "source": "本部门", "role": "R", "source_field": None},
                    "opinion": {"conclusive_required": True, "detail_required": False, "conclusive_options": ["同意", "不同意"]},
                    "opinion_label": "意见", "time_limit_days": None,
                    "submit_paths": [{"path_name": "流程结束", "condition": None, "target_node_id": "END"}],
                },
            )
        ],
    )
    assert not result.errors
    assert [n.node_id for n in result.process.flow_nodes] == ["draft", "dept_supervisor", "x"]


def test_update_submit_path_rename_via_new_path_name() -> None:
    """路径改名：一步到位（path_name 定位旧名、new_path_name 给新名），而不是无法改名的
    死循环。修复"名字说送A、实际送B"却改不动的 bug。"""
    process = _process()
    result = apply_edit_operations(
        process,
        [
            UpdateSubmitPath(
                node_id="dept_supervisor",
                path_name="流程结束",
                updates=SubmitPathUpdate(new_path_name="审批通过·结束"),
            )
        ],
    )
    assert not result.errors
    names = {p.path_name for p in result.process.get_node_by_id("dept_supervisor").submit_paths}
    assert names == {"审批通过·结束", "退回起草"}  # 旧名没了、新名在
    # 改名连带 target/condition 不变
    renamed = next(p for p in result.process.get_node_by_id("dept_supervisor").submit_paths if p.path_name == "审批通过·结束")
    assert renamed.target_node_id == "END" and renamed.condition == "结论性意见=同意"
    assert "改名为" in result.applied[0].summary


def test_update_submit_path_rename_and_retarget_together() -> None:
    """改名 + 改指向可以同一次操作完成（插入新环节后修主管那条路径的典型场景）。"""
    process = _process()
    result = apply_edit_operations(
        process,
        [
            UpdateSubmitPath(
                node_id="dept_supervisor",
                path_name="流程结束",
                updates=SubmitPathUpdate(new_path_name="送副总经理审批", target_node_id="dept_vp"),
            )
        ],
    )
    assert not result.errors
    p = next(pp for pp in result.process.get_node_by_id("dept_supervisor").submit_paths if pp.path_name == "送副总经理审批")
    assert p.target_node_id == "dept_vp"


def test_update_submit_path_rename_to_existing_name_errors() -> None:
    """改成本环节已有的路径名要报错，不能制造重名。"""
    process = _process()
    result = apply_edit_operations(
        process,
        [
            UpdateSubmitPath(
                node_id="dept_supervisor",
                path_name="流程结束",
                updates=SubmitPathUpdate(new_path_name="退回起草"),  # 已存在
            )
        ],
    )
    assert result.errors
    # 原路径名保持不变（操作失败不落库）
    names = {p.path_name for p in result.process.get_node_by_id("dept_supervisor").submit_paths}
    assert names == {"流程结束", "退回起草"}


def test_submit_path_clear_condition() -> None:
    process = _process()
    result = apply_edit_operations(
        process,
        [UpdateSubmitPath(node_id="dept_supervisor", path_name="流程结束", updates=SubmitPathUpdate(clear_condition=True))],
    )
    path = next(p for p in result.process.get_node_by_id("dept_supervisor").submit_paths if p.path_name == "流程结束")
    assert path.condition is None


def test_attachment_add_update_remove() -> None:
    process = _process()
    add_result = apply_edit_operations(
        process,
        [AddAttachment(attachment={"attachment_type": "病假证明", "upload_stages": ["draft"], "required_stages": []})],
    )
    assert add_result.process.attachments and add_result.process.attachments[0].attachment_type == "病假证明"

    update_result = apply_edit_operations(
        add_result.process,
        [UpdateAttachment(attachment_type="病假证明", updates=AttachmentUpdate(required_stages=["draft"]))],
    )
    assert update_result.process.attachments[0].required_stages == ["draft"]

    remove_result = apply_edit_operations(update_result.process, [RemoveAttachment(attachment_type="病假证明")])
    assert remove_result.process.attachments is None

    error_result = apply_edit_operations(remove_result.process, [RemoveAttachment(attachment_type="病假证明")])
    assert error_result.errors


def test_attachment_required_condition_set_and_clear() -> None:
    process = _process()
    add_result = apply_edit_operations(
        process,
        [AddAttachment(attachment={"attachment_type": "就诊证明", "upload_stages": ["draft"], "required_stages": ["draft"]})],
    )
    assert add_result.process.attachments[0].required_condition is None

    condition_result = apply_edit_operations(
        add_result.process,
        [
            UpdateAttachment(
                attachment_type="就诊证明",
                updates=AttachmentUpdate(required_condition="请假类型=病假 且 请假天数>=3"),
            )
        ],
    )
    assert condition_result.process.attachments[0].required_condition == "请假类型=病假 且 请假天数>=3"

    clear_result = apply_edit_operations(
        condition_result.process,
        [UpdateAttachment(attachment_type="就诊证明", updates=AttachmentUpdate(clear_required_condition=True))],
    )
    assert clear_result.process.attachments[0].required_condition is None


def test_role_add_update_remove() -> None:
    process = _process()
    add_result = apply_edit_operations(
        process,
        [AddRole(role={"role_name": "会签专员", "role_type": "流程角色", "members": ["张三"], "department": None})],
    )
    assert add_result.process.roles and add_result.process.roles[0].role_name == "会签专员"

    update_result = apply_edit_operations(
        add_result.process,
        [UpdateRole(role_name="会签专员", updates=RoleUpdate(members=["张三", "李四"], department="财务部"))],
    )
    role = add_result.process.roles[0]
    updated_role = update_result.process.roles[0]
    assert updated_role.members == ["张三", "李四"]
    assert updated_role.department == "财务部"

    clear_result = apply_edit_operations(
        update_result.process, [UpdateRole(role_name="会签专员", updates=RoleUpdate(clear_department=True))]
    )
    assert clear_result.process.roles[0].department is None

    remove_result = apply_edit_operations(clear_result.process, [RemoveRole(role_name="会签专员")])
    assert remove_result.process.roles is None


def test_update_meta() -> None:
    process = _process()
    result = apply_edit_operations(process, [UpdateMeta(updates=MetaUpdate(description="更新后的描述"))])
    assert result.process.meta.description == "更新后的描述"
    assert result.process.meta.process_name == "测试流程"  # 未提及的属性不变


def test_duplicate_add_reports_error_without_mutating() -> None:
    process = _process()
    result = apply_edit_operations(
        process,
        [
            AddFlowNode(
                node={
                    "node_id": "dept_supervisor",
                    "node_name": "重复环节",
                    "is_draft": False,
                    "handler": None,
                    "opinion": None,
                    "opinion_label": None,
                    "time_limit_days": None,
                    "submit_paths": [],
                }
            )
        ],
    )
    assert result.errors and "已存在" in result.errors[0]
    assert len(result.process.flow_nodes) == 2  # 未被重复添加


def test_apply_does_not_mutate_input_process() -> None:
    process = _process()
    apply_edit_operations(process, [RemoveFormField(field_name="请假事由")])
    assert len(process.form_fields) == 1  # 原始 process 未被修改（深拷贝隔离）
