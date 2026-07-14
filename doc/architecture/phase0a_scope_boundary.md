# Phase 0A 当前开发主线与冻结范围

## 当前主线

当前阶段项目主线收敛为：

> AI 流程设计 Agent + 可量化 Eval 基线。

重点是让 LangGraph 从多源材料抽取结构化 `ProcessDefinition`，输出待确认项，并用人工审定的 gold target 做稳定评测。

## 本阶段要做

- 固定 `01_leave_request`、`05_invitation_request`、`06_subsidiary_major_matter` 三个 eval case。
- 为每个 case 建立 reviewable target：JSON 是机器评测输入，Excel/Markdown 是人工审阅视图。
- 跑真实 LangGraph 输出，生成单案例 `evaluation_report.json/.md`。
- 汇总三案例评分，形成第一版 benchmark。
- 保留 human override 机制，用于记录规则评测无法处理的合理等价判断。

## 冻结范围

以下内容在 Phase 0A/Phase 1 暂停继续扩展：

- Runtime / SPA 的登录、工作台、组织架构、流程管理 UI。
- 流程运行时能力扩展。
- 前端流程图样式继续优化。
- RAG 知识库深度接入。
- 运营分析 Agent。
- Excel / draw.io 导出链路。

当前 Runtime / SPA 只作为 demo data layer，用来展示流程定义和产生后续可分析数据，不作为本阶段主开发线。

## 退出条件

Phase 1 完成后应具备：

- 三个固定 eval case 均可跑通 LangGraph 和 eval。
- 至少两个 target 经过人工审定并标记为 `human_reviewed`。
- `eval_summary` 能展示 auto score、human adjusted score、主要扣分项。
- report 能解释失败原因，而不是只给总分。
