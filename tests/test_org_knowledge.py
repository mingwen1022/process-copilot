from __future__ import annotations

import json
from pathlib import Path

from app.io_utils import is_process_definition_json
from app.org_knowledge import (
    detect_same_actor_escalations,
    get_user_assignments,
    load_org_seed,
    parse_knowledge_documents,
    resolve_role,
    validate_org_knowledge,
)


def test_org_and_knowledge_validation_passes() -> None:
    assert validate_org_knowledge() == []


def test_resolver_smoke_for_core_roles() -> None:
    data = load_org_seed()
    applicant = "u_it_app_staff"

    expected = {
        "部门主管": {"李承宇"},
        "部门总经理": {"赵文杰"},
        "条线分管领导": {"赵文杰"},
        "财务负责人": {"韩知行"},
        "法务审核": {"姜予安", "叶清辞"},
        "印章管理员": {"薛知夏"},
        "采购委员会": {"顾南舟", "韩知行", "顾清源", "汤明轩", "周启航"},
    }
    for role, names in expected.items():
        resolved = {user.name for user in resolve_role(data, role, applicant_user_id=applicant)}
        assert resolved == names


def test_concurrent_assignment_and_same_actor_warning() -> None:
    data = load_org_seed()
    assignments = get_user_assignments(data, "u_it_line_leader")

    assert assignments["primary"][0]["position_code"] == "LINE_LEADER"
    assert assignments["secondary"][0]["position_code"] == "GENERAL_MANAGER"

    issues = detect_same_actor_escalations(
        data,
        ["部门总经理", "条线分管领导"],
        applicant_user_id="u_it_app_staff",
    )
    assert issues == [
        {
            "previous_role": "部门总经理",
            "current_role": "条线分管领导",
            "overlap_user_ids": ["u_it_line_leader"],
            "recommendation": "需跳过或升级，不在 V2.1 自动执行",
        }
    ]


def test_current_standard_process_roles_are_covered_by_aliases() -> None:
    data = load_org_seed()
    aliases = {item["role_alias"] for item in data["role_aliases"]}
    roles: set[str] = set()
    for path in Path("data").glob("*/standard/*.json"):
        if not is_process_definition_json(path):
            continue
        payload = json.loads(path.read_text(encoding="utf-8"))
        process = payload.get("deterministic_process_definition", payload)
        for node in process["flow_nodes"]:
            handler = node.get("handler")
            if handler and handler.get("role"):
                roles.add(handler["role"])

    assert roles - aliases == set()


def test_knowledge_corpus_loads_recursively_and_anchors_to_real_cases() -> None:
    """新知识库按维度分子目录、锚定真实案例（LEAVE-001 / EOA140），不再引用
    已退役的 EXPENSE/SEAL/PROC 流程。旧版硬编码流程覆盖校验已废弃。"""
    docs = parse_knowledge_documents()
    assert docs, "知识库语料应被递归加载到"

    dimensions = {doc["metadata"].get("dimension") for doc in docs}
    assert {"company_policy", "design_standard", "org_role", "process_playbook", "ops_baseline"} <= dimensions

    anchored = set()
    retired = {"EXPENSE-001", "SEAL-001", "PROC-001", "ITCHANGE-001"}
    for doc in docs:
        procs = set(doc["metadata"].get("applies_to_processes", []))
        anchored |= procs
        assert not (procs & retired), f"{doc['path']} 仍引用已退役流程 {procs & retired}"
    assert "LEAVE-001" in anchored and "EOA140" in anchored
