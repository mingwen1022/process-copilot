# Slice 1 我的工作台审批闭环产品文档

版本：v0.6  
日期：2026-06-09  
设计依据：`doc/prototypes/my_workspace_prototype.html`、`doc/product/product_interaction_blueprint.md`

## 1. 设计口径

这份文档按“目标产品”重新设计，不以当前已有前后端接口为约束。现有实现只能作为后续落地时的参考或可复用资产，不能反向限制产品结构、接口命名、数据模型和页面拆分。

Slice 1 的目标是先打通普通用户的审批闭环：

```text
选择用户
  -> 我的工作台
  -> 发起流程
  -> 填写申请
  -> 提交后生成下一处理人待办
  -> 审批人进入自己的待办
  -> 查看详情、选择意见和提交路径
  -> 流程继续流转、退回或结束
```

这一刀优先解决三个问题：

1. 用户隔离：每个用户只看到自己的待办、草稿、发起记录和处理历史。
2. 起草和审批分离：起草页只保存/提交申请，审批页才有同意、不同意、退回等动作。
3. 运行闭环真实：流程实例、待办、表单、流转轨迹和操作记录都来自后端状态，不只是前端 mock。

## 2. Slice 范围

### 2.1 本 Slice 包含

- 模拟登录页：选择一个模拟用户进入工作台。
- 工作台首页：展示待办、草稿、我发起的、已办摘要和下一步推荐。
- 发起流程：从流程目录或自然语言匹配入口选择流程。
- 申请填写页：按流程表单填写申请，保存草稿或提交。
- 我的待办：只展示当前用户需要处理的任务。
- 审批详情页：查看申请内容、流程轨迹、处理意见、可提交路径。
- 我发起的：查看自己发起的流程状态。
- 草稿：继续处理未提交或被退回的申请。
- 已办/历史：查看自己处理过的任务。
- 后端领域模型和 API：围绕工作台闭环重新定义，不受当前接口命名约束。

### 2.2 本 Slice 不包含

- 不做真实登录，先保留模拟用户选择。
- 不做流程设计台的 AI 生成和版本发布。
- 不做流程 owner 管理台。
- 不做系统管理员的组织和权限配置。
- 不做真实附件上传，先保留附件元数据和占位。
- 不做完整 RAG，流程匹配助手第一版可以用关键词和规则模拟。
- 不做“流程助手发现”的真实分析能力；首页保留入口和空态/占位，MVP 不生成材料缺失、金额风险、超时建议等助手发现。
- 不接飞书、OA 或其他外部审批系统。

## 3. 用户与页面结构

Slice 1 面向“普通用户”。普通用户既可能是申请人，也可能是审批人。

页面不做成一个大工作台塞所有功能，而是按任务拆成多个页面：

```text
用户工作台入口
  -> 选择用户页
  -> 工作台首页
      -> 我的待办列表
          -> 审批任务详情页
      -> 发起流程页
          -> 申请填写页
      -> 草稿列表
          -> 起草任务详情页
      -> 我发起的列表
          -> 实例详情页
      -> 已办/历史列表
          -> 已办详情页
```

### 3.1 模拟用户

用户选择页模拟未来登录页。选择用户后，整个工作台进入该用户上下文。

用户卡片展示：

- 姓名
- 工号
- 部门
- 主岗位
- 兼任岗位
- 当前待办数
- 是否有退回草稿

用户上下文示例：

```json
{
  "user_id": "u_it_app_staff",
  "user_name": "王嘉树",
  "employee_no": "E1001",
  "primary_org": "IT条线 / 应用研发部",
  "primary_position": "普通员工",
  "concurrent_positions": []
}
```

## 4. 页面设计明细

### 4.1 工作台首页

目标：用户进入后立刻知道自己要处理什么。

页面模块参考原型：

- 摘要条：
  - 我的待办
  - 即将超时
  - 我发起的进行中
  - 草稿
- 下一步卡片：
  - 优先展示即将超时、高风险或被退回的任务
  - 点击进入对应任务详情
- 工作入口：
  - 我的待办
  - 我发起的
  - 草稿
  - 已办
  - 发起流程
- 流程助手发现：
  - MVP 只保留入口位置和空态/占位。
  - 第一版不做真实助手分析，不生成材料缺失、金额风险或即将超时建议。
  - 后续再接规则/RAG/历史案例分析后启用。

首页不直接处理审批动作，所有处理动作都进入详情页完成。

### 4.2 发起流程页

目标：用户不一定知道流程名称，也能找到应该发起的流程。

页面模块：

- 自然语言输入框：
  - 例如“我要采购几台电脑”
  - 返回 1-3 个推荐流程
- 流程分类：
  - 人事
  - 财务
  - 采购
  - 合同/用印
  - IT
- 流程目录：
  - 流程名称
  - 适用场景
  - 负责部门
  - 预计环节
  - 所需材料
  - 当前发布版本
- 流程详情侧栏：
  - 表单字段概览
  - 流程环节概览
  - 材料要求
  - 常见退回原因
  - 发起按钮

推荐结果不直接替用户发起，只帮助用户选择。用户确认后点击“发起该流程”进入申请填写页。

### 4.3 申请填写页

目标：完成一次申请的起草、保存和提交。

页面模块：

- 顶部：
  - 流程名称
  - 流程版本
  - 当前状态：草稿
  - 申请人
  - 所属部门
- 表单区：
  - 根据流程表单配置渲染字段
  - 必填字段提示
  - 选项字段、日期字段、数字字段、多行文本字段
- 材料与规则区：
  - 所需附件
  - 预计流转路径
  - 提交前检查提示
- 操作区：
  - 保存草稿
  - 提交申请

起草页规则：

- 不显示“同意/不同意”。
- 不显示审批意见必填。
- 可以展示“提交路径”，例如“送部门主管审批”。
- 保存草稿只更新表单，不推动流程。
- 提交申请才生成下一处理人待办。

### 4.4 我的待办列表

目标：展示当前用户需要处理的任务。

列表字段：

- 流程名称
- 当前环节
- 发起人
- 发起部门
- 到达时间
- 处理期限
- 优先级
- 风险等级
- 摘要
- 操作：打开

筛选：

- 流程类型
- 到达时间
- 是否超时
- 发起部门
- 风险等级
- 关键词

待办列表只展示审批/确认/补充材料等“当前需要处理”的任务，不展示其他用户任务。

起草任务不混入“我的待办”，而进入“草稿”列表。这样用户不会误以为起草也需要审批意见。

### 4.5 审批任务详情页

目标：审批人完成一次判断和提交。

页面模块参考原型：

- 顶部：
  - 返回待办
  - 流程名称
  - 当前环节
  - 状态
- 申请信息：
  - 表单字段
  - 附件占位
  - 申请说明
- 当前任务：
  - 处理人
  - 环节要求
  - 处理期限
- 处理动作：
  - 同意
  - 不同意
  - 退回
  - 保存意见草稿
- 可提交路径：
  - 根据当前意见、表单和流程规则动态返回
  - 不可用路径展示原因
- 右侧辅助：
  - 申请摘要
  - 风险提示
  - 制度依据占位
  - 流程轨迹

审批页规则：

- 只有非起草任务显示同意/不同意。
- 处理意见是否必填由当前环节配置决定。
- 用户选择意见后，系统刷新可提交路径。
- 用户只能提交 enabled 的路径。
- 提交后当前任务完成，系统创建下一环节任务，或结束流程。

### 4.6 草稿列表与起草任务详情

目标：管理未提交、保存中、被退回的申请。

草稿列表字段：

- 流程名称
- 草稿状态：未提交 / 被退回
- 最近保存时间
- 退回环节，若有
- 退回原因，若有
- 操作：继续填写

起草任务详情复用申请填写页，但需要多展示：

- 最近退回人
- 退回意见
- 退回环节
- 已流转轨迹

### 4.7 我发起的

目标：让申请人跟踪自己的流程。

列表字段：

- 流程名称
- 实例编号
- 当前状态
- 当前环节
- 当前处理人
- 发起时间
- 更新时间
- 摘要

状态：

- 草稿
- 进行中
- 被退回
- 已完成
- 已取消

主操作：

- 查看详情
- 继续处理退回
- 复制发起，后续做
- 催办，后续做

### 4.8 已办/历史

目标：查看当前用户处理过的任务。

列表字段：

- 流程名称
- 处理环节
- 处理动作
- 提交路径
- 处理意见
- 处理时间
- 实例当前状态

历史详情只读，不允许再次提交。

## 5. 领域模型

这里定义目标产品模型和 API 对象模型，不是数据库 ERD。它不是“每个小节对应一张表”的关系。

MVP 的数据库建议是：核心实体单独成表，配置类子对象可以先作为 JSON 存在核心表里。

映射规则：

- `User` 可以对应 `users` 表。
- `WorkflowDefinition` 可以对应 `workflow_definitions` 表，一条记录就是一个可运行流程版本。
- `FormField`、`WorkflowNode`、`WorkflowEdge` 第一版不需要独立表，作为 `workflow_definitions.definition_json` 里的数组保存。
- `WorkflowCase` 可以对应 `workflow_cases` 表，表单值第一版可以作为 `form_values_json` 存在 case 里。
- `WorkItem` 可以对应 `work_items` 表。
- `TimelineEvent` 可以对应 `timeline_events` 表。
- `AttachmentMeta` 可以对应 `attachment_metas` 表。

后续如果要做字段级检索、节点级统计、流程编辑器协同、版本 diff，再把 `FormField`、`WorkflowNode`、`WorkflowEdge` 拆成独立表。

### 5.1 User

```json
{
  "id": "u_it_app_staff",
  "name": "王嘉树",
  "employee_no": "E1001",
  "org_path": ["公司", "IT条线", "应用研发部"],
  "primary_position": "普通员工",
  "concurrent_positions": [],
  "roles": ["employee"]
}
```

### 5.2 WorkflowDefinition

Slice 1 MVP 中，`WorkflowDefinition` 表示一个具体可运行的流程版本。也就是说，它不是单纯的“流程主档”，而是可以直接发起、创建 case、生成 work item 的完整流程定义。

一个 `workflow_id + version` 应唯一对应一套表单字段、流程环节、路径和附件要求。流程实例发起后绑定这个版本，后续即使流程升级，也不影响已经发起的实例。

```json
{
  "id": "wfd_leave_request_v1",
  "workflow_id": "wf_leave_request",
  "code": "LEAVE-001",
  "name": "员工请假申请",
  "category": "人事",
  "owner_dept": "人力资源部",
  "status": "PUBLISHED",
  "version": "V1.0.0",
  "description": "员工请假、调休、病假等申请流程",
  "launch_scope": {
    "type": "all_employees"
  },
  "tags": ["请假", "考勤", "人事"],
  "form_fields": [],
  "nodes": [],
  "edges": [],
  "attachment_requirements": [],
  "created_at": "2026-06-09T00:00:00Z"
}
```

关键规则：

- `workflow_id` 表示同一个业务流程，例如“员工请假申请”。
- `version` 表示这个业务流程的某个版本，例如 `V1.0.0`。
- `id` 表示这个可运行定义的唯一 ID，可以由 `workflow_id + version` 生成。
- `form_fields`、`nodes`、`edges`、`attachment_requirements` 都属于这个具体版本。
- `WorkflowCase` 应绑定 `workflow_definition_id`，或至少绑定 `workflow_id + version`，确保实例运行过程使用固定版本。

### 5.3 Versioning Boundary

`WorkflowDefinition` 在 MVP 里可以直接包含版本字段和完整配置。后续如果要做流程管理台、草稿版本、发布审核和历史版本对比，可以在数据库内部再拆成两个表：

```text
Workflow            # 稳定业务主档：名称、分类、owner、当前发布版本
WorkflowVersion     # 不可变版本快照：version、form_fields、nodes、edges
```

但这个拆分是后续实现细节，不是 Slice 1 前端和 API 必须感知的两个模块。Slice 1 的产品接口优先返回完整 `WorkflowDefinition`。

### 5.4 FormField

```json
{
  "id": "leave_type",
  "label": "请假类型",
  "component": "select",
  "required": true,
  "default_value": "",
  "options": ["年假", "事假", "病假", "调休"],
  "visible_on": ["draft", "approval"],
  "editable_on": ["draft"],
  "help_text": "选择本次请假的类型"
}
```

### 5.5 WorkflowNode

```json
{
  "id": "dept_supervisor",
  "name": "部门主管审批",
  "type": "APPROVAL",
  "handler_rule": {
    "type": "role",
    "role": "部门主管",
    "scope": "initiator_department"
  },
  "requires_decision": true,
  "requires_comment": true,
  "sla_hours": 24
}
```

节点类型：

- `DRAFT`：起草
- `APPROVAL`：审批
- `CONFIRMATION`：确认/办理
- `CC`：知会
- `AUTO`：自动规则节点，后续

### 5.6 WorkflowEdge

```json
{
  "id": "edge_dept_supervisor_to_dept_leader",
  "source_node_id": "dept_supervisor",
  "target_node_id": "dept_leader",
  "label": "同意",
  "condition": {
    "decision": "APPROVE"
  }
}
```

### 5.7 WorkflowCase

流程实例。用户一旦从流程发起页进入填写并保存或提交，就生成 case。

```json
{
  "id": "case_20260609_0001",
  "workflow_definition_id": "wfd_leave_request_v1",
  "workflow_id": "wf_leave_request",
  "workflow_version": "V1.0.0",
  "title": "员工请假申请 - 王嘉树",
  "status": "DRAFT",
  "initiator_id": "u_it_app_staff",
  "current_node_id": "draft",
  "current_assignees": ["u_it_app_staff"],
  "form_values": {
    "leave_type": "年假",
    "leave_days": 2
  },
  "created_at": "2026-06-09T09:00:00Z",
  "updated_at": "2026-06-09T09:10:00Z"
}
```

Case 状态：

- `DRAFT`：草稿中
- `IN_PROGRESS`：流转中
- `RETURNED`：退回起草
- `COMPLETED`：已完成
- `CANCELED`：已取消
- `SUSPENDED`：异常挂起，后续

### 5.8 WorkflowCase 与 WorkItem 的关系

`WorkflowCase` 是一张完整的流程实例或申请单；`WorkItem` 是这张申请单在某个环节分派给某个用户处理的一条任务。

关系是：

```text
一个 WorkflowCase
  -> 可以产生多个 WorkItem
  -> 同一时间可以有 0 个、1 个或多个 OPEN WorkItem
```

举例：王嘉树发起一张请假申请。

```text
WorkflowCase: 员工请假申请 - 王嘉树

WorkItem 1: 起草任务，分派给王嘉树
  -> 王嘉树提交后，WorkItem 1 变成 COMPLETED

WorkItem 2: 部门主管审批，分派给赵明
  -> 赵明同意后，WorkItem 2 变成 COMPLETED

WorkItem 3: 部门负责人审批，分派给李婷
  -> 李婷同意后，WorkItem 3 变成 COMPLETED

Case 最终进入 COMPLETED
```

两者职责不同：

| 对象 | 关注点 | 存什么 |
| --- | --- | --- |
| `WorkflowCase` | 这张申请单整体 | 当前状态、发起人、绑定的流程版本、表单值、当前环节 |
| `WorkItem` | 某个用户要处理的一项任务 | 来源路径、当前处理人、环节、任务状态、处理意见、提交路径、下一处理人 |

工作台列表查的是 `WorkItem`：

- 我的待办：当前用户的 `OPEN` 审批/确认类 `WorkItem`
- 草稿：当前用户的 `OPEN` 起草类 `WorkItem`
- 已办：当前用户已经 `COMPLETED` 的 `WorkItem`

我发起的列表查的是 `WorkflowCase`：

- 当前用户作为 initiator 发起过的 case
- 可以看到这张申请单当前流转到哪个环节、哪个处理人

### 5.9 WorkItem

用户工作台里的任务对象。待办、草稿、已办本质上都是不同状态和类型的 work item。

`WorkItem` 字段需要区分三类人和两类路径：

- 申请发起人：这张 case 最早是谁发起的，字段用 `case_initiator_*`。
- 当前处理人：这条 work item 当前分派给谁，字段用 `current_handler_*`。
- 提交人：这条 work item 被谁提交完成，字段用 `submitted_by_*`，任务未完成时为空。
- 出口路径：这条任务提交时选择哪条路径，字段用 `selected_edge_*`。

MVP 不在 `WorkItem` 上冗余保存“从哪里来”的入口字段。因为上一环节可以通过同一个 case 下其他任务的出口字段反推：

```text
上一条任务.selected_edge_id
上一条任务.target_node_id == 当前任务.node_id
上一条任务.next_handler_ids 包含 当前任务.current_handler_id
```

如果后续出现并行审批、循环退回、同一人多次处理同一环节等复杂场景，以 `timeline_events` 作为精确审计来源，而不是在 `work_items` 上同时维护入口和出口两套关系。

```json
{
  "id": "wi_001",
  "case_id": "case_20260609_0001",
  "workflow_name": "员工请假申请",
  "node_id": "dept_supervisor",
  "node_name": "部门主管审批",
  "type": "APPROVAL",
  "status": "OPEN",
  "case_initiator_id": "u_it_app_staff",
  "case_initiator_name": "王嘉树",
  "current_handler_id": "u_it_supervisor",
  "current_handler_name": "赵明",
  "arrived_at": "2026-06-09T09:20:00Z",
  "due_at": "2026-06-10T09:20:00Z",
  "priority": "normal",
  "risk_level": "low",
  "summary": "年假 2 天，2026-06-12 至 2026-06-13",
  "submitted_by_id": null,
  "submitted_by_name": null,
  "decision": null,
  "selected_edge_id": null,
  "selected_edge_label": null,
  "target_node_id": null,
  "target_node_name": null,
  "next_handler_ids": [],
  "next_handler_names": []
}
```

任务完成后，同一条 work item 会补齐出口字段：

```json
{
  "status": "COMPLETED",
  "submitted_by_id": "u_it_supervisor",
  "submitted_by_name": "赵明",
  "decision": "APPROVE",
  "selected_edge_id": "edge_dept_supervisor_to_dept_leader",
  "selected_edge_label": "送部门负责人审批",
  "target_node_id": "dept_leader",
  "target_node_name": "部门负责人审批",
  "next_handler_ids": ["u_it_dept_leader"],
  "next_handler_names": ["李婷"]
}
```

这样一个 case 下的多条 work item 可以通过上一条任务的 `selected_edge_id`、`target_node_id`、`next_handler_ids` 串起来。更完整的审计顺序仍以 `timeline_events` 为准，`work_items` 保存的是任务维度的当前状态和提交结果快照。

WorkItem 类型：

- `DRAFT`：起草任务，进入草稿列表
- `APPROVAL`：审批任务，进入待办列表
- `CONFIRMATION`：确认/办理任务，进入待办列表
- `MATERIAL_REQUEST`：补材料任务，进入待办或草稿，视设计决定
- `CC`：知会任务，后续

WorkItem 状态：

- `OPEN`
- `COMPLETED`
- `SKIPPED`
- `CANCELED`

### 5.10 Decision

```json
{
  "decision": "APPROVE",
  "comment": "同意，申请信息完整。",
  "selected_edge_id": "edge_dept_supervisor_to_dept_leader",
  "form_updates": {}
}
```

Decision 枚举：

- `APPROVE`：同意
- `REJECT`：不同意
- `RETURN`：退回
- `SUBMIT`：起草提交
- `SAVE`：保存草稿

### 5.11 RouteOption

```json
{
  "edge_id": "edge_dept_supervisor_to_dept_leader",
  "label": "送部门负责人审批",
  "target_node_id": "dept_leader",
  "target_node_name": "部门负责人审批",
  "enabled": true,
  "disabled_reason": null,
  "requires_comment": true
}
```

### 5.12 TimelineEvent

```json
{
  "id": "evt_001",
  "case_id": "case_20260609_0001",
  "type": "WORK_ITEM_COMPLETED",
  "node_id": "dept_supervisor",
  "node_name": "部门主管审批",
  "actor_id": "u_it_supervisor",
  "actor_name": "赵明",
  "decision": "APPROVE",
  "comment": "同意。",
  "from_node_id": "dept_supervisor",
  "to_node_id": "dept_leader",
  "message": "赵明同意并送部门负责人审批",
  "created_at": "2026-06-09T10:00:00Z"
}
```

### 5.13 AttachmentMeta

第一版只做元数据，不做真实文件服务。

```json
{
  "id": "att_001",
  "case_id": "case_20260609_0001",
  "field_id": "invoice_files",
  "filename": "invoice.pdf",
  "size": 123456,
  "status": "UPLOADED",
  "uploaded_by": "u_it_app_staff",
  "uploaded_at": "2026-06-09T09:10:00Z"
}
```

## 6. API 设计

接口按目标产品重新定义，建议统一 `/api/v1`。前端不需要知道后端是否复用现有 SQLite、旧 runtime engine 或旧 schema。

### 6.1 模拟登录与用户

#### `GET /api/v1/users`

用途：用户选择页。

响应：

```json
{
  "items": [
    {
      "id": "u_it_app_staff",
      "name": "王嘉树",
      "employee_no": "E1001",
      "org_path": ["公司", "IT条线", "应用研发部"],
      "primary_position": "普通员工",
      "concurrent_positions": [],
      "open_work_item_count": 2
    }
  ]
}
```

#### `POST /api/v1/session/mock`

用途：选择模拟用户，创建前端使用的 session。

请求：

```json
{
  "user_id": "u_it_app_staff"
}
```

响应：

```json
{
  "session_id": "mock_session_001",
  "user": {}
}
```

后续真实登录时，这个接口替换为正式 auth。

### 6.2 工作台

#### `GET /api/v1/workbench/summary`

用途：工作台首页摘要。

Query：

```text
user_id=u_it_app_staff
```

响应：

```json
{
  "summary": {
    "todo_count": 3,
    "due_soon_count": 1,
    "initiated_active_count": 2,
    "draft_count": 1,
    "done_count": 8
  },
  "priority_item": {
    "id": "wi_001",
    "case_id": "case_001",
    "workflow_name": "采购申请",
    "node_name": "采购部门受理",
    "summary": "剩余 4 小时",
    "priority": "high"
  },
  "assistant_findings": []
}
```

说明：`assistant_findings` 作为后续扩展字段预留。Slice 1 MVP 中该字段默认返回空数组，前端只展示“流程助手发现”入口和空态，不实现真实发现逻辑。

#### `GET /api/v1/workbench/items`

用途：待办、草稿、已办列表统一查询。

Query：

```text
user_id=u_it_app_staff
bucket=todo|drafts|done
q=采购
status=OPEN
```

Bucket 规则：

- `todo`：当前用户的 `OPEN` 且类型不是 `DRAFT` 的 work item。
- `drafts`：当前用户的 `OPEN` 且类型是 `DRAFT` 的 work item。
- `done`：当前用户已完成的 work item 或 timeline 处理记录。

响应：

```json
{
  "items": [
    {
      "id": "wi_001",
      "case_id": "case_001",
      "workflow_name": "员工费用报销申请",
      "node_name": "部门主管审批",
      "type": "APPROVAL",
      "status": "OPEN",
      "initiator_name": "王嘉树",
      "arrived_at": "2026-06-09T09:20:00Z",
      "due_at": null,
      "priority": "normal",
      "risk_level": "medium",
      "summary": "差旅报销 12800 元"
    }
  ],
  "total": 1
}
```

#### `GET /api/v1/workbench/initiated`

用途：我发起的。

Query：

```text
user_id=u_it_app_staff
status=DRAFT,IN_PROGRESS,RETURNED,COMPLETED
```

响应：

```json
{
  "items": [
    {
      "case_id": "case_001",
      "workflow_name": "员工请假申请",
      "case_status": "IN_PROGRESS",
      "current_node_name": "部门主管审批",
      "current_assignees": ["赵明"],
      "created_at": "2026-06-09T09:00:00Z",
      "updated_at": "2026-06-09T09:20:00Z",
      "summary": "年假 2 天"
    }
  ]
}
```

### 6.3 流程发起

#### `GET /api/v1/workflows/catalog`

用途：发起流程页目录。

Query：

```text
user_id=u_it_app_staff
category=采购
q=电脑
```

响应：

```json
{
  "items": [
    {
      "id": "wfd_procurement_v1",
      "workflow_id": "wf_procurement",
      "code": "PROC-001",
      "name": "采购申请",
      "category": "采购",
      "owner_dept": "采购管理中心",
      "description": "办公设备、服务和其他采购事项申请",
      "version": "V1.0.0",
      "estimated_steps": 6,
      "materials": ["采购需求说明", "预算信息", "供应商材料"],
      "can_launch": true
    }
  ]
}
```

#### `POST /api/v1/workflows/match`

用途：自然语言匹配流程。

请求：

```json
{
  "user_id": "u_it_app_staff",
  "query": "我要采购几台电脑",
  "top_k": 3
}
```

响应：

```json
{
  "items": [
    {
      "workflow_id": "wf_procurement",
      "workflow_name": "采购申请",
      "score": 0.91,
      "rank_label": "最可能",
      "reason": "命中采购、电脑、供应商材料等关键词"
    }
  ],
  "needs_clarification": false
}
```

#### `GET /api/v1/workflows/{workflow_id}/launch-form`

用途：进入申请填写页前加载流程表单、材料和预计路径。

响应：

```json
{
  "workflow": {},
  "version": {},
  "form_schema": {
    "fields": []
  },
  "attachment_requirements": [],
  "initial_values": {},
  "first_route_options": []
}
```

#### `POST /api/v1/cases`

用途：创建草稿 case。用户点击“发起该流程”后创建。

请求：

```json
{
  "workflow_id": "wf_leave_request",
  "initiator_id": "u_it_app_staff",
  "initial_values": {}
}
```

响应：

```json
{
  "case_id": "case_001",
  "draft_work_item_id": "wi_draft_001",
  "status": "DRAFT"
}
```

### 6.4 Case 与表单

#### `GET /api/v1/cases/{case_id}`

用途：实例详情、申请填写、审批详情共用。

响应：

```json
{
  "case": {},
  "workflow": {},
  "current_work_items": [],
  "form_schema": {},
  "form_values": {},
  "attachments": [],
  "timeline": []
}
```

#### `PATCH /api/v1/cases/{case_id}/form`

用途：保存草稿或当前环节允许编辑的表单字段，不推动流程。

请求：

```json
{
  "actor_id": "u_it_app_staff",
  "work_item_id": "wi_draft_001",
  "form_updates": {
    "leave_type": "年假",
    "leave_days": 2
  },
  "save_reason": "manual"
}
```

响应：

```json
{
  "case_id": "case_001",
  "status": "DRAFT",
  "form_values": {},
  "updated_at": "2026-06-09T09:10:00Z"
}
```

后端校验：

- actor 必须有该 case 当前表单编辑权。
- 只能更新当前节点可编辑字段。
- 必填缺失不阻止保存草稿，但提交时阻止。

#### `POST /api/v1/cases/{case_id}/submit`

用途：起草提交。语义上比 complete task 更贴合产品。

请求：

```json
{
  "actor_id": "u_it_app_staff",
  "work_item_id": "wi_draft_001",
  "edge_id": "edge_draft_to_dept_supervisor",
  "form_updates": {},
  "comment": null
}
```

响应：

```json
{
  "case_id": "case_001",
  "case_status": "IN_PROGRESS",
  "created_work_items": [
    {
      "id": "wi_002",
      "assignee_id": "u_it_supervisor",
      "node_name": "部门主管审批"
    }
  ],
  "timeline": []
}
```

### 6.5 审批处理

#### `GET /api/v1/work-items/{work_item_id}`

用途：打开待办详情。

响应：

```json
{
  "work_item": {},
  "case": {},
  "workflow": {},
  "form_schema": {},
  "form_values": {},
  "attachments": [],
  "timeline": [],
  "assist": {
    "summary": "申请信息完整，金额不高。",
    "risks": []
  }
}
```

#### `POST /api/v1/work-items/{work_item_id}/route-options`

用途：根据意见、表单和上下文计算可提交路径。

请求：

```json
{
  "actor_id": "u_it_supervisor",
  "decision": "APPROVE",
  "form_updates": {}
}
```

响应：

```json
{
  "items": [
    {
      "edge_id": "edge_dept_supervisor_to_dept_leader",
      "label": "送部门负责人审批",
      "target_node_name": "部门负责人审批",
      "enabled": true,
      "disabled_reason": null
    }
  ]
}
```

#### `POST /api/v1/work-items/{work_item_id}/complete`

用途：审批提交。

请求：

```json
{
  "actor_id": "u_it_supervisor",
  "decision": "APPROVE",
  "edge_id": "edge_dept_supervisor_to_dept_leader",
  "comment": "同意，申请信息完整。",
  "form_updates": {}
}
```

响应：

```json
{
  "case_id": "case_001",
  "case_status": "IN_PROGRESS",
  "completed_work_item_id": "wi_002",
  "created_work_items": [
    {
      "id": "wi_003",
      "assignee_id": "u_it_dept_leader",
      "node_name": "部门负责人审批"
    }
  ],
  "timeline": []
}
```

后端校验：

- `actor_id` 必须等于 `work_item.assignee_id`，或有后续授权权限。
- work item 必须是 `OPEN`。
- 起草类型不能走审批 complete，必须走 `/cases/{case_id}/submit`。
- `edge_id` 必须是当前节点可用路径。
- 当前决策和路径条件必须匹配。
- 必填意见、必填字段、附件规则必须通过。

## 7. 路由与权限规则

### 7.1 用户可见性

普通用户能看到一个 case 的条件：

- 他是发起人。
- 他是当前 open work item 的处理人。
- 他处理过该 case 的历史 work item。

不满足以上条件，不返回 case 详情。

### 7.2 待办隔离

待办列表只返回：

```text
work_item.assignee_id == current_user.id
and work_item.status == OPEN
and work_item.type in APPROVAL / CONFIRMATION / MATERIAL_REQUEST
```

草稿列表只返回：

```text
work_item.assignee_id == current_user.id
and work_item.status == OPEN
and work_item.type == DRAFT
```

### 7.3 起草与审批动作

起草任务：

- 可保存表单
- 可提交申请
- 不可选择同意/不同意
- 不需要审批意见

审批任务：

- 可选择处理意见
- 可查看可用路径
- 可提交审批
- 默认不能改起草字段，除非字段配置允许

### 7.4 退回

退回路径进入起草节点时：

- case 状态变为 `RETURNED`
- 创建起草人的 `DRAFT` work item
- 草稿详情展示退回意见和轨迹
- 再次提交后 case 状态变回 `IN_PROGRESS`

## 8. 数据库表结构设计

这部分是 Slice 1 MVP 的后台持久化设计，不要求复用当前已有表。第一版以 SQLite 为默认落地方式，但字段命名和结构尽量保持通用 SQL 语义，后续迁移到 Postgres 也不需要重做领域模型。

设计原则：

- 核心运行实体单独成表：用户、组织、流程定义、实例、任务、轨迹、附件。
- 流程配置子对象第一版先放 JSON：表单字段、流程环节、提交路径、附件要求。
- 草稿不单独成表：草稿是 case 状态 + DRAFT work item。
- 每个 case 必须绑定一个固定的 `workflow_definition_id`，避免流程升级影响已发起实例。

### 8.1 表清单

```text
users
org_units
user_positions
workflow_definitions
workflow_cases
work_items
timeline_events
attachment_metas
```

### 8.2 `users`

模拟用户基础信息。后续接真实登录后，这张表仍可作为本地用户画像和组织映射缓存。

| 字段 | 类型 | 约束 | 说明 |
| --- | --- | --- | --- |
| `id` | TEXT | PK | 用户 ID，如 `u_it_app_staff` |
| `name` | TEXT | NOT NULL | 用户姓名 |
| `employee_no` | TEXT | UNIQUE | 工号 |
| `status` | TEXT | NOT NULL | `ACTIVE` / `DISABLED` |
| `email` | TEXT | NULL | 邮箱，MVP 可为空 |
| `mobile` | TEXT | NULL | 手机号，MVP 可为空 |
| `created_at` | TEXT | NOT NULL | 创建时间 |
| `updated_at` | TEXT | NOT NULL | 更新时间 |

### 8.3 `org_units`

组织层级，用于展示组织架构和处理人解析。

| 字段 | 类型 | 约束 | 说明 |
| --- | --- | --- | --- |
| `id` | TEXT | PK | 组织 ID |
| `parent_id` | TEXT | NULL, FK -> `org_units.id` | 上级组织 |
| `name` | TEXT | NOT NULL | 组织名称 |
| `type` | TEXT | NOT NULL | `company` / `line` / `department` / `team` |
| `sort_order` | INTEGER | NOT NULL DEFAULT 0 | 展示排序 |
| `status` | TEXT | NOT NULL | `ACTIVE` / `DISABLED` |
| `created_at` | TEXT | NOT NULL | 创建时间 |
| `updated_at` | TEXT | NOT NULL | 更新时间 |

### 8.4 `user_positions`

用户岗位关系，支持主岗和兼岗。

| 字段 | 类型 | 约束 | 说明 |
| --- | --- | --- | --- |
| `id` | TEXT | PK | 岗位关系 ID |
| `user_id` | TEXT | NOT NULL, FK -> `users.id` | 用户 |
| `org_unit_id` | TEXT | NOT NULL, FK -> `org_units.id` | 所属组织 |
| `position_code` | TEXT | NOT NULL | 岗位编码，如 `dept_manager` |
| `position_name` | TEXT | NOT NULL | 岗位名称，如 `部门总经理` |
| `is_primary` | INTEGER | NOT NULL | 1=主岗，0=兼岗 |
| `is_manager` | INTEGER | NOT NULL DEFAULT 0 | 是否部门/团队负责人 |
| `role_tags_json` | TEXT | NOT NULL DEFAULT `[]` | 角色标签，如 `["部门主管"]` |
| `status` | TEXT | NOT NULL | `ACTIVE` / `DISABLED` |
| `created_at` | TEXT | NOT NULL | 创建时间 |
| `updated_at` | TEXT | NOT NULL | 更新时间 |

建议约束：

- 同一个用户必须至少有一个 `is_primary = 1` 的岗位。
- 同一个用户同一时间最多一个主岗。

### 8.5 `workflow_definitions`

可运行流程定义。Slice 1 中，一条记录就是一个具体可运行版本，包含表单字段、环节、路径和附件要求。

| 字段 | 类型 | 约束 | 说明 |
| --- | --- | --- | --- |
| `id` | TEXT | PK | 可运行定义 ID，如 `wfd_leave_request_v1` |
| `workflow_id` | TEXT | NOT NULL | 稳定业务流程 ID，如 `wf_leave_request` |
| `code` | TEXT | NOT NULL | 流程编码，如 `LEAVE-001` |
| `name` | TEXT | NOT NULL | 流程名称 |
| `category` | TEXT | NOT NULL | 分类：人事、财务、采购等 |
| `owner_dept_id` | TEXT | NULL, FK -> `org_units.id` | 负责部门 ID |
| `owner_dept_name` | TEXT | NOT NULL | 负责部门名称快照 |
| `version` | TEXT | NOT NULL | 版本号，如 `V1.0.0` |
| `status` | TEXT | NOT NULL | `DRAFT` / `PUBLISHED` / `DISABLED` / `ARCHIVED` |
| `description` | TEXT | NULL | 流程说明 |
| `launch_scope_json` | TEXT | NOT NULL | 可发起范围 |
| `definition_json` | TEXT | NOT NULL | 完整定义：`form_fields`、`nodes`、`edges`、`attachment_requirements` |
| `created_by` | TEXT | NULL, FK -> `users.id` | 创建人 |
| `created_at` | TEXT | NOT NULL | 创建时间 |
| `updated_at` | TEXT | NOT NULL | 更新时间 |
| `published_at` | TEXT | NULL | 发布时间 |

建议约束：

- `UNIQUE(workflow_id, version)`
- `definition_json` 必须包含 `form_fields`、`nodes`、`edges` 三个数组。
- `PUBLISHED` 状态的记录才能被发起。

`definition_json` 示例：

```json
{
  "form_fields": [],
  "nodes": [],
  "edges": [],
  "attachment_requirements": []
}
```

### 8.6 `workflow_cases`

流程实例。用户发起流程后创建 case，后续所有任务、轨迹和表单值都围绕 case 运行。

| 字段 | 类型 | 约束 | 说明 |
| --- | --- | --- | --- |
| `id` | TEXT | PK | case ID，如 `case_20260609_0001` |
| `case_no` | TEXT | UNIQUE | 人类可读编号，MVP 可同 `id` |
| `workflow_definition_id` | TEXT | NOT NULL, FK -> `workflow_definitions.id` | 绑定的可运行流程定义 |
| `workflow_id` | TEXT | NOT NULL | 业务流程 ID 快照 |
| `workflow_version` | TEXT | NOT NULL | 流程版本快照 |
| `title` | TEXT | NOT NULL | 实例标题 |
| `status` | TEXT | NOT NULL | `DRAFT` / `IN_PROGRESS` / `RETURNED` / `COMPLETED` / `CANCELED` |
| `initiator_id` | TEXT | NOT NULL, FK -> `users.id` | 发起人 |
| `initiator_name` | TEXT | NOT NULL | 发起人姓名快照 |
| `current_node_id` | TEXT | NULL | 当前环节 ID |
| `current_node_name` | TEXT | NULL | 当前环节名称 |
| `current_assignee_ids_json` | TEXT | NOT NULL DEFAULT `[]` | 当前处理人 ID 快照 |
| `form_values_json` | TEXT | NOT NULL DEFAULT `{}` | 当前表单值 |
| `created_at` | TEXT | NOT NULL | 创建时间 |
| `updated_at` | TEXT | NOT NULL | 更新时间 |
| `completed_at` | TEXT | NULL | 完成时间 |

建议索引：

- `idx_cases_initiator_status(initiator_id, status)`
- `idx_cases_workflow(workflow_definition_id)`

#### 8.6.1 表单值存储策略

不同流程的表单字段不同，但 Slice 1 MVP 不建议“每个流程一张单独业务表”。原因是流程字段会随版本变化，如果每个流程都建表，后续字段增删、版本兼容、运行中实例读取都会变成数据库迁移问题。

MVP 推荐：

```text
workflow_definitions.definition_json.form_fields  # 定义这个版本有哪些字段
workflow_cases.form_values_json                   # 保存这个 case 的字段值
```

示例：

```json
{
  "请假类型": "年假",
  "开始日期": "2026-06-12",
  "结束日期": "2026-06-13",
  "请假天数": 2,
  "请假事由": "家庭事务"
}
```

优点：

- 可以支持任意流程的动态表单。
- 新增流程或新增字段不需要改数据库表结构。
- case 绑定 `workflow_definition_id` 后，可以按当时版本的 `form_fields` 解释 `form_values_json`。
- 更适合当前“AI 生成流程定义”的产品方向。

限制：

- 不能方便地用 SQL 对某个业务字段做筛选、聚合和索引。
- 表单字段级审计、字段级权限统计和跨流程报表会比较弱。

后续增强方案：

1. 增加通用字段值表 `case_field_values`，但仍然不是每个流程一张表。
2. 对少数高频流程或报表场景，建立只读 view / materialized view / reporting table。
3. 只有当某个流程已经稳定、字段长期不变、并且有强 SQL 查询需求时，才考虑单独业务表。

可选增强表 `case_field_values`：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `id` | TEXT | 字段值记录 ID |
| `case_id` | TEXT | 所属 case |
| `workflow_definition_id` | TEXT | 绑定流程定义版本 |
| `field_id` | TEXT | 字段 ID |
| `field_label` | TEXT | 字段名称快照 |
| `field_type` | TEXT | 字段类型 |
| `value_text` | TEXT | 文本值 |
| `value_number` | REAL | 数字值 |
| `value_date` | TEXT | 日期值 |
| `value_json` | TEXT | 复杂值 |
| `updated_at` | TEXT | 更新时间 |

Slice 1 先只做 `form_values_json`。`case_field_values` 留作 Slice 2 或报表/检索需要时再加。

### 8.7 `work_items`

用户工作台任务。待办、草稿、已办都从这张表派生。

| 字段 | 类型 | 约束 | 说明 |
| --- | --- | --- | --- |
| `id` | TEXT | PK | work item ID |
| `case_id` | TEXT | NOT NULL, FK -> `workflow_cases.id` | 所属实例 |
| `workflow_definition_id` | TEXT | NOT NULL, FK -> `workflow_definitions.id` | 流程定义快照引用 |
| `node_id` | TEXT | NOT NULL | 环节 ID |
| `node_name` | TEXT | NOT NULL | 环节名称 |
| `type` | TEXT | NOT NULL | `DRAFT` / `APPROVAL` / `CONFIRMATION` / `MATERIAL_REQUEST` / `CC` |
| `status` | TEXT | NOT NULL | `OPEN` / `COMPLETED` / `SKIPPED` / `CANCELED` |
| `case_initiator_id` | TEXT | NOT NULL, FK -> `users.id` | 申请发起人 |
| `case_initiator_name` | TEXT | NOT NULL | 申请发起人姓名快照 |
| `current_handler_id` | TEXT | NOT NULL, FK -> `users.id` | 当前处理人 |
| `current_handler_name` | TEXT | NOT NULL | 当前处理人姓名快照 |
| `arrived_at` | TEXT | NOT NULL | 到达时间 |
| `due_at` | TEXT | NULL | 处理期限 |
| `completed_at` | TEXT | NULL | 完成时间 |
| `priority` | TEXT | NOT NULL DEFAULT `normal` | `low` / `normal` / `high` |
| `risk_level` | TEXT | NOT NULL DEFAULT `low` | `low` / `medium` / `high` |
| `summary` | TEXT | NULL | 列表摘要 |
| `submitted_by_id` | TEXT | NULL, FK -> `users.id` | 当前任务提交人，完成前为空 |
| `submitted_by_name` | TEXT | NULL | 当前任务提交人姓名快照 |
| `decision` | TEXT | NULL | 提交动作：`APPROVE` / `REJECT` / `RETURN` / `SUBMIT` |
| `selected_edge_id` | TEXT | NULL | 当前任务提交时选择的路径 ID |
| `selected_edge_label` | TEXT | NULL | 当前任务提交时选择的路径名称 |
| `target_node_id` | TEXT | NULL | 当前任务提交后的目标环节 ID |
| `target_node_name` | TEXT | NULL | 当前任务提交后的目标环节名称 |
| `next_handler_ids_json` | TEXT | NOT NULL DEFAULT `[]` | 当前任务提交后生成的下一处理人 ID |
| `next_handler_names_json` | TEXT | NOT NULL DEFAULT `[]` | 当前任务提交后生成的下一处理人姓名 |
| `comment` | TEXT | NULL | 处理意见 |
| `created_at` | TEXT | NOT NULL | 创建时间 |
| `updated_at` | TEXT | NOT NULL | 更新时间 |

建议索引：

- `idx_work_items_handler_status(current_handler_id, status)`
- `idx_work_items_case(case_id)`
- `idx_work_items_case_initiator(case_initiator_id)`

### 8.8 `timeline_events`

流转轨迹和审计日志。详情页右侧轨迹、已办历史都从这里生成。

| 字段 | 类型 | 约束 | 说明 |
| --- | --- | --- | --- |
| `id` | TEXT | PK | 事件 ID |
| `case_id` | TEXT | NOT NULL, FK -> `workflow_cases.id` | 所属实例 |
| `work_item_id` | TEXT | NULL, FK -> `work_items.id` | 关联任务 |
| `type` | TEXT | NOT NULL | `CASE_CREATED` / `FORM_SAVED` / `WORK_ITEM_CREATED` / `WORK_ITEM_COMPLETED` / `CASE_COMPLETED` 等 |
| `node_id` | TEXT | NULL | 环节 ID |
| `node_name` | TEXT | NULL | 环节名称 |
| `actor_id` | TEXT | NULL, FK -> `users.id` | 操作人 |
| `actor_name` | TEXT | NULL | 操作人姓名快照 |
| `decision` | TEXT | NULL | 处理动作 |
| `comment` | TEXT | NULL | 意见 |
| `from_node_id` | TEXT | NULL | 来源环节 |
| `to_node_id` | TEXT | NULL | 目标环节 |
| `message` | TEXT | NOT NULL | 人类可读轨迹 |
| `payload_json` | TEXT | NOT NULL DEFAULT `{}` | 扩展字段 |
| `created_at` | TEXT | NOT NULL | 创建时间 |

建议索引：

- `idx_timeline_case_created(case_id, created_at)`
- `idx_timeline_actor(actor_id)`

### 8.9 `attachment_metas`

附件元数据。Slice 1 不做真实文件服务，但先保留表结构，方便页面展示附件占位和后续接上传。

| 字段 | 类型 | 约束 | 说明 |
| --- | --- | --- | --- |
| `id` | TEXT | PK | 附件 ID |
| `case_id` | TEXT | NOT NULL, FK -> `workflow_cases.id` | 所属实例 |
| `field_id` | TEXT | NULL | 绑定的表单字段 |
| `filename` | TEXT | NOT NULL | 文件名 |
| `size` | INTEGER | NULL | 文件大小 |
| `mime_type` | TEXT | NULL | 文件类型 |
| `storage_path` | TEXT | NULL | 后续真实文件路径 |
| `status` | TEXT | NOT NULL | `UPLOADED` / `DELETED` |
| `uploaded_by` | TEXT | NOT NULL, FK -> `users.id` | 上传人 |
| `uploaded_at` | TEXT | NOT NULL | 上传时间 |

### 8.10 为什么不单独做 drafts 表

草稿不是独立业务对象，而是 case 的一种状态：

```text
workflow_case.status = DRAFT / RETURNED
work_item.type = DRAFT
```

这样“草稿、退回、再次提交、流转轨迹”在同一个 case 生命周期里，不会出现草稿表和实例表状态不一致。

## 9. 前端路由建议

可以继续使用原生 HTML/JS，也可以后续迁到 React。产品路由建议先按页面切开：

```text
/demo/workbench/login
/demo/workbench/home?user_id=...
/demo/workbench/todo?user_id=...
/demo/workbench/drafts?user_id=...
/demo/workbench/initiated?user_id=...
/demo/workbench/done?user_id=...
/demo/workbench/items/:work_item_id
/demo/workflows/launch?user_id=...
/demo/workflows/:workflow_id/start?user_id=...
/demo/cases/:case_id
```

如果当前仍使用单 HTML，可以用 query/hash 模拟这些路由，但代码结构上要按页面模块拆开，避免变回一个巨大的工作台函数。

## 10. 验收场景

### 场景 1：发起请假申请

1. 进入用户工作台入口。
2. 选择用户王嘉树。
3. 进入工作台首页。
4. 点击发起流程。
5. 选择员工请假申请。
6. 系统创建草稿 case 和起草 work item。
7. 进入申请填写页。
8. 页面不展示同意/不同意。
9. 填写请假类型、日期、天数、原因。
10. 保存草稿后刷新页面，草稿仍存在。
11. 提交申请后，王嘉树草稿列表不再显示该任务。
12. 部门主管的待办列表出现审批任务。

### 场景 2：审批人处理待办

1. 切换到部门主管用户。
2. 进入工作台首页。
3. 待办数包含刚才的请假申请。
4. 打开审批详情。
5. 表单字段默认只读。
6. 选择同意。
7. 系统返回可用提交路径。
8. 填写意见并提交。
9. 当前任务进入已办。
10. 下一处理人收到待办，或流程结束。

### 场景 3：退回起草

1. 审批人选择不同意或退回。
2. 系统计算退回路径。
3. 提交后 case 状态变为 `RETURNED`。
4. 起草人草稿列表出现退回任务。
5. 起草人打开后能看到退回意见。
6. 起草人修改后再次提交。

### 场景 4：用户隔离

1. 王嘉树发起流程并提交给主管。
2. 王嘉树在“我的待办”看不到主管审批任务。
3. 王嘉树在“我发起的”能看到实例进度。
4. 无关用户登录后看不到该待办和实例详情。
5. 主管登录后能看到并处理该待办。

### 场景 5：刷新和恢复

1. 创建 case 并提交到审批环节。
2. 刷新浏览器。
3. 重新选择审批人。
4. 审批人的待办仍存在。
5. 打开详情，表单、路径和轨迹都能恢复。

## 11. Definition of Done

Slice 1 完成标准：

- 用户工作台入口先选用户，再进入该用户隔离的工作台。
- 工作台首页、待办、草稿、我发起的、已办、发起流程、详情页都拆成清晰页面。
- 发起流程后生成草稿 case 和起草 work item。
- 起草页不显示同意/不同意，只支持保存和提交。
- 审批页显示意见、路径、表单和轨迹；辅助提示区域可保留占位，但不作为 MVP 必做智能能力。
- 待办严格按用户隔离。
- 提交后能生成下一处理人 work item。
- 退回后能回到起草人的草稿列表。
- 刷新或重启后状态可以恢复。
- 至少用请假和费用报销两个流程跑通闭环。

## 12. 与当前实现的关系

当前 repo 里的接口、运行时和 SQLite 表可以作为实现参考，但不是产品契约。

落地时建议：

1. 先按本文件定义新的 `/api/v1` 接口。
2. 后端内部可以临时适配现有 runtime engine。
3. 前端只依赖 `/api/v1`，不要直接绑死旧接口。
4. 旧 Demo 页面可以保留为历史演示，不再作为 Slice 1 的产品结构来源。
5. 当新接口跑通后，再决定是否迁移、重命名或删除旧接口。
