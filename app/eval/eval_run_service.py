"""设计评测 · 评测运行读取 + 打分入历史（评测模式叠加在正常设计初始化之上）。

**模型**：评测模式 = 正常设计初始化（照样建流程管理卡片、照样进工作台）**额外**多打一次分。
所以评测运行**不再是独立管线**——它就是走 WorkflowDesignService 的初始化任务（产出卡片 +
run_dir），初始化完成后本模块把那个 run_dir 对选中的 gold 打分、写入运行历史。评测运行的
`run_id` **直接用该次设计产出的流程定义 id（wfd_ai_xxx）**——卡片和评测记录共享同一个 id，
天然一一对应，点评测历史能跳回流程管理那张卡片。

设计纪律：评测锚在**抽取步**（同一套 evaluate_run_against_target，和 CLI batch 对齐），
不锚被后续编辑的草稿，保证可复现。运行历史与 batch 基线（runs/eval_summary/{case}）文件布局
一致，前端同一读取器复用。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from app.eval.process_target_eval import evaluate_run_against_target, write_evaluation_report
from app.runtime.models import now_iso

PROJECT_ROOT = Path(__file__).resolve().parents[2]
CASES_CONFIG = PROJECT_ROOT / "data/eval_cases.json"
EVAL_RUNS_ROOT = PROJECT_ROOT / "runs/eval_runs"
BASELINE_ROOT = PROJECT_ROOT / "runs/eval_summary"

_CASE_NAMES = {
    "EOA140_subsidiary_major_matter": "子公司重大事项审批备案（EOA140）",
    "leave_request": "请假申请流程",
    "expense_reimbursement": "费用报销流程",
}
_INTERNAL_SOURCE_FILES = {"source_manifest.json", "uploaded_source_manifest.json", "input_manifest.json"}


def _safe_case_id(case_id: str) -> str:
    return "".join(c for c in case_id if c.isalnum() or c == "_")


def _safe_run_id(run_id: str) -> str:
    return "".join(c for c in run_id if c.isalnum() or c in {"_", "-"})


# ——— 案例 / 源清单 ———
def load_cases() -> dict[str, dict[str, Any]]:
    if not CASES_CONFIG.exists():
        return {}
    raw = json.loads(CASES_CONFIG.read_text(encoding="utf-8"))
    out: dict[str, dict[str, Any]] = {}
    for case in raw.get("cases", []):
        cid = case.get("case_id")
        if not cid:
            continue
        target = case.get("target", "")
        target_path = (PROJECT_ROOT / target) if target and not Path(target).is_absolute() else Path(target)
        case_dir = target_path.parent.parent if target_path.parts else None
        out[cid] = {
            "case_id": cid,
            "name": _CASE_NAMES.get(cid, cid),
            "target_path": target_path,
            "case_dir": case_dir,
            "allow_draft_target": bool(case.get("allow_draft_target", False)),
        }
    return out


def case_meta(case_id: str) -> dict[str, Any] | None:
    return load_cases().get(case_id)


def raw_source_files(case_dir: Path | None) -> list[Path]:
    if not case_dir:
        return []
    raw_dir = case_dir / "raw_sources"
    if not raw_dir.is_dir():
        return []
    return sorted(
        p
        for p in raw_dir.iterdir()
        if p.is_file() and not p.name.startswith(".") and p.name not in _INTERNAL_SOURCE_FILES
    )


def load_case_source_files(case_id: str, excluded: set[str] | None = None) -> list[dict[str, Any]]:
    """把案例 raw_source 读成 uploaded_files 形状（含 bytes），供初始化管线直接吃。"""
    case = case_meta(case_id)
    if case is None:
        raise KeyError(f"评测案例不存在：{case_id}")
    excluded = excluded or set()
    files: list[dict[str, Any]] = []
    for p in raw_source_files(case["case_dir"]):
        if p.name in excluded:
            continue
        files.append(
            {
                "filename": p.name,
                "content": p.read_bytes(),
                "source_type": p.suffix.removeprefix(".").lower() or "file",
                "size_bytes": p.stat().st_size,
            }
        )
    return files


def list_cases() -> dict[str, Any]:
    items = [
        {
            "case_id": c["case_id"],
            "name": c["name"],
            "source_count": len(raw_source_files(c["case_dir"])),
        }
        for c in load_cases().values()
        if c["case_dir"] and c["case_dir"].is_dir()
    ]
    return {"available": True, "cases": items}


def list_case_sources(case_id: str) -> dict[str, Any]:
    case = case_meta(case_id)
    if case is None:
        raise KeyError(f"评测案例不存在：{case_id}")
    return {
        "case_id": case_id,
        "name": case["name"],
        "sources": [
            {"name": p.name, "size_bytes": p.stat().st_size, "source_type": p.suffix.removeprefix(".").lower() or "file"}
            for p in raw_source_files(case["case_dir"])
        ],
    }


# ——— 运行历史 ———
def _case_runs_dir(case_id: str) -> Path:
    return EVAL_RUNS_ROOT / _safe_case_id(case_id)


def _index_path(case_id: str) -> Path:
    return _case_runs_dir(case_id) / "index.json"


def _read_index(case_id: str) -> list[dict[str, Any]]:
    path = _index_path(case_id)
    if not path.exists():
        return []
    data = json.loads(path.read_text(encoding="utf-8"))
    return data if isinstance(data, list) else []


def _baseline_entry(case_id: str) -> dict[str, Any] | None:
    base = BASELINE_ROOT / _safe_case_id(case_id) / "evaluation_report.json"
    if not base.exists():
        return None
    report = json.loads(base.read_text(encoding="utf-8"))
    score = report.get("score", {})
    review = report.get("review_metadata", {})
    return {
        "run_id": "baseline",
        "case_id": case_id,
        "kind": "baseline",
        "label": "基线（batch 快照）",
        "timestamp": review.get("reviewed_at") or "",
        "status": report.get("status"),
        "total": score.get("total"),
        "deterministic": score.get("deterministic_process_definition"),
        "clarification": score.get("clarification"),
        "evidence": score.get("evidence_and_guardrail"),
        "source_count": None,
        "excluded": [],
        "workflow_definition_id": None,
    }


def list_runs(case_id: str) -> dict[str, Any]:
    # 只返回产品页跑出来的评测运行（有卡片/id）。CLI 基线快照不再作为一条"运行"混入——
    # 它没有流程卡片、没有 id，概念上不是"某次设计运行"，会造成混淆。
    return {"case_id": case_id, "runs": list(_read_index(case_id))}


def _append_index(case_id: str, entry: dict[str, Any]) -> None:
    runs_dir = _case_runs_dir(case_id)
    runs_dir.mkdir(parents=True, exist_ok=True)
    index = _read_index(case_id)
    index.insert(0, entry)
    _index_path(case_id).write_text(json.dumps(index, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def load_run_report(case_id: str, run_id: str) -> dict[str, Any]:
    base = _case_runs_dir(_safe_case_id(case_id)) / _safe_run_id(run_id)
    path = base / "evaluation_report.json"
    if not path.exists():
        raise KeyError(f"评测运行不存在：{case_id}/{run_id}")
    report = json.loads(path.read_text(encoding="utf-8"))

    def _def(fname: str) -> dict[str, Any]:
        fp = base / fname
        if not fp.exists():
            return {}
        data = json.loads(fp.read_text(encoding="utf-8"))
        return data.get("deterministic_process_definition", data) if isinstance(data, dict) else {}

    report["gold_definition"] = _def("gold_target_snapshot.json")
    report["actual_definition"] = _def("actual_eval_target.json")
    return report


# 每个维度每项的属性槽位数（与 process_target_eval.ListEvalSpec.attr_count 对齐）——
# 折算属性准确率时需要用它把"匹配项数"换算成"总属性点数"。meta 不在此列，它是单份定值
# （ProcessMeta 固定 7 个字段），公式和 recall/precision 无关，另外处理。
_ATTR_COUNT = {"form_fields": 8, "flow_nodes": 7, "submit_paths": 1, "attachments": 2}
_META_FIELD_COUNT = 7  # process_id/process_name/version/responsible_dept/description/applicant_scope/entry_point


def _apply_semantic_judge(report: dict[str, Any]) -> dict[str, Any]:
    """把 LLM 判为同义的属性差异折算进主分：确定性字面比对先跑（决定 recall/precision + 哪些属性字面不符），
    对字面不符的属性再交 LLM 复评，判同义的当作抽对 → 重算该维度 attribute_accuracy → 重算维度分与总分。

    设计取舍：这一层用 LLM 判"两个属性值是不是同一个意思"——这本就是需要理解语义、确定性代码判不了
    的事，交给 LLM 是合适的；结构性的召回/精确仍由确定性层决定。判定结果连同高亮所需信息写进报告，
    前端直接读（不再手动触发）。"""
    from app.eval.semantic_judge import flatten_attribute_diffs, judge_diffs

    cr = report.get("comparison_report", {})
    det0 = report.get("deterministic_eval", {})
    meta_diffs = ((det0.get("details") or {}).get("meta") or {}).get("differences", [])
    diffs = flatten_attribute_diffs(cr, meta_differences=meta_diffs)
    if not diffs:
        report["semantic_judge"] = {"available": True, "judged": [], "equivalent_count": 0, "total_diffs": 0}
        return report
    try:
        verdicts = judge_diffs(diffs)
    except Exception as exc:  # noqa: BLE001 - 裁判失败不该拖垮打分，退回纯字面分
        verdicts = None
        report["semantic_judge"] = {"available": False, "reason": f"语义裁判异常：{exc}",
                                    "judged": [], "equivalent_count": 0, "total_diffs": len(diffs)}
    if verdicts is None:
        report.setdefault("semantic_judge", {"available": False, "reason": "语义裁判模型未就绪",
                                             "judged": [], "equivalent_count": 0, "total_diffs": len(diffs)})
        return report

    judged: list[dict[str, Any]] = []
    equiv_by_dim: dict[str, int] = {}
    diff_by_dim: dict[str, int] = {}
    for d in diffs:
        v = verdicts.get(d["id"])
        eq = bool(v.equivalent) if v else False
        diff_by_dim[d["dim"]] = diff_by_dim.get(d["dim"], 0) + 1
        if eq:
            equiv_by_dim[d["dim"]] = equiv_by_dim.get(d["dim"], 0) + 1
        judged.append({"dim": d["dim"], "location": d["location"], "attribute": d["attribute"],
                       "expected": d["expected"], "actual": d["actual"], "equivalent": eq,
                       "reason": (v.reason if v else "")})

    det = report.get("deterministic_eval", {})
    details = det.get("details", {})
    det_total_before = det.get("score", 0.0)
    for dim, K in equiv_by_dim.items():
        dd = details.get(dim)
        if not dd or K <= 0:
            continue
        weight = dd.get("weight", 0.0)
        if dim == "meta":
            # meta 是单份定值，没有 recall/precision——公式是 权重 × (总字段数-差异数)/总字段数
            new_diff = max(0, diff_by_dim.get(dim, 0) - K)
            new_acc = max(0.0, (_META_FIELD_COUNT - new_diff) / _META_FIELD_COUNT)
            dd["accuracy_literal"] = dd.get("accuracy")
            dd["score_literal"] = dd.get("score")
            dd["accuracy"] = new_acc
            dd["score"] = weight * new_acc
            dd["equivalent_count"] = K
            continue
        recall = dd.get("recall", 1.0)
        precision = dd.get("precision", 1.0)
        matched = max(0, dd.get("target_count", 0) - dd.get("missing_count", 0))
        total_attrs = matched * _ATTR_COUNT.get(dim, 0)
        if total_attrs <= 0:
            continue
        new_diff = max(0, diff_by_dim.get(dim, 0) - K)
        new_a = max(0.0, (total_attrs - new_diff) / total_attrs)
        dd["attribute_accuracy_literal"] = dd.get("attribute_accuracy")
        dd["score_literal"] = dd.get("score")
        dd["attribute_accuracy"] = new_a
        dd["score"] = weight * (0.60 * recall + 0.30 * new_a + 0.10 * precision)
        dd["equivalent_count"] = K
    new_det_total = sum(v.get("score", 0.0) for v in details.values())
    det["score_literal"] = det_total_before
    det["score"] = new_det_total
    # 折算后的主分：确定性维度分变了，待确认/护栏不变
    for key in ("score", "auto_score"):
        sc = report.get(key)
        if not sc:
            continue
        clar = sc.get("clarification", 0.0)
        ev = sc.get("evidence_and_guardrail", 0.0)
        sc["deterministic_process_definition_literal"] = sc.get("deterministic_process_definition")
        sc["total_literal"] = sc.get("total")
        sc["deterministic_process_definition"] = round(new_det_total, 2)
        sc["total"] = round(new_det_total + clar + ev, 2)
    report["semantic_judge"] = {
        "available": True,
        "judged": judged,
        "equivalent_count": sum(1 for j in judged if j["equivalent"]),
        "total_diffs": len(diffs),
        "literal_total": report.get("score", {}).get("total_literal"),
        "folded_total": report.get("score", {}).get("total"),
    }
    return report


def score_and_append_run(
    *,
    case_id: str,
    run_dir: Path,
    workflow_definition_id: str,
    workflow_name: str,
    session_id: str,
    included: list[str],
    excluded: list[str],
    extra_count: int,
) -> dict[str, Any]:
    """对初始化产出的 run_dir 打分，run_id = 流程定义 id（卡片↔评测共享一个 id），写入历史。"""
    case = case_meta(case_id)
    if case is None:
        raise KeyError(f"评测案例不存在：{case_id}")
    report = evaluate_run_against_target(
        run_dir, case["target_path"], allow_draft_target=case["allow_draft_target"]
    )
    report["case_id"] = case_id
    report = _apply_semantic_judge(report)  # LLM 同义复评折算进主分（评测路径专属，CLI batch 不受影响）
    run_id = _safe_run_id(workflow_definition_id) or workflow_definition_id
    write_evaluation_report(report, _case_runs_dir(case_id) / run_id)
    score = report.get("score", {})
    is_ablation = bool(excluded) or extra_count > 0
    if excluded and extra_count:
        label = f"消融 · 去{len(excluded)}加{extra_count}源"
    elif excluded:
        label = f"消融 · 去 {len(excluded)} 个源"
    elif extra_count:
        label = f"补料 · 加 {extra_count} 个源"
    else:
        label = "全量评测"
    weights = report.get("weights", {})
    det_max = sum(v for k, v in weights.items() if k not in ("clarifications", "evidence_guardrail"))
    entry = {
        "run_id": run_id,
        "case_id": case_id,
        "kind": "eval_run",
        "label": label,
        "timestamp": now_iso(),
        "status": report.get("status"),
        "total": score.get("total"),
        "deterministic": score.get("deterministic_process_definition"),
        "deterministic_max": det_max or None,
        "clarification": score.get("clarification"),
        "clarification_max": weights.get("clarifications"),
        "evidence": score.get("evidence_and_guardrail"),
        "evidence_max": weights.get("evidence_guardrail"),
        "source_count": len(included),
        "included": included,
        "excluded": excluded,
        "extra_count": extra_count,
        "is_ablation": is_ablation,
        "workflow_definition_id": workflow_definition_id,
        "workflow_name": workflow_name,
        "session_id": session_id,
    }
    _append_index(case_id, entry)
    return entry


class EvalRunService:
    """只读门面：案例/源清单 + 运行历史 + 某次运行报告（评测运行本身走 WDS 初始化任务）。"""

    def list_cases(self) -> dict[str, Any]:
        return list_cases()

    def list_case_sources(self, case_id: str) -> dict[str, Any]:
        return list_case_sources(case_id)

    def list_runs(self, case_id: str) -> dict[str, Any]:
        return list_runs(case_id)

    def load_run_report(self, case_id: str, run_id: str) -> dict[str, Any]:
        return load_run_report(case_id, run_id)
