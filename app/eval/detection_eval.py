"""运营分析 agent 的检出率评测（分析侧闭环 Phase 4）。

对齐设计侧"评测驱动"的纪律：拿 Phase 1 注入的病灶清单当 gold，量分析 agent
到底能不能把这些人为埋进去的堵点找出来。

评测对象是**确定性阈值层**（app.analytics.thresholds.detect_candidates），不是
LLM——检出能力按设计就是确定性的（LLM 只写归因文案），所以这份评测本身也确定
性、可复现、不依赖模型输出、不会因 LLM 抽风而波动。

两个诚实之处：
1. 僵尸实例（ZOMBIE_INSTANCE）当前不可检——需要"在途滞留时长"信号才能跟正常的
   近期在途区分，指标层还没有。评测里如实标为 detectable=False，既算"全部注入
   的覆盖率"（分母含僵尸），也算"可检病灶的检出率"（分母不含僵尸），两个都报。
2. 注入的病灶会有**下游连带信号**（如某环节慢→它的 SLA 也低；高退回→整体返工率
   高）。这些额外检出不是误报，是真实的二级现象。评测把额外检出分成"二级连带"
   和"真误报"，只把后者计入误报，且把两类都透明列出，不藏。
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from app.analytics.thresholds import (
    CATEGORY_CONFORMANCE_VIOLATION,
    CATEGORY_HIGH_RETURN_RATE,
    CATEGORY_HIGH_REWORK,
    CATEGORY_MANUAL_INTERVENTION,
    CATEGORY_SLA_BREACH,
    CATEGORY_SLOW_NODE,
    CandidateBottleneck,
    ThresholdConfig,
    detect_candidates,
)

# 注入病灶类型（DefectType 中文值）→ 阈值层候选类别
_DEFECT_TYPE_TO_CATEGORY: dict[str, str | None] = {
    "慢环节": CATEGORY_SLOW_NODE,
    "高退回率": CATEGORY_HIGH_RETURN_RATE,
    "超时违约": CATEGORY_SLA_BREACH,
    "违规跳级": CATEGORY_CONFORMANCE_VIOLATION,
    "线下人工介入": CATEGORY_MANUAL_INTERVENTION,
    "僵尸实例": None,  # 当前不可检（需在途滞留时长信号）
}

# 按环节匹配的类别（其余按类别匹配即可，因为候选本身是流程级）
_NODE_SPECIFIC_CATEGORIES = {CATEGORY_SLOW_NODE, CATEGORY_HIGH_RETURN_RATE, CATEGORY_SLA_BREACH}


class DefectMatch(BaseModel):
    defect_id: str
    defect_type: str
    target_node_id: str | None
    expected_category: str | None = Field(description="映射到的候选类别；None 表示当前不可检")
    detectable: bool
    detected: bool
    matched_bottleneck_id: str | None = None


class ExtraDetection(BaseModel):
    bottleneck_id: str
    category: str
    node_id: str | None
    classification: str = Field(description="secondary_consequence（二级连带，非误报）或 spurious（真误报）")
    reason: str


class DetectionScorecard(BaseModel):
    scenario_id: str | None = None
    total_injected: int
    detectable_injected: int
    detected_injected: int
    recall_over_detectable: float | None = Field(description="可检病灶的检出率 = 检出 / 可检")
    recall_over_all: float | None = Field(description="全部注入的覆盖率 = 检出 / 全部（分母含不可检的僵尸）")
    detected_candidate_count: int
    spurious_false_positive_count: int
    defect_matches: list[DefectMatch]
    extra_detections: list[ExtraDetection]


def evaluate_detection(
    metrics: dict[str, Any],
    defect_catalog: list[dict[str, Any]],
    *,
    scenario_id: str | None = None,
    threshold_config: ThresholdConfig | None = None,
) -> DetectionScorecard:
    candidates = detect_candidates(metrics, threshold_config)
    detected_categories = {c.category for c in candidates}

    matches: list[DefectMatch] = []
    matched_bottleneck_ids: set[str] = set()
    for defect in defect_catalog:
        expected = _DEFECT_TYPE_TO_CATEGORY.get(defect["type"])
        matched = _find_match(defect, expected, candidates)
        if matched is not None:
            matched_bottleneck_ids.add(matched.bottleneck_id)
        matches.append(
            DefectMatch(
                defect_id=defect["defect_id"],
                defect_type=defect["type"],
                target_node_id=defect.get("target_node_id"),
                expected_category=expected,
                detectable=expected is not None,
                detected=matched is not None,
                matched_bottleneck_id=matched.bottleneck_id if matched else None,
            )
        )

    extras = _classify_extras(candidates, matched_bottleneck_ids, detected_categories)

    detectable = [m for m in matches if m.detectable]
    detected_detectable = [m for m in detectable if m.detected]
    spurious = [e for e in extras if e.classification == "spurious"]

    return DetectionScorecard(
        scenario_id=scenario_id,
        total_injected=len(matches),
        detectable_injected=len(detectable),
        detected_injected=len(detected_detectable),
        recall_over_detectable=_safe_div(len(detected_detectable), len(detectable)),
        recall_over_all=_safe_div(len(detected_detectable), len(matches)),
        detected_candidate_count=len(candidates),
        spurious_false_positive_count=len(spurious),
        defect_matches=matches,
        extra_detections=extras,
    )


def _find_match(
    defect: dict[str, Any], expected_category: str | None, candidates: list[CandidateBottleneck]
) -> CandidateBottleneck | None:
    if expected_category is None:
        return None
    for candidate in candidates:
        if candidate.category != expected_category:
            continue
        if expected_category in _NODE_SPECIFIC_CATEGORIES:
            if candidate.node_id == defect.get("target_node_id"):
                return candidate
        else:
            return candidate  # 流程级类别（违规跳级/人工介入）按类别匹配即可
    return None


def _classify_extras(
    candidates: list[CandidateBottleneck],
    matched_bottleneck_ids: set[str],
    detected_categories: set[str],
) -> list[ExtraDetection]:
    """把没匹配上任何注入病灶的检出，分成"二级连带"和"真误报"。

    二级连带规则（这个场景里真实存在的因果）：
    - 某环节 sla_breach，且同环节也被判为 slow_node → SLA 低是因为慢，二级现象。
    - high_rework（整体返工率高），且检出了 high_return_rate → 返工高是退回高的直接后果。
    其余没匹配上的一律算真误报。
    """
    slow_nodes = {c.node_id for c in candidates if c.category == CATEGORY_SLOW_NODE}
    extras: list[ExtraDetection] = []
    for candidate in candidates:
        if candidate.bottleneck_id in matched_bottleneck_ids:
            continue
        classification, reason = "spurious", "未对应任何注入病灶"
        if candidate.category == CATEGORY_SLA_BREACH and candidate.node_id in slow_nodes:
            classification, reason = "secondary_consequence", "该环节被判为慢环节，SLA 低是慢的连带结果"
        elif candidate.category == CATEGORY_HIGH_REWORK and CATEGORY_HIGH_RETURN_RATE in detected_categories:
            classification, reason = "secondary_consequence", "整体返工率高是高退回率的直接后果"
        extras.append(
            ExtraDetection(
                bottleneck_id=candidate.bottleneck_id,
                category=candidate.category,
                node_id=candidate.node_id,
                classification=classification,
                reason=reason,
            )
        )
    return extras


def _safe_div(numerator: int, denominator: int) -> float | None:
    return round(numerator / denominator, 4) if denominator else None


def render_scorecard_markdown(scorecard: DetectionScorecard) -> str:
    lines = [
        f"# 运营分析 agent 检出率评测 · {scorecard.scenario_id or ''}".rstrip(),
        "",
        f"- 注入病灶：{scorecard.total_injected}（其中可检 {scorecard.detectable_injected}，僵尸类当前不可检）",
        f"- **可检病灶检出率**：{_pct(scorecard.recall_over_detectable)}（{scorecard.detected_injected}/{scorecard.detectable_injected}）",
        f"- 全部注入覆盖率：{_pct(scorecard.recall_over_all)}（{scorecard.detected_injected}/{scorecard.total_injected}）",
        f"- 检出候选总数：{scorecard.detected_candidate_count}；真误报：{scorecard.spurious_false_positive_count}",
        "",
        "## 逐病灶命中",
        "",
        "| 注入病灶 | 类型 | 环节 | 可检 | 检出 |",
        "|---|---|---|---|---|",
    ]
    for m in scorecard.defect_matches:
        lines.append(
            f"| {m.defect_id} | {m.defect_type} | {m.target_node_id or '—'} | "
            f"{'是' if m.detectable else '否（设计缺口）'} | {'✓' if m.detected else '✗'} |"
        )
    lines += ["", "## 额外检出（非注入病灶）", ""]
    if scorecard.extra_detections:
        lines += ["| 候选 | 分类 | 说明 |", "|---|---|---|"]
        for e in scorecard.extra_detections:
            label = "二级连带" if e.classification == "secondary_consequence" else "真误报"
            lines.append(f"| {e.bottleneck_id} | {label} | {e.reason} |")
    else:
        lines.append("（无）")
    return "\n".join(lines)


def _pct(value: float | None) -> str:
    return f"{value * 100:.1f}%" if value is not None else "—"
