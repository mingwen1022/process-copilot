from __future__ import annotations

import json
from pathlib import Path

from app.integrations.feishu import translate_to_feishu_approval
from data.schema import ProcessDefinition

LEAVE_TARGET = Path("data/cases/leave_request/standard/target.json")


def _linear_process() -> ProcessDefinition:
    """一个最小线性流程：起草 → 部门主管(单人) → 部门经理(会签) → END。"""
    return ProcessDefinition.model_validate(
        {
            "meta": {
                "process_id": "T-001",
                "process_name": "测试请假流程",
                "version": "V1.0.0",
                "responsible_dept": "人力资源部",
                "description": "翻译单测用",
                "applicant_scope": "全员",
                "entry_point": "OA系统",
            },
            "form_fields": [
                {"seq": 1, "field_name": "请假类型", "required_stages": ["draft"], "visible_stages": ["all"],
                 "editable_stages": ["draft"], "component_type": "下拉单选", "options": ["事假", "病假"]},
                {"seq": 2, "field_name": "请假天数", "required_stages": ["all"], "visible_stages": ["all"],
                 "editable_stages": ["draft"], "component_type": "数字"},
                {"seq": 3, "field_name": "请假事由", "required_stages": [], "visible_stages": ["all"],
                 "editable_stages": ["draft"], "component_type": "多行文本"},
                {"seq": 4, "field_name": "申请人", "required_stages": [], "visible_stages": ["all"],
                 "editable_stages": [], "component_type": "只读文本"},
            ],
            "flow_nodes": [
                {"node_id": "draft", "node_name": "起草", "is_draft": True, "handler": None,
                 "submit_paths": [{"path_name": "送部门主管", "condition": None, "target_node_id": "mgr"}]},
                {"node_id": "mgr", "node_name": "部门主管审批", "is_draft": False,
                 "handler": {"mode": "单选-单人处理", "source": "本部门", "role": "部门主管", "source_field": None},
                 "submit_paths": [{"path_name": "送部门经理", "condition": None, "target_node_id": "gm"}]},
                {"node_id": "gm", "node_name": "部门经理审批", "is_draft": False,
                 "handler": {"mode": "多选-并行处理", "source": "本部门", "role": "部门经理", "source_field": None},
                 "submit_paths": [{"path_name": "流程结束", "condition": "结论性意见=同意", "target_node_id": "END"}]},
            ],
            "attachments": None,
            "roles": None,
        }
    )


def test_node_list_is_linear_start_to_end() -> None:
    result = translate_to_feishu_approval(_linear_process())
    node_list = result.definition["node_list"]
    assert node_list[0] == {"id": "START"}
    assert node_list[-1] == {"id": "END"}
    middle = node_list[1:-1]
    assert [n["id"] for n in middle] == ["mgr", "gm"]  # 按流转顺序、跳过起草


def test_node_type_maps_from_handler_mode() -> None:
    result = translate_to_feishu_approval(_linear_process())
    by_id = {n["id"]: n for n in result.definition["node_list"] if n["id"] not in ("START", "END")}
    assert by_id["mgr"]["node_type"] == "OR"   # 单人处理 → 或签
    assert by_id["gm"]["node_type"] == "AND"   # 并行处理 → 会签


def test_default_approver_is_supervisor_level_chain() -> None:
    result = translate_to_feishu_approval(_linear_process())
    by_id = {n["id"]: n for n in result.definition["node_list"] if n["id"] not in ("START", "END")}
    assert by_id["mgr"]["approver"] == [{"type": "Supervisor", "level": "1"}]
    assert by_id["gm"]["approver"] == [{"type": "Supervisor", "level": "2"}]


def test_approver_override_injects_real_person() -> None:
    result = translate_to_feishu_approval(
        _linear_process(),
        approver_overrides={"mgr": [{"type": "Personal", "user_id": "ou_abc"}]},
    )
    by_id = {n["id"]: n for n in result.definition["node_list"] if n["id"] not in ("START", "END")}
    assert by_id["mgr"]["approver"] == [{"type": "Personal", "user_id": "ou_abc"}]


def test_form_content_is_valid_json_with_expected_controls() -> None:
    result = translate_to_feishu_approval(_linear_process())
    controls = json.loads(result.definition["form"]["form_content"])
    by_id = {c["id"]: c for c in controls}
    assert by_id["field_1"]["type"] == "input"        # 下拉单选降级（radio 未接入）
    assert by_id["field_2"]["type"] == "number"
    assert by_id["field_3"]["type"] == "textarea"
    # 请假天数 required_stages=['all'] → 提交必填；请假事由 [] → 非必填
    assert by_id["field_2"]["required"] is True
    assert by_id["field_3"]["required"] is False


def test_control_extra_fields_from_live_probe() -> None:
    # 探针实测：date 需 value:""，员工选择(contact) 需 value:{}；其余裸结构
    proc = ProcessDefinition.model_validate(
        {
            "meta": {"process_id": "T", "process_name": "T", "version": "V1.0.0", "responsible_dept": "x",
                     "description": "x", "applicant_scope": "x", "entry_point": "OA系统"},
            "form_fields": [
                {"seq": 1, "field_name": "日期", "required_stages": ["draft"], "visible_stages": ["all"],
                 "editable_stages": ["draft"], "component_type": "日期组件"},
                {"seq": 2, "field_name": "交接人", "required_stages": ["draft"], "visible_stages": ["all"],
                 "editable_stages": ["draft"], "component_type": "员工选择"},
                {"seq": 3, "field_name": "部门", "required_stages": ["draft"], "visible_stages": ["all"],
                 "editable_stages": ["draft"], "component_type": "部门选择"},
                {"seq": 4, "field_name": "附件", "required_stages": [], "visible_stages": ["all"],
                 "editable_stages": ["draft"], "component_type": "附件"},
            ],
            "flow_nodes": [
                {"node_id": "draft", "node_name": "起草", "is_draft": True, "handler": None,
                 "submit_paths": [{"path_name": "送审", "condition": None, "target_node_id": "END"}]},
            ],
            "attachments": None, "roles": None,
        }
    )
    by_id = {c["id"]: c for c in json.loads(translate_to_feishu_approval(proc).definition["form"]["form_content"])}
    assert by_id["field_1"]["type"] == "date" and by_id["field_1"]["value"] == "YYYY-MM-DD"
    assert by_id["field_2"]["type"] == "contact" and by_id["field_2"]["value"] == {}
    assert by_id["field_3"]["type"] == "department" and "value" not in by_id["field_3"]  # 裸结构
    assert by_id["field_4"]["type"] == "attachmentV2" and "value" not in by_id["field_4"]


def test_readonly_field_degrades_to_input_with_note() -> None:
    result = translate_to_feishu_approval(_linear_process())
    controls = json.loads(result.definition["form"]["form_content"])
    by_id = {c["id"]: c for c in controls}
    assert by_id["field_4"]["type"] == "input"  # 只读文本降级
    assert any("只读文本" in n and "申请人" in n for n in result.notes)


def test_select_degrades_to_input_with_note() -> None:
    result = translate_to_feishu_approval(_linear_process())
    controls = json.loads(result.definition["form"]["form_content"])
    by_id = {c["id"]: c for c in controls}
    assert by_id["field_1"]["type"] == "input"   # 下拉单选降级
    assert "option" not in by_id["field_1"]
    assert any("单选/多选" in n for n in result.notes)


def test_conditional_branch_is_linearized_with_note() -> None:
    # gm 节点有 condition 的路径 → 记录线性化 note
    result = translate_to_feishu_approval(_linear_process())
    assert result.linearized is True
    assert any("线性化" in n for n in result.notes)


def test_i18n_resources_cover_all_referenced_keys() -> None:
    result = translate_to_feishu_approval(_linear_process())
    texts = result.definition["i18n_resources"][0]["texts"]
    keys = {t["key"] for t in texts}
    # 定义体里所有 @i18n@ 引用都应在 i18n_resources 里有文案
    blob = json.dumps(result.definition, ensure_ascii=False)
    import re

    referenced = set(re.findall(r"@i18n@[A-Za-z0-9_]+", blob))
    # i18n_resources 自己的 key 字段也会被上面的正则扫到，但它们本就是被定义的键，一致即可
    assert referenced <= keys, f"缺少文案的键：{referenced - keys}"


def test_real_leave_case_translates_without_crash() -> None:
    if not LEAVE_TARGET.exists():
        return  # 锚案例不在时跳过，不让缺文件阻断
    raw = json.loads(LEAVE_TARGET.read_text(encoding="utf-8"))
    process = ProcessDefinition.model_validate(raw["deterministic_process_definition"])
    result = translate_to_feishu_approval(process)
    assert result.definition["node_list"][0] == {"id": "START"}
    assert result.definition["node_list"][-1] == {"id": "END"}
    json.loads(result.definition["form"]["form_content"])  # 必须是合法 JSON
    assert result.definition["approval_name"].startswith("@i18n@")
