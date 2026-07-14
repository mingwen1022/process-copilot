# 运营分析 agent 检出率评测 · leave_v1

- 注入病灶：6（其中可检 5，僵尸类当前不可检）
- **可检病灶检出率**：100.0%（5/5）
- 全部注入覆盖率：83.3%（5/6）
- 检出候选总数：7；真误报：0

## 逐病灶命中

| 注入病灶 | 类型 | 环节 | 可检 | 检出 |
|---|---|---|---|---|
| slow_line_leader | 慢环节 | line_leader | 是 | ✓ |
| high_return_dept_supervisor | 高退回率 | dept_supervisor | 是 | ✓ |
| sla_breach_dept_gm | 超时违约 | dept_gm | 是 | ✓ |
| illegal_skip_line_leader | 违规跳级 | dept_gm | 是 | ✓ |
| manual_intervention_case | 线下人工介入 | — | 是 | ✓ |
| zombie_case | 僵尸实例 | — | 否（设计缺口） | ✗ |

## 额外检出（非注入病灶）

| 候选 | 分类 | 说明 |
|---|---|---|
| sla_breach:line_leader | 二级连带 | 该环节被判为慢环节，SLA 低是慢的连带结果 |
| high_rework:process | 二级连带 | 整体返工率高是高退回率的直接后果 |
