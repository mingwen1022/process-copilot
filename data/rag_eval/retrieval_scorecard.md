# 检索命中率评测（recall@5）

- **recall@5**：100.0%（10/10）

| query | 期望规则 | 命中 | 名次 |
|---|---|---|---|
| 病假请假需要上传什么材料 | leave.sick_leave_certificate | ✓ | 1 |
| 请假超过七天要经过哪一级领导审批 | leave.line_leader_for_long_or_special | ✓ | 1 |
| 请假类型开始结束日期天数事由必填吗 | leave.core_fields_required_at_draft | ✓ | 1 |
| 费用报销五万以上要不要财务负责人审批 | auth.finance_head_for_large_expense | ✓ | 1 |
| 采购五十万以上要不要采购委员会会签 | auth.procurement_committee_for_large | ✓ | 1 |
| 公司公章合同章用印要不要合规法律部审核 | auth.legal_for_high_risk_seal | ✓ | 1 |
| 子公司重大事项要不要公司领导批示 | eoa140.company_leader_instruction | ✓ | 1 |
| 对口部门会签要不要多人并行处理 | eoa140.countersign_parallel | ✓ | 1 |
| 流程必须有起草环节和通向结束的路径吗 | design.has_draft_and_end | ✓ | 1 |
| 申请人所属部门这种自动带出字段要设成只读吗 | design.autofill_fields_readonly | ✓ | 1 |
