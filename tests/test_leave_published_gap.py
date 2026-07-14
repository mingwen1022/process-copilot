"""请假发布版 V1.0 相对金标准的"故意缺口"——反哺闭环演示的真实前提。

发布的 V1.0（slice1 seed 进 db 的定义 = 设计副驾进入设计时的 base）在 dept_gm 环节
留了一档 4~7 天的覆盖漏洞；金标准 target.json 不含此缺口。二者不同，是"上线后被运行数据
发现、经反哺闭环修复"这条叙事成立的前提——不然发布的就是完美的，没东西可反哺可修。
"""

from __future__ import annotations

import json
import sqlite3

from app.api.slice1_service import (
    LEAVE_CASE_DIR,
    LEAVE_WORKFLOW_DEFINITION_ID,
    Slice1Service,
    _inject_leave_published_gap,
)
from app.io_utils import find_standard_json, load_process_definition


def _end_condition(process, *, node_id: str = "dept_gm", path_name: str = "流程结束") -> str:
    node = next(n for n in process.flow_nodes if n.node_id == node_id)
    return next(p.condition for p in node.submit_paths if p.path_name == path_name)


def test_inject_only_lowers_day_limit_and_leaves_gold_untouched() -> None:
    gold = load_process_definition(find_standard_json(LEAVE_CASE_DIR))
    published = _inject_leave_published_gap(gold)

    assert "≤7天" in _end_condition(gold)  # 金标准不含缺口（4~7天可直接结束）
    assert "≤3天" in _end_condition(published)  # 发布 V1.0 有缺口（只到3天）
    assert "≤7天" not in _end_condition(published)
    # deep copy：注入不就地改坏金标准对象
    assert "≤7天" in _end_condition(gold)
    # 只动「流程结束」这一档，同环节的「送条线分管领导审批」不受影响
    gm_gold = next(n for n in gold.flow_nodes if n.node_id == "dept_gm")
    gm_pub = next(n for n in published.flow_nodes if n.node_id == "dept_gm")
    line_path = "送条线分管领导审批"
    assert (
        next(p.condition for p in gm_pub.submit_paths if p.path_name == line_path)
        == next(p.condition for p in gm_gold.submit_paths if p.path_name == line_path)
    )


def test_inject_is_noop_when_no_matching_gold_condition() -> None:
    """金标准哪天改了措辞、找不到 ≤7天 阈值时，原样返回不硬塞、不崩——mock 排障流程用
    node_id gm（非 dept_gm）且条件已是 ≤3天，正好复现"匹配不上"的情形。"""
    from app.copilots.mock_instances import _leave_gap_process

    process = _leave_gap_process()
    assert _inject_leave_published_gap(process).model_dump() == process.model_dump()


def test_seeded_published_leave_definition_carries_the_gap(tmp_path) -> None:
    """端到端：Slice1Service 播种进 db 的请假发布定义（= 设计副驾的 base）确实带缺口，
    而金标准文件仍然无缺口——两者在 db/文件两侧同时可验证。"""
    db_path = tmp_path / "runtime.db"
    Slice1Service(db_path)  # __init__ 触发 ensure_seed_data

    conn = sqlite3.connect(db_path)
    try:
        row = conn.execute(
            "SELECT definition_json FROM workflow_definitions WHERE id = ?",
            (LEAVE_WORKFLOW_DEFINITION_ID,),
        ).fetchone()
    finally:
        conn.close()
    published = json.loads(row[0])
    gm = next(n for n in published["flow_nodes"] if n["node_id"] == "dept_gm")
    end_cond = next(p["condition"] for p in gm["submit_paths"] if p["path_name"] == "流程结束")
    assert "≤3天" in end_cond and "≤7天" not in end_cond  # 发布库里就是有缺口的 V1.0

    gold = load_process_definition(find_standard_json(LEAVE_CASE_DIR))
    assert "≤7天" in _end_condition(gold)  # 金标准文件不受播种影响
