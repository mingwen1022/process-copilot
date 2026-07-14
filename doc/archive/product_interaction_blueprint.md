# 产品交互逻辑蓝图 v0.1

日期：2026-06-08

本文档用于重新定义流程 AI 产品的整体交互逻辑、角色权限、页面结构和核心链路。当前阶段先作为产品设计蓝图，不包含具体前端实现方案。

## 1. 产品定位

本产品不应被定义为单纯的审批系统，而应定义为：

> 企业工作流设计与治理平台，支持人工审批流、AI Agent 工作流、人机协同工作流。

第一阶段以审批流为主，因为审批流天然包含企业工作流的核心治理要素：

- 表单
- 任务
- 权限
- 流转
- 状态
- 审计
- 责任人
- 组织和角色解析

后续 Agentic workflow 应作为新的流程类型接入同一套平台，而不是另起一套产品。

## 2. 当前产品基础

当前仓库已经具备以下基础能力：

| 能力 | 当前状态 | 相关位置 |
| --- | --- | --- |
| 流程抽取 | 从 raw source 抽取标准流程定义 | `app/agents/process_extraction.py` |
| 标准流程模型 | 定义流程元信息、表单字段、环节、路径、角色、附件 | `data/schema/process_schema.py` |
| 可读副产物生成 | 输出 JSON、Excel、draw.io 等视图和导出物 | `app/agents/artifact_generation.py` |
| 运行时引擎 | 支持发起实例、生成待办、审批流转、退回、完成 | `app/runtime/engine.py` |
| SQLite 存储 | 保存流程定义、实例、任务、审计日志；后续应承载版本化结构化流程数据 | `app/runtime/store.py` |
| API 服务 | 暴露流程、用户、组织、实例、任务接口 | `app/api/server.py` |
| 组织知识 | 条线、部门、岗位、主岗、兼岗、角色别名解析 | `data/org/org_seed.json` |
| 当前前端 Demo | 产品入口、设计台、流程库、用户工作台、组织架构 | `app/static/index.html` |

当前已有 4 个标准流程：

| 流程 | 负责部门 | 字段数 | 环节数 | 路径数 |
| --- | --- | ---: | ---: | ---: |
| 员工请假申请 | 人力资源部 | 10 | 4 | 9 |
| 员工费用报销申请 | 财务部 | 9 | 6 | 12 |
| 用印申请 | 行政管理部 | 9 | 7 | 13 |
| 采购申请 | 采购管理中心 | 10 | 6 | 13 |

### 2.1 核心产物与副产物

成熟系统里，流程设计 Agent 的核心输出不应是 JSON、Excel 或 draw.io 文件，而应是：

> 可运行、可校验、可版本化、可审计的结构化流程数据。

推荐理解为：

```text
raw source
  -> AI 抽取 / 人工确认
  -> 结构化 Workflow Definition
  -> 数据库存储
  -> 运行时执行
  -> 按需生成 JSON / Excel / 流程图 / 文档
```

不同产物的定位：

| 类型 | 定位 |
| --- | --- |
| raw source | 输入材料和证据来源 |
| 数据库结构化字段 | 核心产物，系统 source of truth |
| JSON | API 传输、调试、导入导出、版本检查或快照格式之一 |
| Excel | 业务人员审阅、线下沟通、归档材料 |
| draw.io / Mermaid / 流程图 | 基于结构化节点和边生成的可视化视图 |
| 文档 / PDF | 汇报、审计、交付说明和制度附件 |

因此，正式产品应避免把文件反向当作真实流程：

```text
不推荐：Excel / draw.io / JSON 文件 -> 代表真实流程
推荐：结构化流程数据 -> 生成 Excel / draw.io / JSON / 文档
```

流程图仍然是非常重要的用户视图，但它的地位是结构化流程数据的可视化表达，而不是流程的最终 source of truth。

## 3. 核心产品判断

当前 Demo 的导航结构是：

- 流程设计台
- 流程库
- 用户工作台
- 组织架构

这更适合项目 Demo，不适合作为正式产品的信息架构。原因是它按功能模块切分，而真实用户是按角色和任务进入系统。

正式产品应改为三类工作区：

```text
我的工作台
流程管理台
系统管理台
```

对应三类用户：

```text
普通用户
流程 owner / 设计者
流程系统管理员
```

核心原则：

> 用户做事，owner 管流程，管理员管系统。

## 4. 用户角色与权限

### 4.1 普通用户

普通用户是只参与业务流程的人。对他们来说，系统里只有两个核心操作：

- 发起流程
- 审批或处理流程

普通用户不需要理解流程定义、组织配置、角色解析规则、版本管理等复杂概念。

| 能看到 | 能操作 | 不应看到或操作 |
| --- | --- | --- |
| 自己的待办 | 发起流程 | 全量流程定义 |
| 自己发起的实例 | 保存草稿 | 其他人的实例 |
| 自己处理过的历史 | 提交审批意见 | 流程设计入口 |
| 自己可发起的流程 | 退回、同意、不同意、转交、加签 | 组织架构配置 |
| 与自己相关的流转轨迹 | 查看实例状态 | 权限配置 |

### 4.2 流程 owner / 设计者

流程 owner 是某个或某些流程的业务责任人。他们负责流程是否正确、可运行、可持续优化。

owner 的权限边界应基于流程归属，而不是全局管理权限。

| 能看到 | 能操作 | 不应操作 |
| --- | --- | --- |
| 自己负责的流程 | 新建流程草稿 | 修改全局组织架构 |
| 自己流程的版本 | 上传 source | 修改别人的流程 |
| 自己流程的运行实例 | AI 生成流程草稿 | 配置系统级权限 |
| 自己流程的测试结果 | 编辑字段、节点、路径、角色 | 直接绕过发布治理 |
| 自己流程的运行分析 | 测试运行、提交发布、申请下线 | 处理非自己流程的异常 |

### 4.3 系统管理员

系统管理员负责平台级治理，不负责日常审批，也不应替业务 owner 设计所有业务细节。

| 能看到 | 能操作 |
| --- | --- |
| 全部流程 | 分配流程 owner |
| 全部组织架构 | 配置部门、岗位、人员、兼任 |
| 全部权限 | 配置用户组和角色 |
| 全部运行实例 | 处理异常实例 |
| 全部审计日志 | 查询、导出、追责 |
| 全部规则知识库 | 上传、启用、停用制度和授权矩阵 |
| 全部角色解析规则 | 测试和发布角色解析配置 |

## 5. 总体信息架构

### 5.1 主导航

正式产品建议使用以下主导航：

```text
我的工作台
  - 待办
  - 我发起的
  - 草稿
  - 已办 / 历史
  - 发起流程

流程管理台
  - 我的流程
  - 流程设计
  - 版本管理
  - 测试运行
  - 发布审核
  - 运行分析

系统管理台
  - 全部流程
  - 组织架构
  - 用户与权限
  - 角色解析规则
  - 规则知识库
  - 运行治理 / 审计
```

### 5.2 权限展示规则

用户登录后看到的主导航应由权限决定：

| 用户类型 | 可见工作区 |
| --- | --- |
| 普通用户 | 我的工作台 |
| 流程 owner | 我的工作台、流程管理台 |
| 系统管理员 | 我的工作台、流程管理台、系统管理台 |

管理员可能同时是某些流程的 owner，也可能同时参与审批。因此工作区之间不是互斥关系，而是权限叠加。

## 6. 我的工作台

我的工作台面向所有用户，是系统最高频入口。

### 6.1 工作台首页

目标：让用户一进入系统就知道自己要处理什么。

页面内容：

- 我的待办数量
- 即将超时或已超时任务
- 我发起的进行中流程
- 常用流程入口
- 最近处理记录

主操作：

- 发起流程
- 处理待办
- 查看我发起的流程

### 6.2 我的待办

目标：聚合所有需要当前用户处理的任务。

列表字段：

- 流程名称
- 当前环节
- 发起人
- 发起时间
- 到达时间
- 超时状态
- 摘要信息
- 优先级或风险提示

筛选维度：

- 流程类型
- 到达时间
- 是否超时
- 发起部门
- 任务类型：审批、知会、补充材料、确认、AI 审核确认

主操作：

- 打开详情
- 批量处理低风险任务，后续能力
- 标记关注

### 6.3 发起流程

目标：让用户快速找到自己能发起的流程。发起流程不应只是工作台里的一个 tab，而应是独立页面，因为用户进入这里时通常还没有明确流程名称，只知道自己想完成某件事。

页面结构：

- 自然语言流程匹配入口
- 流程分类
- 流程地图 / 流程目录
- 流程详情
- 推荐流程的发起入口

流程卡片字段：

- 流程名称
- 负责部门
- 适用范围
- 预计环节或处理时长
- 材料要求
- 流程 owner
- 关联流程

主操作：

- 发起流程
- 查看流程详情
- 按分类或关键词查找流程

#### 6.3.1 流程匹配助手：Top-K + RAG

流程匹配助手的目标不是直接替用户决定唯一流程，而是帮助用户从自然语言意图中找到最接近的流程候选。

推荐机制应设计为 Top-K，而不是固定返回一个结果：

| 情况 | 推荐策略 |
| --- | --- |
| Top1 置信度很高，且 Top1 与 Top2 差距明显 | 只突出 1 个最可能流程 |
| Top1、Top2、Top3 分数接近 | 返回 2-3 个候选流程 |
| 结果都低于置信阈值 | 不强推流程，先反问澄清 |

前端交互建议：

- 默认展示最多 3 个候选。
- 第一项默认选中，并标记为“最可能”。
- 其他候选标记为“相关候选”或“后续可能”。
- “查看详情”和“发起”操作始终作用于当前选中的候选。
- 推荐原因单独展示，不和流程对象混在一段长文本里。

后续 RAG 实现建议：

```text
用户自然语言
  -> 意图抽取：动作、对象、金额、部门、材料、场景
  -> 候选召回：流程名称、适用场景、关键词、材料要求、表单字段、历史样例
  -> 权限过滤：仅保留当前用户可发起流程
  -> 重排序：规则分 + embedding 相似度 + 使用频率 + 部门/角色相关性
  -> Top-K 决策：按置信度和分差返回 1-3 个候选或反问澄清
```

RAG 的检索来源不应只依赖流程名称，应包括：

- 流程结构化定义
- 表单字段说明
- 材料要求
- 流程 owner 维护的适用场景说明
- 历史发起样例和用户选择反馈
- 制度、授权矩阵和知识库片段

### 6.4 流程填写页

目标：完成一次申请提交。

页面结构建议：

```text
顶部：流程名称、状态、保存、提交
左侧：表单字段
右侧：流程说明、预计路径、材料要求、AI 辅助
底部：保存草稿、提交
```

交互要求：

- 必填字段实时提示
- 附件材料缺失提示
- 根据字段变化动态提示可能路径
- 如果有 AI 预审，提交前给出材料完整性和风险提示
- 草稿自动保存

### 6.5 审批详情页

目标：让审批人能在一个页面完成判断和提交。

页面结构建议：

```text
左侧：申请表单和附件
中间：当前任务、审批动作、意见填写
右侧：流转轨迹、当前节点、制度依据、AI 摘要
```

审批动作：

- 同意
- 不同意
- 退回
- 转交
- 加签
- 保存意见

AI 辅助区域：

- 申请摘要
- 风险提示
- 制度匹配
- 历史相似案例，后续能力
- 推荐意见，必须明确标注为辅助

原则：

> AI 可以辅助判断，但不能在未配置自动节点的情况下替用户点击审批动作。

### 6.6 我发起的

目标：跟踪自己提交过的流程。

状态分类：

- 进行中
- 被退回
- 已完成
- 已取消
- 异常

主操作：

- 查看当前节点
- 查看处理人
- 催办，后续能力
- 撤回，按流程配置决定
- 复制发起

### 6.7 草稿

目标：管理尚未提交的申请。

主操作：

- 继续填写
- 删除草稿
- 复制草稿

### 6.8 已办 / 历史

目标：查看自己处理过或参与过的实例。

主操作：

- 查看处理记录
- 查看最终结果
- 查看自己提交的意见

## 7. 流程管理台

流程管理台面向流程 owner / 设计者。

### 7.1 我的流程

目标：让 owner 管理自己负责的流程资产。

列表字段：

- 流程名称
- 流程类型
- 负责部门
- 当前版本
- 发布状态
- 最近更新时间
- 运行实例数
- 异常数
- owner

状态：

- 草稿
- 校验通过
- 测试中
- 待发布审核
- 已发布
- 已停用
- 已归档

主操作：

- 新建流程
- 编辑草稿
- 查看详情
- 测试运行
- 提交发布
- 申请下线

### 7.2 流程详情

目标：展示某个流程的完整业务定义和运行情况。

页面结构：

```text
概览
表单字段
流程节点
处理人规则
提交路径
附件要求
版本记录
运行数据
发布记录
```

关键字段：

- 流程 ID
- 流程名称
- 流程类型
- responsible_dept
- owner
- 可发起范围
- 当前发布版本
- 最新草稿版本
- 最近校验结果

### 7.3 流程设计

目标：通过 AI 和人工编辑共同完成流程定义。

建议分 4 步：

```text
1. 输入来源
2. AI 生成草稿
3. 人工校验和编辑
4. 测试和发布
```

#### 7.3.1 输入来源

支持输入：

- 需求说明文档
- 制度文件
- 聊天记录
- 邮件记录
- 会议纪要
- 直接自然语言描述
- 现有流程版本

主操作：

- 上传文件
- 粘贴文本
- 选择基于已有流程修改
- 选择流程类型

#### 7.3.2 AI 生成草稿

AI 输出：

- 流程元信息
- 表单字段
- 节点列表
- 处理角色
- 条件分支
- 退回路径
- 附件要求
- 规则依据
- 不确定项

交互要求：

- 不确定项必须显式展示
- 不应直接保存为已发布流程
- 需要 owner 确认后进入编辑

#### 7.3.3 人工编辑

编辑区域：

- 表单字段编辑
- 节点编辑
- 提交路径编辑
- 处理人角色编辑
- 条件表达式编辑
- 附件配置
- 权限和发起范围

辅助视图：

- 流程图
- 表格配置
- JSON 预览，给高级用户
- 与上一版本差异对比

#### 7.3.4 校验

发布前必须校验：

- Schema 合法性
- 节点可达性
- END 可达性
- 条件冲突
- 退回路径完整性
- 角色是否可解析到人
- 同人连续审批风险
- 表单字段在各环节的可见/必填/可编辑是否合理
- 附件要求是否完整
- 制度和授权矩阵是否命中，后续能力

校验结果分类：

- 阻断问题：必须修复
- 风险提示：可带说明继续
- 优化建议：不阻断

### 7.4 版本管理

目标：让流程生命周期可追溯。

版本状态：

```text
草稿 -> 校验通过 -> 测试中 -> 待发布审核 -> 已发布 -> 已停用 -> 已归档
```

版本能力：

- 查看历史版本
- 对比两个版本
- 从历史版本复制新草稿
- 回滚到历史版本，需管理员或发布权限
- 查看某版本对应的运行实例

版本规则：

- 已发布版本不可直接编辑
- 修改必须生成新草稿版本
- 新实例使用当前发布版本
- 已运行实例继续绑定发起时版本
- 实例绑定的是 `workflow_version_id`，不是可变化的流程主定义
- 一个已发布版本对应一组不可变结构化记录，包含节点、边、字段、条件、处理人规则等
- 版本结构快照不要求一定是 JSON；JSON 只是可选的序列化格式之一

推荐版本模型：

```text
workflow_definition
  表示“这个流程是什么”
  例如：员工费用报销申请

workflow_version
  表示“这个流程在某个版本下长什么样”
  例如：员工费用报销申请 v1.0.0 / v1.1.0

workflow_instance
  表示“某一次具体运行”
  发起时绑定 workflow_version_id
```

实例发起时的绑定关系：

```text
instance.workflow_id = EXPENSE
instance.workflow_version_id = EXPENSE-v1.0.0
```

当 owner 后续发布 `EXPENSE-v1.1.0` 时，已经发起的 `EXPENSE-v1.0.0` 实例不应受到影响。运行中的实例必须继续按发起时版本执行。

推荐存储原则：

```text
一个 workflow_version_id 下的一组不可变结构化记录
```

这组记录可以包括：

```text
workflow_versions
workflow_nodes
workflow_edges
workflow_form_fields
workflow_handler_rules
workflow_conditions
workflow_permissions
workflow_artifact_views
workflow_validation_results
workflow_source_evidence
```

如果实现上需要，也可以在 `workflow_versions` 中额外保存一份序列化快照，例如 `definition_snapshot_json`，用于调试、diff、回放、导出和审计。但产品原则不是“必须存 JSON”，而是“每个版本必须有不可变的流程结构”。

### 7.5 测试运行

目标：在发布前模拟真实流转。

测试参数：

- 发起人
- 表单数据
- 组织上下文
- 条件分支输入
- 审批意见模拟

测试输出：

- 实际流转路径
- 每个节点处理人
- 条件命中原因
- 自动跳过原因
- 异常节点
- 最终状态

主操作：

- 运行测试
- 保存测试用例
- 从失败点回到设计页

### 7.6 发布审核

目标：控制流程上线风险。

发布策略可以分级：

| 流程风险 | 发布方式 |
| --- | --- |
| 低风险内部流程 | owner 校验通过后可发布 |
| 中风险跨部门流程 | owner 提交，管理员审核 |
| 高风险合规/财务/法务流程 | owner 提交，管理员和指定业务负责人审核 |

发布记录应包含：

- 提交人
- 审核人
- 发布时间
- 版本差异
- 校验结果
- 发布说明

### 7.7 运行分析

目标：帮助 owner 优化流程。

指标：

- 实例量
- 平均完成时长
- 各节点平均耗时
- 退回率
- 超时率
- 异常率
- 自动跳过次数
- AI 节点失败率，后续

分析视角：

- 按流程
- 按版本
- 按部门
- 按节点
- 按处理角色

## 8. 系统管理台

系统管理台面向平台管理员。

### 8.1 全部流程

目标：管理全平台流程资产。

列表字段：

- 流程名称
- 流程类型
- responsible_dept
- owner
- 当前版本
- 状态
- 实例数
- 异常数
- 最近发布人
- 最近更新时间

主操作：

- 分配 owner
- 查看流程
- 停用流程
- 回滚版本
- 转交管理权
- 查看审计

### 8.2 组织架构

目标：维护流程运行所依赖的组织基础数据。

对象：

- 条线
- 部门
- 岗位
- 用户
- 主岗
- 兼岗
- 有效期
- 上下级关系

主操作：

- 新增部门
- 调整部门归属
- 新增岗位
- 设置部门负责人
- 设置用户主岗/兼岗
- 设置失效日期
- 查看变更影响

### 8.3 用户与权限

目标：控制用户能看到什么、能操作什么。

权限类型：

- 普通用户
- 流程 owner
- 流程设计者
- 流程发布审核人
- 系统管理员
- 审计员，只读

权限粒度：

- 全局
- 按部门
- 按流程
- 按流程类型
- 按工作区

### 8.4 角色解析规则

目标：把业务角色映射到实际处理人。

示例角色：

- 起草人
- 部门主管
- 部门负责人
- 条线分管领导
- 财务负责人
- 印章管理员
- 采购委员会

规则配置应支持：

- 直接指定人员
- 指定部门岗位
- 根据发起人所在部门向上查找
- 根据表单字段指定
- 根据条线负责人解析
- 多人并行
- 抢办

测试工具：

```text
选择流程
选择发起人
选择表单数据
查看每个节点解析出的处理人
```

### 8.5 规则知识库

目标：为流程设计、流程校验和 AI 节点提供制度依据。

知识类型：

- 授权矩阵
- 组织职责
- 财务制度
- 采购制度
- 印章合同制度
- IT 变更制度
- 电子公文制度

主操作：

- 上传规则文档
- 标注业务域
- 标注适用流程
- 启用/停用
- 查看被哪些流程引用
- 查看规则冲突

### 8.6 运行治理

目标：处理 owner 无法解决的系统级异常。

异常类型：

- 找不到处理人
- 角色解析失败
- 条件无可用路径
- AI 节点执行失败
- 工具调用失败
- 实例卡住
- 任务超时
- 组织变更导致实例异常

管理员操作：

- 重派任务
- 终止实例
- 恢复实例
- 修改异常状态
- 添加处理说明

所有治理操作必须进入审计日志。

### 8.7 审计日志

审计对象：

- 流程发布
- 流程停用
- 版本回滚
- 权限变更
- 组织变更
- 角色解析规则变更
- 管理员处理实例
- AI 节点敏感工具调用

审计字段：

- 操作人
- 操作时间
- 操作对象
- 操作前
- 操作后
- 原因说明
- 关联实例或流程

## 9. 流程类型设计

产品应支持三类流程。

### 9.1 人工审批流

示例：

- 请假
- 报销
- 用印
- 采购

核心节点：

- 表单填写
- 人工审批
- 条件分支
- 并行会签
- 退回
- 归档

当前 `ProcessDefinition` 主要覆盖这一类。

### 9.2 AI Agent 工作流

示例：

- 合同审查 Agent
- 制度问答 Agent
- 流程文档生成 Agent
- 报销异常检测 Agent
- 采购材料完整性检查 Agent

核心节点：

- LLM 调用
- Agent 执行
- 工具调用
- RAG 检索
- 输出校验
- 人工确认
- 失败重试

### 9.3 人机协同工作流

示例：

```text
采购申请提交
  -> AI 检查材料完整性
  -> AI 检索采购制度和授权矩阵
  -> AI 判断风险等级
  -> 人工主管审批
  -> 采购中心受理
  -> AI 检查归档材料
  -> 流程结束
```

这类流程最符合企业实际场景：AI 负责预审、补全、分流、摘要、校验和建议，人负责关键决策。

## 10. 统一 WorkflowDefinition 抽象

建议后续从 `ProcessDefinition` 升级为更通用的 `WorkflowDefinition`。

`ProcessDefinition` 可以作为审批流的特化，而不是平台唯一流程模型。

这里的 `WorkflowDefinition` 是产品层的逻辑聚合，不一定对应单个 JSON 文件。成熟系统中，它应主要由数据库中的结构化记录组成，再按需要聚合成 API 响应、调试 JSON、Excel、流程图或文档。

建议抽象：

```text
WorkflowDefinition
  - meta
  - workflow_type
  - trigger
  - input_schema
  - state_schema
  - nodes
  - edges
  - policies
  - permissions
  - observability
```

### 10.1 Source of truth

产品的 source of truth 应是版本化结构化数据，而不是某个导出文件。

推荐层级：

```text
workflow_definitions
  - workflow_id
  - workflow_name
  - owner
  - responsible_dept

workflow_versions
  - workflow_version_id
  - workflow_id
  - version
  - status
  - published_at
  - published_by
  - structure_hash

workflow_nodes
  - workflow_version_id
  - node_id
  - node_type
  - config

workflow_edges
  - workflow_version_id
  - source_node_id
  - target_node_id
  - condition

workflow_form_fields
  - workflow_version_id
  - field_name
  - component_type
  - required_rules
  - visible_rules
  - editable_rules
```

对 Agentic workflow，可继续扩展：

```text
workflow_agent_nodes
workflow_tool_permissions
workflow_model_configs
workflow_prompt_versions
workflow_state_schemas
workflow_eval_rules
```

核心原则：

> 流程版本才是运行时绑定单位；每个版本对应一组不可变结构化流程数据，JSON、Excel、流程图和文档只是基于这个版本结构生成的视图或导出物。

### 10.2 workflow_type

```text
human_approval
agentic
hybrid
```

### 10.3 node types

```text
human_task      人工处理
approval_task   审批处理
agent_task      Agent 执行
llm_task        单次 LLM 生成
tool_task       工具/API 调用
retrieval_task  知识库检索
condition       条件判断
parallel        并行分支
join            汇聚
end             结束
```

### 10.4 Agent 节点配置

AI 工作流需要额外定义：

- Agent 名称
- Agent 职责
- 输入 schema
- 输出 schema
- model
- prompt
- tools
- tool permission
- knowledge scope
- retry policy
- timeout
- cost budget
- human approval gate
- evaluator
- fallback
- trace

### 10.5 为什么不要直接绑定 LangGraph

LangGraph 可以作为执行后端，但产品定义层不应直接等于 LangGraph。

原因：

- 产品需要长期保持中立，未来可能接 LangGraph、OpenAI Agents SDK、Temporal、内部 agent runtime 等不同后端。
- 业务用户不应直接理解 LangGraph 的实现概念。
- 同一个平台还要支持传统人工审批流，不应被某个 Agent 框架绑死。

建议：

```text
产品层：WorkflowDefinition
执行层：Runtime Adapter
  - ApprovalRuntimeAdapter
  - LangGraphRuntimeAdapter
  - HybridRuntimeAdapter
```

## 11. 核心业务链路

### 11.1 普通用户发起审批流

```text
选择流程
-> 填写表单
-> 保存草稿或提交
-> 系统解析下一处理人
-> 生成待办
-> 审批流转
-> 结束归档
```

### 11.2 普通用户处理待办

```text
进入待办
-> 查看表单、附件、上下文
-> 查看 AI 摘要和制度依据，可选
-> 填写意见
-> 系统计算可用路径
-> 提交
-> 生成下一节点任务
```

### 11.3 owner 设计流程

```text
新建流程
-> 上传 source
-> AI 生成草稿
-> owner 编辑字段、节点、路径、角色
-> 系统校验
-> 测试运行
-> 提交发布
-> 发布后运行
```

### 11.4 管理员配置组织

```text
维护部门、岗位、人员
-> 配置角色解析规则
-> 测试某流程、某发起人、某表单数据下的处理人
-> 发布组织配置
-> 生成影响分析和审计日志
```

### 11.5 混合 AI 工作流

```text
用户提交输入
-> AI 预处理
-> 规则/知识检索
-> AI 给出判断、摘要或材料补全
-> 人工确认
-> 系统动作或审批流转
-> 审计归档
```

## 12. 状态模型

### 12.1 流程定义状态

```text
DRAFT             草稿
VALIDATED         校验通过
TESTING           测试中
PENDING_RELEASE   待发布审核
PUBLISHED         已发布
DISABLED          已停用
ARCHIVED          已归档
```

### 12.2 流程实例状态

```text
DRAFT       草稿
ACTIVE      运行中
RETURNED    已退回
COMPLETED   已完成
CANCELED    已取消
FAILED      异常
SUSPENDED   已挂起
```

当前代码已有：

```text
DRAFT
ACTIVE
RETURNED
COMPLETED
CANCELED
```

后续 Agentic workflow 建议补充：

```text
FAILED
SUSPENDED
WAITING_HUMAN
WAITING_TOOL
```

### 12.3 任务状态

```text
OPEN
CLAIMED
COMPLETED
SKIPPED
CANCELED
FAILED
EXPIRED
```

当前代码已有：

```text
OPEN
COMPLETED
SKIPPED
CANCELED
```

### 12.4 AI 节点状态

```text
PENDING
RUNNING
WAITING_APPROVAL
SUCCEEDED
FAILED
RETRYING
SKIPPED
```

## 13. MVP 范围建议

第一阶段不要一次做完整 Agentic workflow 平台。建议先完成角色化审批平台，同时预留 AI 工作流扩展点。

### 13.1 MVP 应包含

- 我的工作台
  - 我的待办
  - 我发起的
  - 草稿
  - 已办
  - 发起流程
- 流程管理台
  - 我的流程
  - 流程详情
  - 流程设计草稿
  - 测试运行
- 系统管理台
  - 全部流程
  - 组织架构
  - 角色解析规则
- 流程类型
  - 先支持人工审批流
  - 数据模型预留 `workflow_type`
- AI 展示能力
  - AI 表单预审
  - AI 审批摘要
  - AI 制度匹配提示

### 13.2 MVP 不建议包含

- 完整低代码流程设计器
- 完整 BPMN 引擎
- 完整 Agent marketplace
- 多租户复杂权限
- 复杂报表 BI
- 真实外部 OA/飞书适配
- 全自动 AI 审批决策

## 14. 后续演进方向

### 14.1 阶段 1：审批流角色化重构

目标：

- 从 Demo 导航切换到正式工作区结构
- 普通用户、owner、管理员体验分离
- 支持完整发起和审批链路

### 14.2 阶段 2：流程 owner 闭环

目标：

- owner 可管理自己流程
- 支持流程版本、测试运行、发布审核
- 支持组织解析校验和流程可达性校验

### 14.3 阶段 3：AI 辅助审批

目标：

- AI 生成申请摘要
- AI 检查材料完整性
- AI 检索制度依据
- AI 给出风险提示

### 14.4 阶段 4：Agentic workflow

目标：

- 引入 `WorkflowDefinition`
- 支持 Agent 节点、LLM 节点、Tool 节点、Retrieval 节点
- 接入 LangGraph runtime adapter
- 支持 human-in-the-loop 和 durable execution

### 14.5 阶段 5：治理和分析

目标：

- 流程运行分析
- AI 节点质量评估
- 异常恢复
- 成本和工具调用审计
- 规则知识库版本管理

## 15. 后续 HTML 原型建议

下一步做纯 HTML 原型时，建议直接按以下页面结构实现，不再沿用当前 Demo 菜单。

```text
首页
  - 根据当前用户权限展示工作区入口

我的工作台
  - 待办列表
  - 审批详情页
  - 我发起的
  - 草稿
  - 已办

发起流程
  - 自然语言流程匹配
  - 流程分类
  - 流程地图
  - 流程详情
  - 发起申请单

流程管理台
  - 我的流程列表
  - 流程详情
  - 流程设计四步页
  - 校验结果页
  - 测试运行页
  - 版本管理页

系统管理台
  - 全部流程
  - 组织架构
  - 角色解析规则
  - 用户与权限
  - 运行治理
```

原型重点不是视觉炫技，而是验证：

- 三类用户能否自然找到自己的入口
- 普通用户是否只看到发起和审批
- owner 是否能完成流程设计、校验、测试、发布
- 管理员是否能管理组织、权限、角色解析和异常
- AI 工作流是否能作为未来流程类型自然接入

## 16. 参考资料

- Microsoft Power Automate Approvals: https://learn.microsoft.com/ka-ge/power-automate/get-started-approvals
- Microsoft Unified Action Center: https://www.microsoft.com/en-us/power-platform/blog/power-automate/introducing-the-unified-action-center/
- ServiceNow Process Automation: https://www.servicenow.com/docs/r/build-workflows/workflow-studio/getting-started-process-automation.html
- Camunda Web Modeler Collaboration Roles: https://docs.camunda.io/docs/8.8/components/modeler/web-modeler/collaboration/
- Power Platform Environments and Roles: https://learn.microsoft.com/en-us/power-platform/admin/environments-overview
- ProcessMaker Overview: https://docs.processmaker.com/docs/overview
- LangGraph Overview: https://docs.langchain.com/oss/python/langgraph
- LangGraph Durable Execution: https://docs.langchain.com/oss/python/langgraph/durable-execution
- LangGraph Human-in-the-loop: https://docs.langchain.com/langgraph-platform/add-human-in-the-loop
