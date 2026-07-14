"""发起问答副驾（#4）的流程目录 + 确定性路径预演。

目录里请假流程给了完整定义（可做路径预演）；其余给名称+用途（够"该走哪个流程"推荐）。
路径预演是确定性的：拿用户处境的假设属性，沿 submit_paths 用 condition_eval 走一遍，
得出"你会经过哪些环节"——扎在真实定义上，不是 LLM 编。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from app.io_utils import find_standard_json, load_process_definition
from app.runtime.condition_eval import evaluate_condition
from data.schema import ProcessDefinition

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_EOA140_CASE_DIR = _PROJECT_ROOT / "data/cases/EOA140_subsidiary_major_matter"
_EXPENSE_CASE_DIR = _PROJECT_ROOT / "data/cases/expense_reimbursement"
_PROCUREMENT_CASE_DIR = _PROJECT_ROOT / "data/cases/procurement_request"
_SEAL_CASE_DIR = _PROJECT_ROOT / "data/cases/seal_request"


def _clean_leave_process() -> ProcessDefinition:
    """完整覆盖各天数区间的请假流程（无漏洞），供路径预演。"""
    return ProcessDefinition.model_validate(
        {
            "meta": {"process_id": "LEAVE-001", "process_name": "员工请假申请流程", "version": "V1.0.0",
                     "responsible_dept": "人力资源部", "description": "员工请假的标准审批流程",
                     "applicant_scope": "全员", "entry_point": "OA系统"},
            "form_fields": [
                {"seq": 1, "field_name": "请假类型", "required_stages": ["draft"], "visible_stages": ["all"],
                 "editable_stages": ["draft"], "component_type": "下拉单选", "options": ["事假", "病假", "年假", "婚假", "产假"]},
                {"seq": 2, "field_name": "请假天数", "required_stages": ["all"], "visible_stages": ["all"],
                 "editable_stages": ["draft"], "component_type": "数字"},
            ],
            "flow_nodes": [
                {"node_id": "draft", "node_name": "起草", "is_draft": True, "handler": None,
                 "submit_paths": [{"path_name": "送部门主管", "condition": None, "target_node_id": "mgr"}]},
                {"node_id": "mgr", "node_name": "部门主管审批", "is_draft": False,
                 "handler": {"mode": "单选-单人处理", "source": "本部门", "role": "部门主管", "source_field": None},
                 "submit_paths": [
                     {"path_name": "直接结束", "condition": "结论性意见=同意 且 请假天数≤3天", "target_node_id": "END"},
                     {"path_name": "送部门总经理", "condition": "结论性意见=同意 且 请假天数>3天", "target_node_id": "gm"},
                 ]},
                {"node_id": "gm", "node_name": "部门总经理审批", "is_draft": False,
                 "handler": {"mode": "单选-单人处理", "source": "本部门", "role": "部门总经理", "source_field": None},
                 "submit_paths": [
                     {"path_name": "结束", "condition": "结论性意见=同意 且 请假天数≤7天", "target_node_id": "END"},
                     {"path_name": "送条线分管领导", "condition": "结论性意见=同意 且 请假天数>7天", "target_node_id": "line_leader"},
                 ]},
                {"node_id": "line_leader", "node_name": "条线分管领导审批", "is_draft": False,
                 "handler": {"mode": "单选-单人处理", "source": "本条线", "role": "条线分管领导", "source_field": None},
                 "submit_paths": [{"path_name": "流程结束", "condition": "结论性意见=同意", "target_node_id": "END"}]},
            ],
            "attachments": None, "roles": None,
        }
    )


def _eoa140_process() -> ProcessDefinition:
    """复用设计侧 gold 定义（与 workflow_definitions 里 seed 的同一份），不重复手写。"""
    return load_process_definition(find_standard_json(_EOA140_CASE_DIR))


def _expense_process() -> ProcessDefinition:
    """复用设计侧 gold 定义（与 workflow_definitions 里 seed 的同一份），不重复手写。"""
    return load_process_definition(find_standard_json(_EXPENSE_CASE_DIR))


def _procurement_process() -> ProcessDefinition:
    """P4 轻量目录级流程，复用同一份 gold 定义，不重复手写。"""
    return load_process_definition(find_standard_json(_PROCUREMENT_CASE_DIR))


def _seal_process() -> ProcessDefinition:
    """P4 轻量目录级流程，复用同一份 gold 定义，不重复手写。"""
    return load_process_definition(find_standard_json(_SEAL_CASE_DIR))


@dataclass(frozen=True)
class CatalogProcess:
    process_id: str
    name: str
    description: str
    definition: ProcessDefinition | None  # None = 仅目录项，无法路径预演


def build_catalog() -> list[CatalogProcess]:
    return [
        CatalogProcess("LEAVE-001", "员工请假申请流程", "各类请假（事假/病假/年假/婚假/产假等）的审批。", _clean_leave_process()),
        CatalogProcess("EOA140", "子公司重大事项审批备案流程", "集团内子公司重大事项的审批与备案，起草人所属子公司审核+高管审批，按对口部门分发会签，公司领导批示。", _eoa140_process()),
        CatalogProcess("EXPENSE-001", "费用报销流程", "员工垫付费用的报销申请，按报销总额分级审批，差旅住宿超标需附情况说明。", _expense_process()),
        CatalogProcess("PROC-001", "采购申请流程", "物资/服务采购的申请与审批，大额需委员会会签。", _procurement_process()),
        CatalogProcess("SEAL-001", "用印申请流程", "公章/合同章/法人章用印申请，高风险需法务审核。", _seal_process()),
    ]


def preview_path(process: ProcessDefinition, attributes: dict) -> list[str]:
    """假设各环节均通过（结论性意见=同意），沿 submit_paths 用 condition_eval 走一遍，
    返回会经过的环节名序列。走不通（覆盖漏洞/循环）则返回已走到的部分。"""
    ctx = {**attributes, "结论性意见": "同意"}
    draft = next((n for n in process.flow_nodes if n.is_draft), None)
    if draft is None:
        return []
    sequence: list[str] = [draft.node_name]
    # 起草出边（通常无条件）
    current = _first_forward_target(draft, ctx)
    seen: set[str] = {draft.node_id}
    while current and current not in ("END", "DRAFT") and current not in seen:
        node = process.get_node_by_id(current)
        if node is None:
            break
        sequence.append(node.node_name)
        seen.add(current)
        current = _first_forward_target(node, ctx)
    if current == "END":
        sequence.append("流程结束")
    return sequence


def _first_forward_target(node, ctx: dict) -> str | None:  # noqa: ANN001
    """该环节第一条满足条件、且非退回起草的路径的目标。"""
    for path in node.submit_paths:
        if path.target_node_id == "DRAFT":
            continue
        if evaluate_condition(path.condition, ctx).matched:
            return path.target_node_id
    return None
