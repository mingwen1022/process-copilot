"""分析侧看板/agent 的只读数据服务（分析侧闭环 Phase 3.0 / 3.1 / 3.2）。

不持久化、不缓存指标：每次调用都从 data/analytics/leave_request/ 下已生成的
合成数据重新算一遍——数据量小（数百案例），重算成本可忽略，换来的是"看到的
永远是最新一次生成结果"，不用操心缓存失效。唯一持久化的是"转正"的自定义指标
（custom_metrics.json），因为那是用户主动登记的常态化跟踪，需要跨请求保留。

先只接请假流程这一个案例（跟设计侧同样的"先做深一个案例"节奏），
之后如果要接第二个案例，再考虑要不要参数化 case_id。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from uuid import uuid4

from app.agents.analytics_query_agent import AnalyticsQueryAgent
from app.agents.process_analysis_agent import ProcessAnalysisAgent
from app.analytics.metrics import compute_metrics, load_case_records
from app.analytics.query import (
    AdHocMetricQuery,
    load_custom_metrics,
    run_ad_hoc_query,
    upsert_custom_metric,
)
from app.analytics.sql_store import open_sql_store
from app.analytics.thresholds import CandidateBottleneck, ThresholdConfig, detect_candidates
from app.analytics.version_comparison import compare_versions
from app.insights.producers import SUPPORTED_ANALYTICS_CATEGORIES, AnalyticsInsightProducer
from app.io_utils import load_process_definition
from app.runtime.models import now_iso
from app.tools.design_feedback_tool import build_flag_design_issue_tool
from app.tools.metric_query_tool import build_run_metric_query_tool
from app.tools.sql_query_tool import build_run_sql_tool
from data.schema import CaseRecord, ProcessDefinition, ScenarioConfig

PROJECT_ROOT = Path(__file__).resolve().parents[2]
LEAVE_ANALYTICS_DIR = PROJECT_ROOT / "data/analytics/leave_request"
_MAX_DIAGNOSIS_HISTORY = 20  # 存档上限，超过丢最旧的，避免无限增长


# 价值层用的标准工时系数（小时）。**计次是真实的，系数是明示的假设值**，调这里即可。
# 每项后面标的是"接真实数据时该在哪埋点"——demo 阶段先按计次估算，不影响口径成立。
_EFFORT_HOURS = {
    "审批动作": 5 / 60,    # 埋点：每次审批动作提交时记一条（谁、哪条流程、哪个环节）
    "运维处置": 20 / 60,   # 埋点：副驾处置方案被确认时、授权台批准时各记一条
    "设计初始化": 1.5,     # 埋点：初始化任务完成时记一条，带实际耗时
    "设计修改": 10 / 60,   # 埋点：会话式修改每确认一次改动记一条
}
# 设计侧尚未埋点，demo 用示意值；接上埋点后换成真实轮次/次数即可。
_DEMO_DESIGN_ROUNDS = 6
# 还没有任何反哺洞察时，采纳率的示意基线（已采纳 / 全部）；一旦真有洞察就走真算。
_DEMO_ADOPTED = (3, 5)


class AnalyticsService:
    def __init__(
        self,
        analytics_dir: str | Path = LEAVE_ANALYTICS_DIR,
        analysis_agent: ProcessAnalysisAgent | None = None,
        query_agent: AnalyticsQueryAgent | None = None,
        threshold_config: ThresholdConfig | None = None,
        insight_store: Any | None = None,
        design_activity: Any | None = None,
    ):
        self.analytics_dir = Path(analytics_dir)
        # 惰性构造 agent：只有真正需要时才创建 Bedrock client，
        # 看板/临时查询执行（只算确定性指标）不需要模型。
        self._analysis_agent = analysis_agent
        self._query_agent = query_agent
        # 候选堵点判定阈值——诊断报告的 agent 和这里独立算候选（对话反哺工具用）要用
        # 同一份阈值，不然"报告里判定是堵点"和"对话里判定是堵点"可能对不上。
        self._threshold_config = threshold_config or ThresholdConfig()
        self._insight_store = insight_store  # 反哺闭环：分析侧写入运行洞察（可空，不影响看板/问答本身）
        # 价值层「负责人自助上线」用：一个返回 {published/initialized/adjusted} 的可调用对象。
        # 设计侧数据不归分析服务管，所以由外部注入，避免这里反向依赖设计服务。
        self._design_activity = design_activity

    # ── 看板 ──────────────────────────────────────
    def dashboard_metrics(self) -> dict[str, Any]:
        scenario, process, cases = self._load()
        metrics = self._compute(scenario, process, cases)
        metrics["custom_metrics"] = self._run_custom_metrics(cases)
        metrics["value_metrics"] = self._value_metrics(metrics, process)
        return metrics

    # ── 看板顶部「价值层」：证明这套系统有没有省力（下层那几张是描述现状的运行统计）──
    # 纪律跟别处一致：每项都标数据成色（真算 / 部分真算 / 按动作计次估算），不藏水分。
    # 尚未埋点的地方，埋点位置写在 _EFFORT_HOURS 与各项 note 里，便于接真实数据时按图索骥。
    def _value_metrics(self, metrics: dict[str, Any], process: Any) -> dict[str, Any]:
        ov = metrics.get("overview") or {}
        node_metrics = metrics.get("node_metrics") or {}

        # ① 每条流程全生命周期人工投入（北极星）
        # 口径：人工动作计次 × 标准工时。**不能直接用停留时长**——那里面含排队等待，
        # 不等于人真的花了这么久。计次是真实的，工时系数是明示的假设值。
        approval_actions = sum(v.get("visits", 0) for v in node_metrics.values())
        ops_actions = ov.get("manual_intervention_case_count", 0)
        run_hours = approval_actions * _EFFORT_HOURS["审批动作"]
        ops_hours = ops_actions * _EFFORT_HOURS["运维处置"]
        design_hours = _EFFORT_HOURS["设计初始化"] + _DEMO_DESIGN_ROUNDS * _EFFORT_HOURS["设计修改"]
        total_hours = design_hours + run_hours + ops_hours

        # ② 负责人自助上线
        # 衡量的是**设计侧自主性**：流程负责人不找系统管理员/产品经理，自己用副驾把流程
        # 建起来、改好、上架。"有没有对外求助"本身不好统计，改用三个已有真实记录侧面反映：
        # 上架条数（完成信号）＋ 初始化条数、确认落地的调整次数（工具被真实使用的程度）。
        # 主数字用**上架数**而不是调整次数——调整多不一定是好事（可能是工具难用导致反复改），
        # 上架才代表"真的自己做完了"。
        activity = self._design_activity() if self._design_activity else {}

        # ③ 优化建议采纳率——有洞察就真算（不用新埋点：洞察本来就有 新发现/已查看/已消解 三态）；
        # 一条洞察都还没产生时（刚切回起始态、还没跑过诊断），退化为示意基线，免得卡片空着。
        adopted_ratio, adopted_n, insight_n = self._insight_adoption(process)
        adoption_estimated = insight_n == 0
        if adoption_estimated:
            adopted_n, insight_n = _DEMO_ADOPTED
            adopted_ratio = adopted_n / insight_n

        return {
            "manual_effort_hours": round(total_hours, 1),
            "manual_effort_breakdown": {
                "设计": round(design_hours, 1), "运行": round(run_hours, 1), "运维": round(ops_hours, 1),
            },
            "manual_effort_estimated": True,
            "self_service_published": activity.get("published", 0),
            "self_service_basis": {
                "初始化": activity.get("initialized", 0), "确认调整": activity.get("adjusted", 0),
            },
            "self_service_estimated": False,
            "insight_adoption_ratio": adopted_ratio,
            "insight_adoption_basis": {"已采纳": adopted_n, "全部": insight_n},
            "insight_adoption_estimated": adoption_estimated,
        }

    def _insight_adoption(self, process: Any) -> tuple[float | None, int, int]:
        """采纳率 = （已查看 + 已消解）÷ 全部洞察。设计侧确认修复时会把洞察置为已消解，
        所以演示中修掉一条，这个数字会当场往上走——闭环是看得见的。"""
        if self._insight_store is None:
            return None, 0, 0
        try:
            key = process.meta.process_id
            mine = [i for i in self._insight_store.all_insights() if i.workflow_definition_id == key]
        except Exception:  # noqa: BLE001 - 取不到洞察不影响看板其余部分
            return None, 0, 0
        if not mine:
            return None, 0, 0
        adopted = sum(1 for i in mine if getattr(i.status, "value", i.status) in ("acknowledged", "resolved"))
        return adopted / len(mine), adopted, len(mine)

    # ── 诊断（Mode A）────────────────────────────
    def diagnosis_report(self) -> dict[str, Any]:
        """生成一份新诊断报告，打上时间戳与 id，追加进历史存档（不覆盖旧的），返回之。
        每次生成 = 一份带时间点的存档，之后能回来打开任意一份历史报告。诊断要花
        一次 LLM 调用，所以只有显式点"重新生成"才会新增一份。

        反哺闭环场景一：报告里的候选堵点是 detect_candidates() 确定性判定的真问题，
        每次生成报告时无条件全量反哺进 InsightStore（幂等聚合，重复生成不会重复堆积），
        不需要 LLM 判断"要不要反哺"——跟运维侧"每次 diagnose() 自动 record_incident"
        是同一个模式。"""
        scenario, process, cases = self._load()
        metrics = self._compute(scenario, process, cases)
        agent = self._analysis_agent or ProcessAnalysisAgent(threshold_config=self._threshold_config)
        report = agent.diagnose(metrics)
        report.generated_at = now_iso()
        report.report_id = uuid4().hex[:12]
        payload = report.model_dump(mode="json")
        self._record_candidates_to_insights(process, self._detect_candidates(metrics))

        history = self._load_history()
        history.insert(0, payload)  # 新的在前
        self._save_history(history[:_MAX_DIAGNOSIS_HISTORY])
        return payload

    def diagnosis_overview(self) -> dict[str, Any]:
        """一次返回：历史报告的轻量列表（供选择器）+ 最新一份完整报告（默认展示）。
        不触发新的 LLM 调用。"""
        history = self._load_history()
        reports_meta = [
            {
                "report_id": r.get("report_id"),
                "generated_at": r.get("generated_at"),
                "candidate_count": r.get("candidate_count"),
                "llm_available": r.get("llm_available", True),
            }
            for r in history
        ]
        return {"reports": reports_meta, "latest": history[0] if history else None}

    def get_diagnosis(self, report_id: str) -> dict[str, Any] | None:
        """按 id 取一份历史报告的完整内容；不存在则 None。"""
        for report in self._load_history():
            if report.get("report_id") == report_id:
                return report
        return None

    def _load_history(self) -> list[dict[str, Any]]:
        path = self._diagnosis_path()
        if not path.exists():
            return []
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []

    def _save_history(self, history: list[dict[str, Any]]) -> None:
        path = self._diagnosis_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(history, ensure_ascii=False, indent=2), encoding="utf-8")

    # ── v1/v2 效能对比（Phase 5 · headline）──────
    def version_comparison(self) -> dict[str, Any]:
        v1 = self._metrics_for_version("scenario_v1.json", "event_log_v1.jsonl")
        v2 = self._metrics_for_version("scenario_v2.json", "event_log_v2.jsonl")
        if v1 is None or v2 is None:
            return {"available": False}
        return {"available": True, **compare_versions(v1, v2).model_dump(mode="json")}

    def _metrics_for_version(self, scenario_file: str, log_file: str) -> dict[str, Any] | None:
        spath = self.analytics_dir / scenario_file
        lpath = self.analytics_dir / log_file
        if not spath.exists() or not lpath.exists():
            return None
        scenario = ScenarioConfig.model_validate_json(spath.read_text(encoding="utf-8"))
        process = load_process_definition(scenario.process_target_path)
        cases = load_case_records(lpath)
        return self._compute(scenario, process, cases)

    # ── 对话式指标问答（Mode B）──────────────────
    def answer_question(self, *, message: str, history: list[dict[str, str]] | None = None) -> dict[str, Any]:
        """走 run_with_tools 的取数梯度：快照直接答（0 次工具调用，跟原来单次
        调用同价）→ 答不了升级 run_metric_query（typed，安全）→ 还不够再升级
        run_sql（只读+护栏，见 app.tools.sql_query_tool）。要不要查、查哪一级
        由 agent 自己判断，不在这里预先分类路由——ReAct 循环本身已经把"快照够用
        就不调工具"这件事内建了，没必要在外面再包一层分类。

        反哺闭环场景二：另给一个 flag_design_issue 工具，只认服务端此刻自己算出的
        确定性候选（SUPPORTED_ANALYTICS_CATEGORIES 范围内），agent 编不出候选 id、
        也就编不出反哺内容——它能自主判断的只是"现在要不要调用"（对话里明确指向
        某条候选且像是真实问题，可直接调；拿不准就先问用户，用户确认后下一轮再调），
        不是"这算不算问题"。"""
        scenario, process, cases = self._load()
        metrics = self._compute(scenario, process, cases)
        custom = load_custom_metrics(self._custom_metrics_path())
        agent = self._query_agent or AnalyticsQueryAgent()
        candidates = [c for c in self._detect_candidates(metrics) if c.category in SUPPORTED_ANALYTICS_CATEGORIES]

        with open_sql_store(cases) as store:
            tools = [
                build_run_metric_query_tool(cases, custom_metrics_path=str(self._custom_metrics_path())),
                build_run_sql_tool(store.conn),
                build_flag_design_issue_tool(
                    candidates, insight_store=self._insight_store, workflow_definition_id=process.meta.process_id,
                ),
            ]
            result = agent.run_with_tools(
                message=message,
                metrics_snapshot=metrics,
                schema_ddl=store.schema_ddl(),
                tools=tools,
                conversation_context=_render_history(history or []),
                custom_metric_names=[q.name for q in custom],
                design_candidates=candidates,
            )

        query_results: list[dict[str, Any]] = []
        sql_runs: list[dict[str, Any]] = []
        persisted_names: list[str] = []
        design_flags: list[dict[str, Any]] = []
        for call in result.tool_calls:
            if call.tool == "run_metric_query":
                query_results.append(call.result)
                if call.result.get("persisted") and call.result.get("name"):
                    persisted_names.append(call.result["name"])
            elif call.tool == "run_sql":
                sql_runs.append(
                    {
                        "sql": call.result.get("sql") or call.args.get("sql", ""),
                        "columns": call.result.get("columns", []),
                        "rows": call.result.get("rows", []),
                        "truncated": call.result.get("truncated", False),
                        "error": call.result.get("error"),
                    }
                )
            elif call.tool == "flag_design_issue":
                design_flags.append(call.result)

        return {
            "reply": result.reply,
            "out_of_scope": False,
            "query_results": query_results,
            "sql_runs": sql_runs,
            "persisted_metric_names": persisted_names,
            "design_flags": design_flags,
        }

    # ── 起始态预置 ────────────────────────────────
    def seed_insights(self) -> None:
        """把当前合成数据里确定性检出的候选堵点沉淀进 InsightStore——起始态就让设计侧看到
        "运行侧已发现的效能问题"（慢环节/退回高/时限达成低），一进来就有真实反馈可处理。

        跟 diagnosis_report() 走同一套 record（幂等聚合），只是不做 AI 归因、不写
        diagnosis_history——即"洞察层已确定性沉淀发现，但还没生成 AI 诊断报告"，所以诊断
        报告页仍然是空的（现场生成的演示桥段保留）。洞察内容全部锚在真实合成数据上，
        不是凭空造的问题。"""
        if self._insight_store is None:
            return
        scenario, process, cases = self._load()
        metrics = self._compute(scenario, process, cases)
        self._record_candidates_to_insights(process, self._detect_candidates(metrics))

    # ── 内部 ──────────────────────────────────────
    def _detect_candidates(self, metrics: dict[str, Any]) -> list[CandidateBottleneck]:
        return detect_candidates(metrics, self._threshold_config)

    def _record_candidates_to_insights(self, process: ProcessDefinition, candidates: list[CandidateBottleneck]) -> None:
        if self._insight_store is None:
            return
        AnalyticsInsightProducer.record_from_candidates(
            self._insight_store, workflow_definition_id=process.meta.process_id, candidates=candidates,
        )

    def _load(self) -> tuple[ScenarioConfig, ProcessDefinition, list[CaseRecord]]:
        scenario = self._load_scenario()
        process = load_process_definition(scenario.process_target_path)
        cases = load_case_records(self._event_log_path())
        return scenario, process, cases

    def _compute(self, scenario: ScenarioConfig, process: ProcessDefinition, cases: list[CaseRecord]) -> dict[str, Any]:
        metrics = compute_metrics(cases, process=process, assumed_sla_days=scenario.assumed_sla_days)
        # node_metrics 只用 node_id 做 key（供代码消费），展示层需要中文名——
        # 这层映射属于"给前端看的附加信息"，不属于确定性指标计算本身，
        # 所以放在 API 服务层拼装，不污染 app.analytics.metrics 的纯计算职责。
        metrics["node_names"] = {node.node_id: node.node_name for node in process.flow_nodes}
        metrics["process_name"] = process.meta.process_name
        return metrics

    def _run_custom_metrics(self, cases: list[CaseRecord]) -> list[dict[str, Any]]:
        queries: list[AdHocMetricQuery] = load_custom_metrics(self._custom_metrics_path())
        return [run_ad_hoc_query(q, cases).model_dump(mode="json") for q in queries]

    def _load_scenario(self) -> ScenarioConfig:
        path = self.analytics_dir / "scenario_v1.json"
        if not path.exists():
            raise RuntimeError(f"未找到场景配置：{path}，请先运行 scripts/gen_leave_process_event_log.py")
        return ScenarioConfig.model_validate_json(path.read_text(encoding="utf-8"))

    def _event_log_path(self) -> Path:
        path = self.analytics_dir / "event_log_v1.jsonl"
        if not path.exists():
            raise RuntimeError(f"未找到事件日志：{path}，请先运行 scripts/gen_leave_process_event_log.py")
        return path

    def _custom_metrics_path(self) -> Path:
        return self.analytics_dir / "custom_metrics.json"

    def _diagnosis_path(self) -> Path:
        return self.analytics_dir / "diagnosis_history.json"


def _render_history(history: list[dict[str, str]]) -> str:
    lines = []
    for item in history:
        role = "用户" if item.get("role") == "user" else "助手"
        content = (item.get("content") or "").strip()
        if content:
            lines.append(f"{role}：{content}")
    return "\n".join(lines)
