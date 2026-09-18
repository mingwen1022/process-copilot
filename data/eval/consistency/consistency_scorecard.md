# 横切一致性 / 护栏断言评测

- case 数：4
- 断言检出率（正控）：1.0
- 误报数：0
- 干净基线数（期望自洽且实际自洽）：3

| case | 期望断言 | 实际检出 | 漏检 | 误报 |
|---|---|---|---|---|
| LLM说跳条线分管领导实则填起草（确定性层应中和） | — | — | — | — |
| 改派给不存在的人却承诺已转授权（确定性层应改写文案） | — | — | — | — |
| rationale 直写 node_id（正控，应被检出泄漏） | identifier_leak | identifier_leak | — | — |
| 正常修数据（干净基线） | — | — | — | — |

## 命中的断言明细

- **[identifier_leak]** `rationale 直写 node_id（正控，应被检出泄漏）` @rationale：用户可见文案出现内部词「target_node_id」
  - 证据：`目标环节 target_node_id=line_leader`
- **[identifier_leak]** `rationale 直写 node_id（正控，应被检出泄漏）` @rationale：用户可见文案出现内部词「node_id」
  - 证据：`目标环节 target_node_id=line_leader`
- **[identifier_leak]** `rationale 直写 node_id（正控，应被检出泄漏）` @rationale：用户可见文案出现环节 id「line_leader」，应显示环节名「条线分管领导审批」
  - 证据：`get_node_id=line_leader，比部门总经理高一档。`
