# ProcessDefinition 标准化输出 Schema

本文档说明 LangGraph 工作流中 `ProcessExtractionAgent` 需要填写的标准化流程定义。

代码来源：`data/schema/process_schema.py`

工作流位置：

```text
source_context_builder
  -> process_extraction_agent
  -> structural_validator
  -> business_validation_agent
  -> workflow_design_output_writer
```

`process_extraction_agent` 的核心输出是 `ProcessDefinition`。后续 `business_validation_agent` 生成的待确认项、提示项和校验问题不属于 `ProcessDefinition` 本体。

## 顶层结构

```json
{
  "meta": {},
  "form_fields": [],
  "flow_nodes": [],
  "attachments": null,
  "roles": null
}
```

| 字段 | 类型 | 是否必填 | 说明 | 对系统影响 |
|---|---|---:|---|---|
| `meta` | `ProcessMeta` | 是 | 流程基础信息 | 展示、流程库、设计页标题 |
| `form_fields` | `FormField[]` | 是 | 表单字段配置，按展示顺序排列 | 生成表单、字段权限、运行时填报 |
| `flow_nodes` | `FlowNode[]` | 是 | 流程环节配置，按主要流转顺序排列 | 运行时路由、待办生成、流程图 |
| `attachments` | `AttachmentConfig[] \| null` | 否 | 附件配置；没有明确附件要求时填 `null` | 附件页展示、后续附件校验 |
| `roles` | `RoleConfig[] \| null` | 否 | 流程自定义角色；只使用通用组织角色时填 `null` | 角色配置展示、后续权限配置 |

## 特殊 ID 约定

| 值 | 使用位置 | 含义 |
|---|---|---|
| `draft` | `node_id`、字段环节引用、路径目标 | 起草环节 |
| `DRAFT` | `SubmitPath.target_node_id` | 退回起草 |
| `END` | `SubmitPath.target_node_id` | 流程正常结束 |
| `all` | 字段可见/必填/编辑环节、附件上传环节 | 所有环节 |

## ProcessMeta

```json
{
  "process_id": "LEAVE-001",
  "process_name": "员工请假申请",
  "version": "V1.0.0",
  "responsible_dept": "人力资源部",
  "description": "用于员工申请各类假期，经逐级审批后完成请假管理。",
  "applicant_scope": "全体在职员工",
  "entry_point": "OA系统"
}
```

| 字段 | 类型 | 是否必填 | 说明 |
|---|---|---:|---|
| `process_id` | `string` | 是 | 流程编号，如 `LEAVE-001` |
| `process_name` | `string` | 是 | 流程名称 |
| `version` | `string` | 是 | 版本号；默认 `V1.0.0` |
| `responsible_dept` | `string` | 是 | 流程责任部门 |
| `description` | `string` | 是 | 流程简要描述 |
| `applicant_scope` | `string` | 是 | 适用范围/可发起人员范围 |
| `entry_point` | `string` | 是 | 流程发起入口；默认 `OA系统` |

## FormField

```json
{
  "seq": 1,
  "field_name": "标题",
  "required_stages": ["draft"],
  "visible_stages": ["all"],
  "editable_stages": [],
  "component_type": "只读文本",
  "logic_description": "自动生成，规则：申请人姓名 + 假期类型 + '请假申请'",
  "default_value": "由系统根据申请人和假期类型自动生成",
  "options": null,
  "placeholder": null,
  "max_length": null
}
```

| 字段 | 类型 | 是否必填 | 说明 | 对系统影响 |
|---|---|---:|---|---|
| `seq` | `integer` | 是 | 字段序号 | 表单排序 |
| `field_name` | `string` | 是 | 字段展示名称 | 表单 label、字段匹配 |
| `required_stages` | `string[]` | 是 | 必填环节 `node_id` 列表；空数组表示非必填 | 提交校验 |
| `visible_stages` | `string[]` | 是 | 可见环节 `node_id` 列表；`["all"]` 表示所有环节可见 | 表单展示权限 |
| `editable_stages` | `string[]` | 是 | 可编辑环节 `node_id` 列表；空数组表示只读 | 表单编辑权限 |
| `component_type` | `ComponentType` | 是 | 字段组件类型 | 前端控件渲染 |
| `logic_description` | `string \| null` | 否 | 显示逻辑、联动逻辑、校验逻辑说明 | 设计页说明、后续规则配置 |
| `default_value` | `string \| null` | 否 | 默认值或自动生成规则 | 表单默认值说明 |
| `options` | `string[] \| null` | 否 | 下拉/单选/多选选项 | 选项控件渲染 |
| `placeholder` | `string \| null` | 否 | 输入框提示文字 | 表单提示 |
| `max_length` | `integer \| null` | 否 | 文本最大长度 | 输入校验 |

### ComponentType 枚举

```text
单行文本
多行文本
下拉单选
下拉多选
单选按钮
日期组件
日期时间组件
数字
员工选择
部门选择
只读文本
文号
文字链接
附件
```

## FlowNode

```json
{
  "node_id": "dept_supervisor",
  "node_name": "部门主管审批",
  "is_draft": false,
  "handler": {},
  "opinion": {},
  "opinion_label": "申请部门意见",
  "time_limit_days": null,
  "submit_paths": []
}
```

| 字段 | 类型 | 是否必填 | 说明 | 对系统影响 |
|---|---|---:|---|---|
| `node_id` | `string` | 是 | 环节唯一标识；使用英文/下划线 | 路径引用、待办节点 |
| `node_name` | `string` | 是 | 环节中文名称 | 页面展示、流程图 |
| `is_draft` | `boolean` | 是 | 是否起草环节 | 起草页/审批页交互差异 |
| `handler` | `HandlerConfig \| null` | 否 | 处理人配置；起草环节通常为 `null` | 运行时解析处理人 |
| `opinion` | `OpinionConfig \| null` | 否 | 意见配置；起草环节通常为 `null` | 审批意见控件 |
| `opinion_label` | `string \| null` | 否 | 意见域名称，如 `申请部门意见` | 审批区展示 |
| `time_limit_days` | `integer \| null` | 否 | 限时处理天数；未明确时填 `null` | 处理期限提示 |
| `submit_paths` | `SubmitPath[]` | 是 | 当前环节可提交路径 | 路由计算、流程图边 |

## HandlerConfig

```json
{
  "mode": "单选-单人处理",
  "source": "本部门",
  "role": "部门主管",
  "source_field": null
}
```

| 字段 | 类型 | 是否必填 | 说明 |
|---|---|---:|---|
| `mode` | `HandlerMode` | 是 | 处理人选择方式 |
| `source` | `HandlerSource` | 是 | 处理人部门/组织来源 |
| `role` | `string` | 是 | 处理角色，如 `部门主管`、`一级主/辅负责人` |
| `source_field` | `string \| null` | 否 | 当 `source=表单字段指定` 时，指定从哪个字段取处理人 |

### HandlerMode 枚举

```text
单选-单人处理
多选-并行处理
多选-抢办
全选-并行处理
全选-抢办
```

### HandlerSource 枚举

```text
本部门
本公司
本条线
特定部门
表单字段指定
不指定
```

## OpinionConfig

```json
{
  "conclusive_required": true,
  "detail_required": false,
  "conclusive_options": ["同意", "不同意"]
}
```

| 字段 | 类型 | 是否必填 | 说明 |
|---|---|---:|---|
| `conclusive_required` | `boolean` | 是 | 是否需要结论性意见 |
| `detail_required` | `boolean` | 是 | 意见详情文本是否必填 |
| `conclusive_options` | `string[]` | 是 | 结论性意见选项，默认 `["同意", "不同意"]` |

## SubmitPath

```json
{
  "path_name": "送部门领导审批",
  "condition": "结论性意见=同意",
  "target_node_id": "dept_leader"
}
```

| 字段 | 类型 | 是否必填 | 说明 | 对系统影响 |
|---|---|---:|---|---|
| `path_name` | `string` | 是 | 路径显示名称 | 提交路径按钮/选项 |
| `condition` | `string \| null` | 否 | 路径触发条件；无条件填 `null` | 路由可用性判断 |
| `target_node_id` | `string` | 是 | 目标环节 ID；可为 `END` 或 `DRAFT` | 生成下一待办或结束流程 |

常见条件写法：

```text
结论性意见=同意
结论性意见=不同意
结论性意见=同意 且 请假天数＞3
结论性意见=同意 且 报销金额≥5万
上游环节退回时
```

## AttachmentConfig

```json
{
  "attachment_type": "请假证明材料",
  "upload_stages": ["draft"],
  "required_stages": ["draft"]
}
```

| 字段 | 类型 | 是否必填 | 说明 |
|---|---|---:|---|
| `attachment_type` | `string` | 是 | 附件类型名称 |
| `upload_stages` | `string[]` | 是 | 支持上传的环节 `node_id` 列表；`["all"]` 表示所有环节 |
| `required_stages` | `string[]` | 是 | 必传环节 `node_id` 列表；空数组表示非必传 |

填写规则：

- source 没有明确附件要求时，顶层 `attachments=null`。
- 如果只提到“附件”但没有明确类型、环节或必传规则，优先在 business validation 中生成待确认项，不要编造完整附件配置。

## RoleConfig

```json
{
  "role_name": "请假流程管理员",
  "role_type": "业务角色",
  "members": ["李梅", "王强"],
  "department": "人力资源部"
}
```

| 字段 | 类型 | 是否必填 | 说明 |
|---|---|---:|---|
| `role_name` | `string` | 是 | 流程自定义角色名称 |
| `role_type` | `RoleType` | 是 | 角色类型 |
| `members` | `string[]` | 是 | 挂接人员列表，姓名或工号 |
| `department` | `string \| null` | 否 | 角色所属部门 |

### RoleType 枚举

```text
流程角色
业务角色
```

填写规则：

- `部门主管`、`一级主/辅负责人`、`部门总经理` 等组织通用角色应放在 `FlowNode.handler.role`，不放在 `roles`。
- `roles` 只放流程专属角色，例如“请假流程管理员”“费用报销流程管理员”。
- source 未要求单独角色清单时，顶层 `roles=null`。

## Agent 填写规则

1. 只根据 source material 填写，不要编造 source 未出现的字段、附件、角色或处理期限。
2. 字段名、环节名、路径名使用中文业务名称；`node_id` 使用英文/下划线。
3. 起草环节固定为 `node_id="draft"`、`node_name="起草"`、`is_draft=true`。
4. 退回起草路径统一为 `target_node_id="DRAFT"`。
5. 流程结束路径统一为 `target_node_id="END"`。
6. 审批环节意见条件统一写为 `结论性意见=同意` 或 `结论性意见=不同意`。
7. 不明确但可能重要的问题，不要硬填；交给 `business_validation_agent` 输出待确认项。
8. 线下流转样例只能作为线上流程设计参考，不作为主要依据；线上流程应优先采用制度、需求说明、会议确认和明确拍板内容。

## 完整示例片段

```json
{
  "meta": {
    "process_id": "LEAVE-001",
    "process_name": "员工请假申请",
    "version": "V1.0.0",
    "responsible_dept": "人力资源部",
    "description": "用于员工申请各类假期，经部门主管、部门领导逐级审批，3天以上假期须报部门总经理审批。",
    "applicant_scope": "全体在职员工",
    "entry_point": "OA系统"
  },
  "form_fields": [
    {
      "seq": 1,
      "field_name": "标题",
      "required_stages": ["draft"],
      "visible_stages": ["all"],
      "editable_stages": [],
      "component_type": "只读文本",
      "logic_description": "自动生成，规则：申请人姓名 + 假期类型 + '请假申请'",
      "default_value": "由系统根据申请人和假期类型自动生成",
      "options": null,
      "placeholder": null,
      "max_length": null
    }
  ],
  "flow_nodes": [
    {
      "node_id": "draft",
      "node_name": "起草",
      "is_draft": true,
      "handler": null,
      "opinion": null,
      "opinion_label": null,
      "time_limit_days": null,
      "submit_paths": [
        {
          "path_name": "送部门主管审批",
          "condition": null,
          "target_node_id": "dept_supervisor"
        }
      ]
    }
  ],
  "attachments": null,
  "roles": null
}
```
