from __future__ import annotations

from data.schema import ProcessDefinition
from app.reporting.process_diff import diff_process_definitions, render_diff_markdown


def _process(**overrides) -> ProcessDefinition:
    base = {
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
                "submit_paths": [{"path_name": "送审批", "condition": None, "target_node_id": "approval"}],
            },
            {
                "node_id": "approval",
                "node_name": "审批",
                "is_draft": False,
                "handler": {"mode": "单选-单人处理", "source": "本部门", "role": "部门主管", "source_field": None},
                "opinion": {"conclusive_required": True, "detail_required": False, "conclusive_options": ["同意", "不同意"]},
                "opinion_label": "部门主管意见",
                "time_limit_days": None,
                "submit_paths": [{"path_name": "流程结束", "condition": "结论性意见=同意", "target_node_id": "END"}],
            },
        ],
        "attachments": None,
        "roles": None,
    }
    base.update(overrides)
    return ProcessDefinition.model_validate(base)


def test_no_changes_reports_no_diff() -> None:
    before = _process()
    after = _process()
    diff = diff_process_definitions(before, after)
    assert diff["has_changes"] is False
    assert render_diff_markdown(diff) == "本次未产生流程定义变更。"


def test_form_field_added_and_changed() -> None:
    before = _process()
    after_data = before.model_dump(mode="json")
    after_data["form_fields"][0]["required_stages"] = ["draft"]
    after_data["form_fields"].append(
        {
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
    after = ProcessDefinition.model_validate(after_data)

    diff = diff_process_definitions(before, after)
    assert diff["has_changes"] is True
    assert diff["form_fields"]["added"] == ["请假天数"]
    assert diff["form_fields"]["changed"][0]["key"] == "请假事由"

    markdown = render_diff_markdown(diff)
    assert "新增字段“请假天数”" in markdown
    assert "更新字段“请假事由”" in markdown


def test_flow_node_removed_and_submit_path_changed() -> None:
    before = _process()
    after_data = before.model_dump(mode="json")
    after_data["flow_nodes"][1]["submit_paths"][0]["condition"] = "结论性意见=同意 且 金额<=50000"
    after = ProcessDefinition.model_validate(after_data)

    diff = diff_process_definitions(before, after)
    node_change = next(item for item in diff["flow_nodes"]["changed"] if item["key"] == "approval")
    path_change = node_change["submit_paths"]["changed"][0]
    assert path_change["key"] == "流程结束"
    assert path_change["changes"][0]["attribute"] == "condition"

    markdown = render_diff_markdown(diff)
    assert "环节“approval”路径“流程结束” condition" in markdown


def test_attachment_and_meta_diff() -> None:
    before = _process()
    after_data = before.model_dump(mode="json")
    after_data["meta"]["description"] = "新的描述"
    after_data["attachments"] = [{"attachment_type": "证明材料", "upload_stages": ["draft"], "required_stages": []}]
    after = ProcessDefinition.model_validate(after_data)

    diff = diff_process_definitions(before, after)
    assert diff["meta"][0]["attribute"] == "description"
    assert diff["attachments"]["added"] == ["证明材料"]


# ——— 纯换位置检测：之前会被漏掉的场景——按 key 比对属性，从不看列表顺序本身，
# 导致"只是挪了位置、属性没变"这类改动被误判成"没有变化"（真实复现过：确定性
# 编辑正确把字段插到了新位置，diff 却报 has_changes=False，回复文案错误地追加了
# "本轮实际未发生变化"的纠正提示）。———

def test_pure_reorder_with_no_attribute_change_is_detected() -> None:
    """真实场景：字段属性完全没变，只是列表位置换了——必须能检测到，不能悄悄漏掉。"""
    before_data = _process().model_dump(mode="json")
    before_data["form_fields"].append({
        "seq": 2, "field_name": "请假天数", "required_stages": [], "visible_stages": ["all"],
        "editable_stages": ["draft"], "component_type": "数字", "logic_description": None,
        "default_value": None, "options": None, "placeholder": None, "max_length": None,
    })
    before = ProcessDefinition.model_validate(before_data)

    after_data = before.model_dump(mode="json")
    after_data["form_fields"] = [after_data["form_fields"][1], after_data["form_fields"][0]]  # 顺序颠倒，属性不变
    after_data["form_fields"][0]["seq"] = 1
    after_data["form_fields"][1]["seq"] = 2
    after = ProcessDefinition.model_validate(after_data)

    diff = diff_process_definitions(before, after)
    assert diff["has_changes"] is True  # 之前这里会是 False——纯换位置被漏掉
    order_changes = {
        item["key"]: [c for c in item["changes"] if c["attribute"] == "_order"]
        for item in diff["form_fields"]["changed"]
    }
    assert order_changes.get("请假事由")  # 从"排最前"变成"排在请假天数后面"
    assert order_changes.get("请假天数")  # 从"排在请假事由后面"变成"排最前"


def test_reorder_does_not_flag_items_whose_relative_order_is_unchanged() -> None:
    """插入一个新环节导致后面环节的下标被动往后挪，但它们之间的相对顺序（邻居关系）
    没变——不该被误判成"被移动过"，只有真正插入点前后邻居变了的那个才算。用
    flow_nodes（没有 seq 这类显式排序字段，纯靠列表顺序）才能干净地隔离出"邻居对比法
    本身"的行为，不会被 form_fields.seq 的真实属性变化混进来。"""
    before_data = _process().model_dump(mode="json")
    approval_node = before_data["flow_nodes"][1]
    for name in ["node_c", "node_d"]:
        before_data["flow_nodes"].append({**approval_node, "node_id": name, "node_name": name})
    before = ProcessDefinition.model_validate(before_data)  # 顺序：draft, approval, node_c, node_d

    after_data = before.model_dump(mode="json")
    nodes = after_data["flow_nodes"]
    new_node = {**approval_node, "node_id": "node_x", "node_name": "node_x"}
    after_data["flow_nodes"] = [nodes[0], new_node, nodes[1], nodes[2], nodes[3]]  # node_x 插在 draft 和 approval 之间
    after = ProcessDefinition.model_validate(after_data)

    diff = diff_process_definitions(before, after)
    changed_keys = {item["key"] for item in diff["flow_nodes"]["changed"]}
    # approval 的邻居关系真的变了（原来前面是 draft，现在前面是 node_x）——算移动
    assert "approval" in changed_keys
    # node_c、node_d 虽然下标都往后挪了一位，但彼此之间、跟 approval 的相对邻居关系
    # 完全没变——不该被标记
    assert "node_c" not in changed_keys
    assert "node_d" not in changed_keys
    assert "node_x" not in changed_keys  # node_x 是新增的，走 added，不走 changed


def test_flow_node_pure_reorder_is_detected_without_new_schema_field() -> None:
    """FlowNode 没有显式排序字段，全靠列表顺序——邻居对比法在这种没有 seq 字段的类型上
    也要能工作，不用给 schema 加新字段。"""
    before = _process()
    after_data = before.model_dump(mode="json")
    after_data["flow_nodes"] = [after_data["flow_nodes"][1], after_data["flow_nodes"][0]]  # 顺序颠倒
    after = ProcessDefinition.model_validate(after_data)

    diff = diff_process_definitions(before, after)
    assert diff["has_changes"] is True
    changed_keys = {item["key"] for item in diff["flow_nodes"]["changed"]}
    assert "draft" in changed_keys and "approval" in changed_keys


def test_form_field_seq_change_alone_is_detected() -> None:
    """seq 字段本身变了（哪怕列表顺序这次因为别的原因没跟着变），也要能被 diff 捕捉——
    这条之前被漏掉是因为 _FORM_FIELD_ATTRS 压根没把 seq 列进比对清单。"""
    before = _process()
    after_data = before.model_dump(mode="json")
    after_data["form_fields"][0]["seq"] = 5
    after = ProcessDefinition.model_validate(after_data)

    diff = diff_process_definitions(before, after)
    assert diff["has_changes"] is True
    change = next(c for c in diff["form_fields"]["changed"][0]["changes"] if c["attribute"] == "seq")
    assert change == {"attribute": "seq", "before": 1, "after": 5}


def test_attachment_required_condition_diff() -> None:
    before_data = _process().model_dump(mode="json")
    before_data["attachments"] = [{"attachment_type": "就诊证明", "upload_stages": ["draft"], "required_stages": ["draft"], "required_condition": None}]
    before = ProcessDefinition.model_validate(before_data)

    after_data = before.model_dump(mode="json")
    after_data["attachments"][0]["required_condition"] = "请假类型=病假 且 请假天数>=3"
    after = ProcessDefinition.model_validate(after_data)

    diff = diff_process_definitions(before, after)
    change = diff["attachments"]["changed"][0]
    assert change["key"] == "就诊证明"
    assert change["changes"][0] == {
        "attribute": "required_condition",
        "before": None,
        "after": "请假类型=病假 且 请假天数>=3",
    }
