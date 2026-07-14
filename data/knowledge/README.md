# 流程知识库语料（合成）

本目录是"流程知识库"（模块 3 · 规则 RAG）的**源语料**——设计 agent 和分析 agent
运行时都从这里检索。所有内容为**合成**，参照真实制度/流程要素模板的形状与规则
密度撰写（真材料仅作形状参照、不入库），**不是任何真实公司制度的复刻**。

## 五个维度

| 维度 | 目录 | 谁用 | 内容 |
|---|---|---|---|
| 公司制度/业务规则 | `policies/` | 设计 agent（合规） | 授权矩阵、请假、子公司重大事项、电子公文、印章、费用等 |
| 设计规范/流程要素标准 | `design_standards/` | 设计 agent | 环节命名、组件类型、处理人方式、必填规范 |
| 组织角色知识 | `org_roles/` | 两个 agent | 角色定义、审批权限、回避要求（文本；结构化 org 在 `data/org/`） |
| 流程范例/最佳实践 | `process_playbooks/` | 设计 agent | 锚案例及同类流程的设计要点 |
| 运营基准/指标口径 | `ops_baselines/` | 分析 agent | SLA 标准、正常时长基准，供诊断归因引用 |

## 锚定

可被**合规校验与评测**的规则锚定到真实案例：
- `LEAVE-001` 员工请假申请流程（人力资源部）
- `EOA140` 子公司重大事项审批备案流程（战略发展部）

广度域（印章/费用/采购/IT变更）用 `applies_to_domains` 标域、**不绑造出来的流程号**，
只作知识库的丰富度，不冒充可评测的锚案例（避免老版本"孤儿流程引用"的问题）。

## frontmatter 约定（Phase 0 文档级）

```yaml
doc_id: <稳定 id>
title: <标题>
dimension: company_policy | design_standard | org_role | process_playbook | ops_baseline
applies_to_processes: [LEAVE-001, EOA140]   # 仅真实案例
applies_to_domains: [leave, authorization]  # 业务域
synthetic: true
```

Phase 1 会把这些文档拆成**原子规则**（rule_id + 适用条件 + 要求 + 出处），
文档本身保留作检索语料。
