"""Mock 流程实例（实例接口的 Mock adapter）。

"我的流程"页面 + 运维副驾测试共用这套 fixtures：大部分正常、少数卡住，卡住的
覆盖设计文档 3.5 的三类根因。未来接真实引擎/飞书时，换一个同签名的 adapter 即可，
上层（页面 + 副驾）不动。

注意：这里造的是"实例快照"，不接冻结的 slice1 运行时，也不写任何 DB。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from app.copilots.diagnostics import build_instance_context
from app.org_knowledge import load_org_seed
from data.schema import InstanceContext, ProcessDefinition

APPLICANT = "u_it_app_staff"  # org_seed 里有部门/主管归属
NO_DEPT_APPLICANT = "u_ghost_no_dept"  # 无岗位归属 → 审批人解析必为空（模拟组织缺口）

InstanceStatus = Literal["flowing", "stuck", "completed"]


@dataclass(frozen=True)
class MockInstance:
    instance_id: str
    title: str
    process_name: str
    status: InstanceStatus
    stuck_kind: str | None  # "design_gap" / "org_gap" / "data_error" / None
    context: InstanceContext | None  # completed 的没有可诊断上下文
    can_submit_decision: bool = False  # 当前浏览者是不是这个环节的处理人——只有"待我审批"的
    # 单据才该是 True；这里固定 fixtures 都是"发起人查看自己单子进度"的排障场景，不是本人
    # 待办的审批环节，默认 False。待办队列里桥接来的审批项会显式传 True（见 ops_service）。


def _leave_gap_process() -> ProcessDefinition:
    """演示用请假流程：字段/环节均满足合规（供 #2 体检看到"设计合规"），但在部门总经理
    环节留了一个 4-7 天的路径覆盖漏洞（供 #3 演示设计缺口卡住）——合规体检查不出路径覆盖
    完整性（没有这类 CheckKind），所以设计合规 ✓ 与存在覆盖漏洞 可以并存。"""
    return ProcessDefinition.model_validate(
        {
            "meta": {"process_id": "LEAVE-001", "process_name": "员工请假申请流程", "version": "V1.0.0",
                     "responsible_dept": "人力资源部", "description": "员工请假标准审批流程",
                     "applicant_scope": "全员", "entry_point": "OA系统"},
            "form_fields": [
                {"seq": 1, "field_name": "请假类型", "required_stages": ["draft"], "visible_stages": ["all"],
                 "editable_stages": ["draft"], "component_type": "下拉单选", "options": ["事假", "病假", "年假"]},
                {"seq": 2, "field_name": "开始日期", "required_stages": ["draft"], "visible_stages": ["all"],
                 "editable_stages": ["draft"], "component_type": "日期组件"},
                {"seq": 3, "field_name": "结束日期", "required_stages": ["draft"], "visible_stages": ["all"],
                 "editable_stages": ["draft"], "component_type": "日期组件"},
                {"seq": 4, "field_name": "请假天数", "required_stages": ["all"], "visible_stages": ["all"],
                 "editable_stages": ["draft"], "component_type": "数字"},
                {"seq": 5, "field_name": "请假事由", "required_stages": ["draft"], "visible_stages": ["all"],
                 "editable_stages": ["draft"], "component_type": "多行文本"},
                {"seq": 6, "field_name": "工作交接人", "required_stages": ["draft"], "visible_stages": ["all"],
                 "editable_stages": ["draft"], "component_type": "员工选择"},
            ],
            "flow_nodes": [
                {"node_id": "draft", "node_name": "起草", "is_draft": True, "handler": None,
                 "submit_paths": [{"path_name": "送部门主管", "condition": None, "target_node_id": "mgr"}]},
                {"node_id": "mgr", "node_name": "部门主管审批", "is_draft": False,
                 "handler": {"mode": "单选-单人处理", "source": "本部门", "role": "部门主管", "source_field": None},
                 "submit_paths": [
                     {"path_name": "送部门总经理", "condition": "结论性意见=同意", "target_node_id": "gm"},
                     {"path_name": "退回起草", "condition": "结论性意见=不同意", "target_node_id": "DRAFT"},
                 ]},
                {"node_id": "gm", "node_name": "部门总经理审批", "is_draft": False,
                 "handler": {"mode": "单选-单人处理", "source": "本部门", "role": "部门总经理", "source_field": None},
                 "submit_paths": [
                     {"path_name": "直接结束", "condition": "结论性意见=同意 且 请假天数≤3天", "target_node_id": "END"},
                     {"path_name": "送条线分管领导", "condition": "结论性意见=同意 且 请假天数>7天", "target_node_id": "line_leader"},
                     {"path_name": "退回起草", "condition": "结论性意见=不同意", "target_node_id": "DRAFT"},
                 ]},
                {"node_id": "line_leader", "node_name": "条线分管领导审批", "is_draft": False,
                 "handler": {"mode": "单选-单人处理", "source": "本条线", "role": "条线分管领导", "source_field": None},
                 "submit_paths": [{"path_name": "流程结束", "condition": "结论性意见=同意", "target_node_id": "END"}]},
            ],
            "attachments": [
                {"attachment_type": "就诊证明", "upload_stages": ["all"], "required_stages": ["draft"],
                 "required_condition": "请假类型=病假 且 请假天数>=3"},
            ],
            "roles": None,
        }
    )


def build_mock_instances(org_data: dict[str, Any] | None = None) -> list[MockInstance]:
    org = org_data or load_org_seed()
    proc = _leave_gap_process()

    def ctx(instance_id: str, node: str, form: dict[str, Any], applicant: str = APPLICANT,
            materials: list[str] | None = None) -> InstanceContext:
        return build_instance_context(
            instance_id=instance_id, process=proc, current_node_id=node,
            form_values=form, org_data=org, initiator_user_id=applicant,
            uploaded_materials=materials or [],
        )

    base = {"开始日期": "2026-07-10", "结束日期": "2026-07-12", "请假事由": "个人事务", "工作交接人": "u_it_line_leader"}
    return [
        # —— 正常的（占多数，体现"卡住只是一小部分"）——
        MockInstance("inst_ok_1", "请假申请 · 年假 2 天", "员工请假申请流程", "flowing", None,
                     ctx("inst_ok_1", "mgr", {**base, "请假类型": "年假", "请假天数": 2, "结论性意见": "同意"})),
        MockInstance("inst_ok_2", "请假申请 · 事假 1 天", "员工请假申请流程", "completed", None, None),
        # 病假 10 天在办、但未上传就诊证明 → 供 #2 在办体检查出材料缺失
        MockInstance("inst_ok_3", "请假申请 · 病假 10 天", "员工请假申请流程", "flowing", None,
                     ctx("inst_ok_3", "line_leader", {**base, "请假类型": "病假", "请假天数": 10, "结论性意见": "同意"})),
        # —— ① 设计缺口：请假 5 天，落在部门总经理环节 4-7 天覆盖漏洞里，无前进路径 ——
        MockInstance("inst_gap", "请假申请 · 事假 5 天（卡住）", "员工请假申请流程", "stuck", "design_gap",
                     ctx("inst_gap", "gm", {**base, "请假类型": "事假", "请假天数": 5, "结论性意见": "同意"})),
        # —— ② 组织缺口：条线分管领导环节，发起人无部门归属 → 解析不到审批人 ——
        MockInstance("inst_org", "请假申请 · 病假 9 天（选不到人）", "员工请假申请流程", "stuck", "org_gap",
                     ctx("inst_org", "line_leader", {**base, "请假类型": "病假", "请假天数": 9, "结论性意见": "同意"}, applicant=NO_DEPT_APPLICANT)),
        # —— ③ 数据填错：本该 8 天误填 6 天，卡在漏洞里；改对了就自然走条线领导 ——
        MockInstance("inst_data", "请假申请 · 事假 6 天（疑似填错）", "员工请假申请流程", "stuck", "data_error",
                     ctx("inst_data", "gm", {**base, "请假类型": "事假", "请假天数": 6, "结论性意见": "同意"})),
    ]


def mock_instance_index(org_data: dict[str, Any] | None = None) -> dict[str, MockInstance]:
    return {m.instance_id: m for m in build_mock_instances(org_data)}
