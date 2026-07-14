# 06 子公司重大事项 target 人审记录

## 结论

当前 `standard/target.json` 已从 `draft` 升级为 `human_reviewed`，可作为本轮 eval target 使用。

## 审核方式

- 两个无背景倾向 subagent 独立审核同一份样例库 Excel 与 target。
- 用户基于两个审核结果给出修复意见。
- 已根据用户意见修正 source 和 target。

## 已落地的人审决策

1. 合规与风险管理委员会：raw source 故意设计为模糊内容，保留在待确认项，不进入确定性流程环节。
2. 结束本人处理/结束本部门处理：属于多人并行审批中的特殊运行控制动作；当前 demo 和运行流程不支持同一环节多人审批，因此不纳入 deterministic submit_paths。
3. 同时送并行审批：意味着 runtime 需要支持并行分支和并行审批；当前 demo 不纳入确定性路径。
4. 编号字段：按样例库 Excel 口径，以“弹框选择/起草环节可手工录入或选择”为准；生成规则仅作为逻辑说明。
5. 附件配置：当前 target 保留。
6. 任务名称：不自编固定枚举；raw source 已改为“任务名称与默认对口部门映射未冻结，待战略发展部维护确认”。

## 产物

- 默认评测 target：`data/06_subsidiary_major_matter/standard/target.json`
- 人审快照：`data/06_subsidiary_major_matter/standard/review/target_human_reviewed_20260622.json`
