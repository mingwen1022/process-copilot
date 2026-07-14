# 规则合规率评测（确定性合规检查）

- **违规检出率**：100.0%（6/6）
- 累计误报：0
- case 数：7

| case | 预期违规 | 检出 | 命中 | 漏检 | 误报 |
|---|---|---|---|---|---|
| 合规基线 | （无·合规基线） | — | — | — | — |
| 缺条线分管领导审批环节 | leave.line_leader_for_long_or_special | leave.line_leader_for_long_or_special | ✓ | — | — |
| 缺部门总经理审批环节 | leave.gm_approval_present | leave.gm_approval_present | ✓ | — | — |
| 申请人字段可编辑（非只读） | design.autofill_fields_readonly | design.autofill_fields_readonly | ✓ | — | — |
| 请假天数未起草必填 | leave.core_fields_required_at_draft | leave.core_fields_required_at_draft | ✓ | — | — |
| 缺通向流程结束的路径 | design.has_draft_and_end | design.has_draft_and_end | ✓ | — | — |
| 病假证明未按条件必传 | leave.sick_leave_certificate | leave.sick_leave_certificate | ✓ | — | — |
