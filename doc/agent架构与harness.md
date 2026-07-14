# Agent 架构与 Harness

按**功能模块**组织：一个产品模块一节。模块内如果是一条 LangGraph 管线，把整张图和图里
每个 LLM 节点的完整配置（schema/prompt/调用方式/确定性配套）都放在同一节里；模块内如果是
两个互不相干、各自独立工作的 agent，拆成两个子模块。给到**照着能复现**的粒度。

---

## 0. 总纲 + 共享 Harness（所有模块通用，只写一次）

### 0.1 一条心法
**LLM 只在"理解意图 / 组织语言"处出现；能判对错的事绝不交给它猜。** LLM 负责理解自然语言、
解析成 typed 对象、组织答复；确定性代码负责算对错/应用变更/校验/算指标/算路径/打分/diff。

### 0.2 模型工厂
`app/models/bedrock.py::create_bedrock_chat_model(**overrides)` —— 全项目唯一模型入口。
**AWS Bedrock + Claude（`ChatBedrockConverse`）**；模型 id 读 env `BEDROCK_MODEL_ID`，凭证
`AWS_BEARER_TOKEN_BEDROCK`（gitignore 的 `.env`，绝不入库）。**所有模块的所有 agent 都从这一个
工厂函数拿模型**，不各自 new 一份配置。

### 0.3 结构化输出标准三步（除运行数据分析里的"指标问答"外，全部走这个模式——它是工具调用循环，见 §3.2）
```python
structured = model.with_structured_output(Schema, method="function_calling", include_raw=True)
resp = structured.invoke([{"role":"system","content": SYS}, {"role":"user","content": USER}])
parsed = _coerce(resp)   # 从 resp["parsed"] 取；resp["parsing_error"] 有值则判失败，返回 None
```
- `method="function_calling"`：走 Bedrock tool-use 通道产 typed 对象。
- `include_raw=True`：保留原始文本，供解析失败兜底/repair。
- `_coerce()`：每个 agent 各带一份，语义一致（取 parsed，失败 None）。

### 0.4 解析失败兜底（强度分三档，各模块按需选用）
- **repair 重试**（流程初始化·抽取节点）：拿原始输出 + 报错拼 repair prompt 再问一次。
- **分批 + 单批重试**（评测·语义裁判）：大数组拆批，单批失败不拖累其它批（防"大数组被拍扁成字符串"）。
- **JSON fallback**（流程初始化·业务校验节点）：结构化解析失败切 JSON 提取。

### 0.5 降级守卫
`app/models/text.py::is_legacy_text_generator(model)` —— 模型不支持结构化输出时该步置 None，
agent 的入口方法返回 None，调用方给"模型未就绪"降级答复，不崩不假装。

---

## 一、流程初始化 LangGraph（source → 设计草稿）

**文件** `app/workflows/process_v1.py::build_graph` ｜ **触发**：新建流程页"开始 AI 初始化"、评测模式跑抽取。

这是一整条 **LangGraph 管线**，6 个节点，2 个是 LLM agent、其余确定性。图结构：

```
source_file_loader                (确定性 + vision_ocr 工具：读 source、图片转录)
   ↓
source_context_builder            (确定性：catalog + chunk + 预算截断)
   ↓  route_after_source_context_builder
   │    context_error → write_outputs（跳过抽取）
   │    否则          → process_extraction
   ↓
process_extraction_agent   ★LLM   ── 节点①：见 1.1
   ↓
structural_validator              (确定性：schema/字段引用/路径目标)
   ↓  route_after_structural_validation
   │    should_retry_extraction   → retry_extraction（回边到抽取节点）
   │    schema valid              → business_validation
   │    否则                      → write_outputs
   ↓
business_validation_agent  ★LLM   ── 节点②：见 1.2
   ↓  route_after_business_validation
   │    should_retry_extraction   → retry_extraction（回边）
   │    否则                      → write_outputs
   ↓
workflow_design_output_writer     (确定性：落草稿 + designer 消息 + 待确认项)
   ↓
END
```

- **重试回边**：结构/业务校验判定需要修复时回到抽取节点，次数由 `max_validation_retries`
  （默认 1）+ state `validation_retry_count` 控制，两个 LLM 节点会重新被调用一次。
- **进度事件**：每个节点用 `_progress_node` 包一层，`get_stream_writer()` 发 SSE
  （前端设计初始化页 6 步进度条订这个）。
- **`source_file_loader` 里的工具**：`app/tools/vision_ocr.py::build_bedrock_image_transcriber`——
  多模态 Claude 把图片 source（聊天截图/系统截图/表格图）忠实转录成文本，只转录不推断，
  理解与抽取留给下游节点①。

### 1.1 节点① `process_extraction_agent`（ProcessExtractionAgent）

**文件** `app/agents/process_extraction.py`
**定位**：乱 source → 符合 schema 的 `ProcessDefinition`。**是评测锚点**——评测跑的就是这一步的产出。
**输入**（WorkflowState）：`source_context`（上游已整理好的 source 文本）、`validation_retry_count`/`validation_feedback`（重试轮）、`out_dir`、`verbose`。
**输出**：typed `ProcessDefinition` → `state["candidate_process"]`；失败 `None` + `schema_errors=[{loc,msg,type}]`（不抛断图，交下游路由处理）。

**调用方式**：`model.with_structured_output(ProcessDefinition, method="function_calling", include_raw=True)`。
`parsed` 为 None 时走 **repair 重试**（`_repair_structured_output`，L127）：拿 `raw_output + parsing_error` 拼修复 prompt 再问一次，让模型自我改成合法 schema。降级（纯文本 provider）时走 `generate_text` + `extract_json_object` 解析。每轮 raw output / messages trace 落到 `node_log_dir(out_dir, "process_extraction_agent")`。

**System Prompt**（`_system_prompt`，L300，关键内容）：
> 你是企业 OA 流程设计 Agent，把多份零散 source material 整合为 V1 标准流程定义。
> **要求**：输出符合 ProcessDefinition schema；早晚冲突取后续正式确认/会议纪要/明确拍板；V1 只留表单字段/流程环节/提交路径/附件/角色；不生成 Excel/draw.io/Mermaid/报告；不发起补问（缺失交下游 business validation 暴露）；线下流转记录/纸质签批截图只作参考不照搬线下临时转办/纸质签字/邮件转发节点；线下操作优先判断映射为线上字段/通知/归档/附件，只有明确要求线上人工审批才新增环节；文本不嵌未转义英文双引号。
>
> **字段命名与规范化规则**：字段名忠实用 source 业务短名，不带流程名前缀、不自行同义改写（source 写"联系电话"就输出"联系电话"）；只有 source 明确提标题才输出"标题"字段；系统自动带出/只读字段输出为只读文本（`component_type=只读文本`，`required_stages=[]`）；起草填写字段 `required_stages=["draft"]`，不用 `["all"]`；系统自动算的字段 `required_stages=[]`、`editable_stages=[]`；不编造处理时限（只有 source 明确才填 `time_limit_days`）；不编造附件/角色（无依据 `attachments=null`/`roles=null`）。
>
> **审批环节与路径规范化规则**：起草固定 `node_id="draft"`、`node_name="起草"`；node_id 英文蛇形，node_name/handler.role 忠实 source 中文称呼、不自行改写；意见条件统一写"结论性意见=同意"/"结论性意见=不同意"；退回起草统一 `target_node_id="DRAFT"`、`path_name="退回起草"`；正常结束统一 `path_name="流程结束"`、`target_node_id="END"`；节点/路径只依据 source 实际描述，不套用任何固定模板。
>
> **冲突处理规则**：早晚口径冲突取后续正式会议/明确拍板；后文列为待确认但前文有明确业务口径时，V1 草稿先采用最近一次明确口径、不输出 `<UNKNOWN>`，复核交给下游 business validation 生成待确认项。
>
> 末尾拼接 `render_conventions_for_prompt()`（系统组件词表/输出规范）；纯文本降级时额外拼 `ProcessDefinition` 的 JSON Schema。

**User Prompt**（`_build_user_prompt`，L356）：`请根据以下 source material 合成标准流程定义。\n\n{source_context}`；重试轮（`validation_retry_count>0`）追加：只按 source 补齐/修正不编造，缺就留空列表/空值，优先修 validator 明确指出的问题 + 附上 `validator 反馈：{validation_feedback}`。

**确定性配套**：下游 `structural_validator`（schema 校验、字段-环节引用完整性、路径目标合法性）。抽取本身**不判对错**，对不对由下游节点 + 评测判。

### 1.2 节点② `business_validation_agent`（BusinessValidationAgent）

**文件** `app/agents/business_validation.py`
**定位**：审核候选 `ProcessDefinition` 是否充分/准确反映 source，产出阻断/提示项 + **给用户的待确认项**。不用 gold answer、不重新生成流程 JSON，只审核 + 给修复建议。
**输入**：候选 `ProcessDefinition` + source material +（如有）人审定的 demo 边界说明。

**输出 Schema**（`BusinessValidationResult`）：
```
passed: bool                                  # 无 blocking 即 true
blocking_issues:   list[BusinessValidationIssue]
warning_issues:    list[BusinessValidationIssue]
raw_missing_items: list[BusinessValidationIssue]   # source 本身缺依据
user_clarification_requests: list[UserClarificationRequest]
repair_instructions: list[str]                # 汇总给抽取节点的修复指令
summary: str
```
`BusinessValidationIssue = {issue_type, item, severity(blocking|warning), evidence_status(found_in_raw|not_found_in_raw|uncertain), message, source_hint?, repair_instruction?}`
`UserClarificationRequest = {id, question, reason, severity, issue_type, item, options[], free_text_allowed, related_items[], source_hint?}`
（列表字段有 `_parse_json_encoded_list` 前置校验器——LLM 偶发把列表整体序列化成 JSON 字符串时能救回。）

**调用方式**：`with_structured_output(BusinessValidationResult, method="function_calling", include_raw=True)`；解析失败切 **JSON fallback**。

**System Prompt**（`_system_prompt`，L419，关键内容）：
> 你是业务校验 Agent，审核候选 ProcessDefinition 是否充分/准确反映 source material。
> **边界**：不使用 gold answer；不重新生成完整流程 JSON，只做审核和给修复建议；必须基于 source 举证，不因常识或猜测判定缺失；blocking issue 只用于会导致流程草稿明显不可用的问题；人审定的"当前 demo 边界/可接受确认项"只用于判断哪些线下/高级能力暂不进 demo，不用于反推标准答案。
>
> **重点检查**：大块缺失（表单字段/流程环节/提交路径/角色/附件）；source 明确提到但候选遗漏的关键项；候选中无依据的明显编造；路径条件/审批角色/必填可见可编辑与 source 明显冲突。
>
> **证据状态**：found_in_raw（有明确依据，应退回抽取节点修复）／not_found_in_raw（source 缺依据，不应编造，进缺失报告）／uncertain（依据不清需谨慎，只在阻断质量时作为 blocking）。
>
> **blocking 判定要克制**：只有表单字段/核心环节/核心提交路径大块缺失，或候选采用明显错误且 source 有明确可修复依据时才 blocking；source 存在冲突但候选已采用可运行的明确口径→warning + 待确认项，不 blocking；后文列为待确认但前文有明确口径→候选可先采用该口径，校验只提示复核；缺审批时限/附件要求/字段长度/手机号格式等发布前优化项→通常 warning；V1 允许"单行文本+logic_description/max_length"表达手机号/电话/账号，不因未用专用组件就生成 warning；字段已在 logic_description/placeholder 说明手机号格式并设置合理长度，不再提示格式缺失。
>
> **用户确认项**（`user_clarification_requests`）：source 本身没写/写法冲突/需业务口径确认→写入；source 明确出现"待确认"/"暂未配置"/"上线前确认"/"是否允许"等未决口径，即使候选可运行也**必须**写入，不要只写进 `raw_missing_items` 或 `warning_issues`；处理期限/SLA、端侧入口权限（移动端/PC端/退回后编辑）、附件配置、加签/增加处理人动作，只要 source 标注未决就必须生成用户确认项；`raw_missing_items` 中 warning 级的处理期限/端侧权限/附件/加签动作也必须同步生成对应确认项；extraction agent 重试修复后不要丢弃 source 中仍存在的未决口径，候选可运行不代表确认项消失；每个确认项用问题形式表达并给 2~3 个建议选项、允许自由输入；同一确认项的 options 必须互斥不重复；options 只放业务口径，不放"手动输入""按建议处理"这类泛化选项；问题可以从 source 明确修复的不要写成确认项，应写入 `repair_instructions`。
>
> 末尾拼接 `render_conventions_for_prompt()`。

**User Prompt**（`_user_prompt`，L484）：候选 `ProcessDefinition` JSON + source material +（如有）人审定的 demo 边界说明，要求逐项对照 source 审核。

**确定性配套**：图路由 `route_after_business_validation`（`should_retry_extraction` → 回节点①，否则 → 写输出）。**评测侧的匹配口径**：待确认项"命中 gold"是**关键词匹配**（`app/reporting/normalization.py::clarification_terms` 优先用 gold 的 `expected_question_contains`，否则退化到写死的 5 类主题分类 + 字面全文匹配）——这是评测配置项，跟这个 agent 本身的判断质量是两回事（详见评测相关记忆）。

---

## 二、流程设计 Agent（会话式改流程）

**文件** `app/agents/design_edit_agent.py::DesignEditAgent` ｜ **形态**：单次结构化调用
**触发**：设计工作台右侧副驾对话、顶部提醒"让副驾诊断"、流程总览的批量修复。

**定位**：把用户一句中文指令（"把联系电话改成非必填""新增一个审批环节"），转成对**当前这一个流程草稿**的结构化编辑操作。是全项目"对话改结构化对象"的范式（心法：通用在交互，专用在工具）。

**输入**（`run` 方法参数）：`process: ProcessDefinition`（当前草稿）、`instruction`（本轮指令）、`conversation_context`（会话历史）、`open_clarifications: list`（未解决待确认项）、`source_context`（如有用户补充上传的 source）。

**输出 Schema**（`EditProposal`）：
```
reply: str                          # 给用户看的简短说明，不逐条复述 JSON
operations: list[EditOperation]     # 结构化编辑操作；纯问答/确认时空列表
resolved_clarification_ids: list[str]   # 本轮实质回答/确认了的待确认项 id
out_of_scope: bool                  # 超出单流程编辑范围时 true，此时 operations 必空
```
`EditOperation` 覆盖 add/update/remove × 表单字段/流程环节/提交路径/附件/角色，加一个 `update_meta`（仅限 process_name/responsible_dept/description/applicant_scope/entry_point）。

**调用方式**：`with_structured_output(EditProposal, method="function_calling", include_raw=True)`。

**System Prompt**（`_system_prompt`，L38，关键内容）：
> 你是对话式修改助手，把用户一条中文指令转换成对"当前这一个流程草稿"的结构化编辑操作。
> **范围守卫**（严格遵守）：只能编辑当前对话绑定的这一个 ProcessDefinition 草稿，不要臆造/生成新的独立流程；用户要求超出这个范围（管理其它流程、修改系统权限/组织架构、审批发布上线）时，operations 返回空列表、`out_of_scope=true`、reply 说明超出当前设计范围；只根据用户消息明确给出的信息生成操作，没提到的属性不猜测着改、能不改就不改；指令模糊到无法安全生成结构化操作时，operations 返回空列表、reply 用一句话反问需要澄清的点，不要瞎改。
>
> **可用编辑操作**（每类 add/update/remove）：表单字段、流程环节、环节的提交路径、附件配置、自定义角色；再加 `update_meta`。
>
> **编辑操作使用规则**：update 类不支持改名（field_name/node_id/path_name/role_name/attachment_type 不可变，改名用 remove 旧的+add 新的）；update 的 `updates` 对象里字段留空表示这次不改，只有用户明确要求清空某属性时才用对应的 `clear_*` 标记；新增环节/字段/路径必须给完整合法定义，不能只给部分属性；涉及新增环节的 submit_paths/handler/opinion 等参照系统词表，不发明词表之外的枚举值；用户消息是在回答/确认某个待确认项时，`resolved_clarification_ids` 带上对应 id，若这个确认会改变草稿内容同时给出编辑操作；附件"按业务条件必传"（如"病假且天数≥3天才必须上传证明"）写进 attachment 自己的 `required_condition`，不要写进其它字段的说明文字（前端界面看不到那种写法）——`required_stages` 仍表示哪些环节允许/要求上传，`required_condition` 是叠加的业务条件，二者独立。
>
> 末尾拼接 `render_conventions_for_prompt()`；reply 用简短中文说明本轮做了什么（或为什么没做），不逐条复述 JSON，不用英文双引号。

**User Prompt**（`_user_prompt`，L72）：当前流程定义 JSON + 未解决的待确认项列表（`- id=... ：question（选项：...）`）+ 对话历史（供理解上下文，不重复处理已处理过的指令）+（如有）用户上传的补充材料 + 本轮用户最新指令。

**确定性配套**（关键：LLM 只出 `operations`，怎么落地全是代码）：
1. `apply_edit_operations(process, operations)` —— 确定性应用，怎么改。
2. `_validate_definition(new_definition)` —— 改完是否合法（复用初始化管线同一套校验逻辑）。
3. `diff_process_definitions(before, after)` —— 改了什么；判断"这轮是否真的发生了变更"用 **diff**（数据是否真变了）而不是 `apply_result.applied`（操作是否执行成功——LLM 重复提出"改成同一个值"时 applied 会算成功但值没变）。
4. **合规确认门（pending edit）**：只拦"这次编辑新引入的违规"——编辑前/编辑后各查一次确定性规则、取差集，不是草稿本来就有的问题都算这次编辑的账。有未决合规确认时先弹"确认应用/放弃"，此时 `generate_draft` 不接受新指令（见会话服务 `workflow_design_service.py`）。
5. **多流程会话隔离**：会话按 `(workflow_definition_id, created_by)` 存在 `workflow_design_sessions` 表，进哪条流程加载哪条的历史，互不污染。

---

## 三、运行数据分析 Agent 组（两个互不相干的独立 agent）

这是"运行数据分析"这个产品模块下的两个 agent，**分别独立工作、互不调用**：一个是自动扫描生成诊断报告（诊断页），一个是对话式追问指标（分析问答框）。按你说的规则拆成两个子模块。

### 3.1 堵点诊断 Agent（ProcessAnalysisAgent）

**文件** `app/agents/process_analysis_agent.py::ProcessAnalysisAgent` ｜ **形态**：单次结构化调用
**触发**：分析页"生成诊断报告"按钮。

**定位**：对**确定性代码筛出的候选堵点**写"为什么慢 + 怎么改"。**分工**：`app.analytics.thresholds::detect_candidates` 已经从运行数据里用阈值确定性筛出候选堵点 + 算好 severity；LLM **只**负责归因和建议，**不碰原始日志、不重算指标、不改 severity**。

**输入**（`diagnose` 方法）：`metrics: dict`（指标快照）。内部流程：`detect_candidates(metrics, threshold_config)` 筛出候选 → （可选）`_knowledge_index` 检索相关制度/运营基准片段 → 拼 prompt 调 LLM。

**输出 Schema**：
- LLM 单次调用输出 `DiagnosisAttributionOutput{ summary: str, attributions: list[BottleneckAttribution] }`。
- `BottleneckAttribution = {bottleneck_id, root_cause, suggestion, suggested_process_edit_instruction?}`——最后一个字段：若这条建议能转成对流程定义的一条修改指令（加时限/调路由条件），用一句自然语言写出来，供反哺桥（见下）喂给"流程设计 Agent"；不适用则 null。
- 最终对外的 `DiagnosisReport{process_name, candidate_count, summary, items: list[DiagnosisItem], llm_available, knowledge_context, generated_at, report_id}`——`DiagnosisItem` 里**确定性字段**（category/node_id/severity/metric_reference）来自 thresholds，**文案字段**（root_cause/suggestion）来自 LLM，服务层合并。

**调用方式**：`with_structured_output(DiagnosisAttributionOutput, method="function_calling", include_raw=True)`。

**System Prompt**（`_system_prompt`，L76，verbatim）：
> 你是企业 OA 流程的运营分析助手。系统已经用确定性规则从流程运行数据里筛出了一批"候选堵点"，每个都带好了类别、严重度和支撑指标数字。你的职责只有两件：
> 1. 针对每个候选堵点，用一两句话解释最可能的成因（root_cause）；
> 2. 给出具体、可落地的流程优化建议（suggestion）；如果这条建议可以转成对流程定义的一条修改（比如给某环节加处理时限、调整某条路由的跳转条件、增加提醒/催办），就在 suggested_process_edit_instruction 里用一句自然语言写出来。
>
> 严格约束：
> - 只能基于给定的候选堵点和指标数字来分析，不要臆造没有给出的数据或指标。
> - 不要修改、不要复述严重度判定，也不要自己重新计算指标——那些是系统算好的。
> - attributions 里每一项的 bottleneck_id 必须与输入中的某个候选堵点 id 完全一致，且每个候选堵点都要覆盖到。
> - 若给了"参考知识（运营基准/相关制度）"，归因和建议可引用它、并注明出处（如"依据 SLA 标准，部门总经理审批时限应为2天"）；**未提供参考知识、或其与该堵点无关时，绝不要编造制度/基准引用**——没有依据就只依据指标本身分析。
> - 回复用简洁中文；不要使用英文双引号，需要引用词语时用中文引号。

**User Prompt**（`_user_prompt`，L95）：流程名称 + 候选堵点清单（每条：id｜类别｜严重度、描述、关键指标）+ 参考知识（RAG 检索到的运营基准/制度片段，标出处；为空则显式标注"未检索到相关依据，请勿编造引用"）。

**确定性配套**：`detect_candidates`（阈值筛堵点 + 算 severity，`app.analytics.thresholds`）；知识检索是 retrieve-then-read + 相关性阈值（无关就返回空，不硬凑）。**反哺桥**：`app/agents/design_feedback.py` 把这里产出的 `suggested_process_edit_instruction` 逐条喂给「流程设计 Agent」（§二），解析成结构化编辑操作、确定性 apply 成 v2 草稿——这是"AI 诊断 → AI 改流程"的桥，复用两个已有 agent，没有新造。

### 3.2 指标问答 Agent（AnalyticsQueryAgent）——全项目唯一接入产品的工具调用 agent

**文件** `app/agents/analytics_query_agent.py::AnalyticsQueryAgent` ｜ **形态**：ReAct 工具调用（`langchain.agents.create_agent`）
**触发**：分析页副驾对话框、（进程内被流程总览的 `analytics` 路由直接调用）。

**定位**：对话式指标问答，**三层取数梯度**，层级越高越贵、越需要护栏，agent 自己判断该用哪层，不由外层代码预先分类路由：

| 层 | 机制 | 何时用 | 安全性 |
|---|---|---|---|
| L1 | 指标快照（已在 system prompt 里） | 大多数常见问题（退回率、某环节耗时）——0 次工具调用，跟单次结构化输出同价 | 无风险，纯读快照 |
| L2 | `run_metric_query` 工具（typed，AdHocMetricQuery） | 快照没有、但能用"过滤+聚合+可选分组"表达的新指标 | 安全，可穷举测试，错不了 |
| L3 | `run_sql` 工具（只读 SQL + 确定性护栏） | L2 表达不了的（多表 JOIN、复杂条件组合、跨环节下钻） | 护栏管安全不管正确，见下 |

这条梯度写死在 system prompt 里（"先便宜安全的、不够再升级"），**不是**外层代码先分类再选路径——ReAct 循环本身就会在第一轮判断"要不要调工具"，快照够用时天然 0 次调用，没必要在外面再包一层分类逻辑。

**旧的单次结构化输出方法 `run()` 仍保留**（`AnalyticsQueryProposal` schema 不变，供其余不需要工具的场景/测试复用），但生产路径 `AnalyticsService.answer_question` 现在**始终**走新方法 `run_with_tools`。

**`run_with_tools` 输入**：`message`、`metrics_snapshot: dict`、`schema_ddl: str`（L3 用的表结构说明）、`tools: list`（由 service 层注入，见下）、`conversation_context`、`custom_metric_names`、`max_tool_calls`（步数上限，防跑飞）。

**输出**（`AnalyticsToolRunResult`）：
```
reply: str
tool_calls: list[ToolCallTrace]   # 过程中每一次真实工具调用，0/1/多次
```
`ToolCallTrace = {tool, args, result}`——**数字的可信来源是这里，不是 reply 的转述**；reply 只做解读，不逐条报数字（前端把 tool_calls 单独渲染成卡片）。

**调用方式**：每次调用现建一个 `create_agent(model=self.model, tools=tools, system_prompt=...)`（工具绑定的是当次请求专属的 SQLite 只读连接，不能复用旧图），`agent.invoke({"messages":[...]}, config={"recursion_limit": max_tool_calls*2+4})`；返回的 `messages` 列表里 `AIMessage.tool_calls` 与 `ToolMessage` 按 `tool_call_id` 配对，解析成 `tool_calls` trace（`_extract_tool_run_result`）。

**System Prompt**（`_tool_system_prompt`，关键内容）：
> 你是企业 OA 流程效能分析的对话助手…（快照 JSON + 已登记自定义指标 + 对话历史，跟旧版一致）
>
> 你有两个工具可用，遵循"先便宜安全、不够再升级"的顺序：
> 1. 快照里已经能直接回答的问题——不要调用任何工具，直接在最终回复里回答。
> 2. 快照答不了、但能用"过滤+聚合+可选分组"表达的新指标——调用 run_metric_query。这是类型化查询，安全、可穷举测试，优先于 run_sql。
> 3. run_metric_query 也表达不了的需求（多表 JOIN、复杂条件组合、跨环节下钻、大范围聚合）——才调用 run_sql，对给定表结构写一条只读 SELECT。
>
> 严格约束：只基于快照和工具返回的真实数据回答，不要编造数字；工具返回 {"error": ...} 时据错误改写重试或如实告诉用户查不了，不要假装查到了；最终回复不逐条罗列原始数字/表格（前端单独渲染）；超出单个流程指标分析范围——不调用任何工具，直接说明超出范围。

**两个工具（由 `AnalyticsService.answer_question` 按次请求构建并注入，agent 模块本身不知道 SQLite/CaseRecord 细节，保持解耦）**：

1. **`run_metric_query`**（`app/tools/metric_query_tool.py::build_run_metric_query_tool`）——把 `AdHocMetricQuery`（`app.analytics.query`，见上一版文档）包成 `StructuredTool`。入参 `query_json`（JSON 字符串，模式匹配 `artifact_generation` 里"复杂 Pydantic 入参走 JSON 字符串"的既有约定，因为嵌套 `list[MetricFilter]` 在 Bedrock tool schema 里不够稳）。内部 `model_validate_json` 解析失败直接返回 `{"error": ...}` 让 agent 自己改写；`persist=true` 时同步调用 `upsert_custom_metric` 落常态化指标。执行逻辑完全复用 `run_ad_hoc_query`，没有重新实现。

2. **`run_sql`**（`app/tools/sql_query_tool.py::build_run_sql_tool`）——对一个**当次请求专属**的只读 SQLite 连接执行一条 SELECT。**三层防御**：
   - **解析器守卫**（`guard_sql`，用 `sqlglot` 解析 AST，方言 `sqlite`）：单语句（防 `;` 拼接多语句）、只允许顶层 `Select`、显式封 `Insert/Update/Delete/Create/Drop/Alter/Command/Attach/Pragma/Transaction/Copy/Merge`、表引用（排除 CTE 自身别名后）必须 ⊆ `{cases, events}`。CTE 别名不误伤——`WITH tmp AS (SELECT ... FROM cases) SELECT * FROM tmp` 合法通过，但 CTE 定义体里藏的越权表（`WITH tmp AS (SELECT * FROM salaries) ...`）仍会被拒。
   - **只读连接**（`app.analytics.sql_store.open_sql_store`，`sqlite3.connect("file:...?mode=ro", uri=True)`）——就算解析器有漏网之鱼，连接本身也写不动，纵深防御第二道，`tests/test_sql_query_guard.py` 直接验证过"绕过解析器直接对连接发 DELETE 仍报 `readonly`"。
   - **行数上限 + 进度回调超时**：`fetchmany(200+1)` 判断是否截断（不注入 `LIMIT` 以免改语义）；`conn.set_progress_handler` 每 1000 条虚拟机指令检查一次墙钟时间，超 3 秒中止查询。
   - **护栏管安全，不管正确**——SQL 语法合法但语义写错（JOIN 错列）不在护栏职责内，这条边界写在模块 docstring 里，不假装万能。

**数据地基**（`app/analytics/sql_store.py::open_sql_store`，contextmanager）：复用既有的 `_case_rows`/`_event_rows`（不重新拍平一遍），每次请求现建一个临时 SQLite 文件、灌两张表、切只读连接、退出时关闭+删除临时文件——分析侧本就"不缓存、重算成本可忽略"，这张表沿用同一原则。`cases` 表**不含** `node_id`/`node_name`（列表值字段，SQL 表放不下）——按环节下钻要 `JOIN events`，逼着 SQL 走关系型思路而不是塞列表做字符串匹配。`case_attributes` 里的动态字段（如 请假类型/请假天数）会展开成表的动态列。`store.schema_ddl()` 生成人话版表结构说明喂给 system prompt。

**已知坑（真实调试踩到、已修）**：SQLite 连接默认按创建线程绑定；`create_agent` 的工具执行可能被 LangGraph/FastAPI 派发到跟建库不同的线程，直接用会报 `SQLite objects created in a thread can only be used in that same thread`。修法：`sqlite3.connect(..., check_same_thread=False)`——单请求内只有一个调用方在用这个连接，不存在真实并发竞争，禁用同线程检查是安全的。`tests/test_analytics_sql_store.py::test_connection_usable_from_a_different_thread` 是这个坑的回归测试。

**服务层出参**（`AnalyticsService.answer_question`）：把 `tool_calls` 按工具名拆成 `query_results`（`run_metric_query` 的结果，渲染成"确定统计"卡片，跟改造前的字段形状保持兼容）和 `sql_runs`（`run_sql` 的结果，渲染成"SQL 查询"卡片：折叠展示 SQL 原文 + 结果表；被护栏拦截时 `error` 字段非空，前端渲染成红色"已拦截"卡片而不是异常状态——这本身就是 demo 里"护栏在场"的镜头，不是要隐藏的失败）。

---

## 四、运维副驾 Agent

**文件** `app/copilots/ops_agent.py::OpsCopilotAgent`（决策）+ `app/copilots/ops_service.py::OpsCopilotService`（编排）｜ **形态**：单次结构化调用
**触发**："我的流程"页详情/运维大脑对话。**粒度：单个运行实例**（instance_id），不是流程级聚合。

**定位**：卡住的单据 → 确定性诊断 → LLM 决策 → 分级落地。**LLM 只判类别 + 少量参数**，代码构造 typed 动作、做合法性校验、决定要不要人工确认。

**输入**（`decide` 方法）：`context: InstanceContext`（确定性诊断结果——为什么卡）、`user_message`、`ops_rules`（检索到的适用运维/override 规则）、`conversation_context`。

**输出 Schema**（`OpsDecision`）：
```
reply: str
category: OpsCategory    # data_fix / withdraw / override_jump / override_skip / reassign / deny / escalate / no_issue / clarify
rationale: str           # override 类须引用规则
target_node_id: str | None            # override_jump 用
field_updates: list[FieldUpdate]      # data_fix 用，{field_name, field_value}，可一次多个
reassign_user_id: str | None          # reassign 用
```
`OpsDecision.route()` 把 category 映射成三条落地路径：`user_confirm`（data_fix/withdraw）｜`authorization`（override_jump/override_skip/reassign）｜`terminal`（deny/escalate/no_issue/clarify）。

**调用方式**：`with_structured_output(OpsDecision, method="function_calling", include_raw=True)`。

**System Prompt**（`_system_prompt`，L110，verbatim 关键内容）：
> 你是企业 OA 流程的运维副驾，替代人工运维团队处理"流程卡住"的求助。用户是被卡住的发起人。给你三样东西：①这张单子为什么卡的确定性诊断；②用户的诉求；③适用的运维/override 规则。
>
> 你要输出一个决策（category）。注意区分"拒绝/升级/授权"三种不同情形：
> - **data_fix**：用户要订正表单填错的值（不限于"卡住"，在办任务里发现填错也算）→ 用 field_updates 给出**要改的字段列表**（每项 field_name + field_value）。**可以一次改多个字段**：比如"结束日期填错、实际请1天"往往要同时改结束日期和请假天数，就在 field_updates 里一并给出，不要因为"要改多个"而反问。首选此项：改数据不改流程走向、风险最低（发起人点确认即可执行，你不用自己在 reply 里要求确认）。
> - **withdraw**：用户想撤回自己的单重填（发起人确认即可）。
> - **override_jump / override_skip**：确需破例跳环节，且**规则明确允许** → override_jump 给 target_node_id。这类动流程走向、属高风险，**不能你我说了算，会自动转流程负责人授权**——你只需给出方案 + 引用规则。
> - **reassign**：因选不到人（审批人解析为空）卡住、且规则允许改派 → 给 reassign_user_id（同样转授权）。
> - **deny**：用户的诉求被规则**明令禁止**（如要求跳过合规硬性环节）→ 直接拒绝。这是有明确答案的终局，不需要升级给人，你在 reply 里说清"按XX规则不能这么做"、rationale 引用该规则。
> - **escalate**：规则**没有覆盖**这个诉求、或你**拿不准**、给不出具体方案 → 升级人工接手（deny 和 escalate 的区别：deny 是"规则说不行"有答案；escalate 是"没规则/不确定"没答案）。
> - **no_issue**：没发现需要处理的异常，且用户也没提出订正诉求。

**User Prompt**（`_user_prompt`，L138）：确定性诊断上下文（为什么卡）+ 用户消息 + 适用运维规则 + 对话历史。

**确定性配套**：
1. `build_actions_from_decision(decision, context)` —— 把决策映射成 typed `InstanceAction`：`UpdateFieldValue`/`JumpToNode`/`SkipCurrentNode`/`ReassignApprover`/`Withdraw`；`_coerce_value` 按字段 `component_type` 做轻量类型转换（数字字段转 number）。
2. `apply_instance_action` —— 最终合法性兜底校验。
3. `OpsCopilotService` 按 `route()` 分三条落地：`user_confirm` → 发起人确认门（前端 confirm/discard 按钮）；`authorization` → 生成授权工单，转流程负责人 approve/reject；`terminal` → 一句话答复，无动作。

---

## 五、发起向导 Agent

**文件** `app/copilots/launch_agent.py::LaunchCopilotAgent`（推荐）+ `app/copilots/launch_service.py::LaunchCopilotService`（编排）｜ **形态**：单次结构化调用
**触发**：发起申请页副驾对话框、（进程内被流程总览的 `launch_guide` 路由直接调用）。**纯只读**，不改任何实例。

**定位**：还没发起流程的员工问"该走哪个流程、我这情况会经过哪些环节、要什么材料、符不符合"。**路径预演本身由确定性代码完成，不靠 LLM 读条件。**

**输入**（`answer` 方法）：`catalog: list[CatalogProcess]`（可选流程目录：id/名称/用途/definition）、`rules`（检索到的相关制度规则文本）、`user_message`。

**输出 Schema**（`LaunchAnswer`）：`{reply, recommended_process_id?, leave_type?, leave_days?}`。
service 层 `ask()` 再包一层：`LaunchReply{reply, recommended_process_id, recommended_process_name, path_preview}`。

**调用方式**：`with_structured_output(LaunchAnswer, method="function_calling", include_raw=True)`。

**System Prompt**（`_system_prompt`，L26，verbatim）：
> 你是企业 OA 的发起问答助手，帮还没发起流程的员工搞清楚：该走哪个流程、我这情况会经过哪些环节、需要什么材料、符不符合条件。你只答疑，不发起、不改任何东西。
>
> 给你：可选流程目录（id/名称/用途）+ 检索到的相关制度规则 + 用户的处境描述。
> 你要：
> - recommended_process_id：从目录里选最匹配的一个（必须是目录中的 id）；确实无法判断才留空。
> - 若涉及请假：从处境里抽出 leave_type（请假类型）和 leave_days（天数），供系统预演审批路径。
> - reply：用简洁中文回答。涉及"符不符合/要什么材料"时，**只依据给你的规则**，无规则依据就说"建议咨询 HR/流程负责人"，不要编造制度。不要臆造目录里没有的流程。不用英文双引号。
>
> 注意：审批会经过哪些环节由系统据流程定义确定性算出，你不用在 reply 里自己推演路径细节，说清推荐哪个流程 + 关键条件（如天数分档）+ 材料/资格即可。

**User Prompt**（`_user_prompt`，L41）：可选流程目录（`id | 名称：用途`逐行）+ 检索到的相关制度规则 + 用户处境描述。

**确定性配套（独有能力）**：`app/copilots/launch_catalog.py::preview_path(process, attributes)` —— 拿 LLM 从用户处境抽出的假设属性（如"请假天数=8"），沿 `submit_paths` 用 `condition_eval` **确定性**走一遍，算出会经过的环节序列。这是发起向导独有的能力，别的模块没有——**流程总览的 `launch_guide` 路由就是来复用这一整套（agent+preview_path）**，不是另起一套。

---

## 六、评测 · 语义裁判（非产品页面，评测工具链的一环）

**文件** `app/eval/semantic_judge.py::judge_diffs` ｜ **形态**：单次结构化调用（分批）
**触发**：评测模式初始化跑完后自动叠加（`app/eval/eval_run_service.py::_apply_semantic_judge`）。

**定位**：确定性字面比对判为"差异"的属性，交 LLM 复评"这两个值是不是同一个意思"。判同义的按抽对计入属性准确率，**折算进评测主分**（用确定性公式，不是 LLM 说了算的分数）。

**输入**（`judge_diffs`）：`diffs: list[{id, dim, location, attribute, expected, actual}]`——由 `flatten_attribute_diffs(comparison_report, meta_differences)` 从 gold vs 抽取结果的差异报告 + meta 差异一起展平出来（覆盖字段/环节/路径/附件/元信息 5 类）。

**输出 Schema**：`SemanticJudgeResult{ judgments: list[DiffJudgment] }`；`DiffJudgment = {id, equivalent: bool, reason}`。

**调用方式**：`with_structured_output(SemanticJudgeResult, method="function_calling", include_raw=True)`，**分批调用**（`_BATCH_SIZE=10`，单批失败 `_BATCH_RETRIES` 次重试）——实测大批量（30+ 条）一次性发送时 Bedrock 偶发把 `judgments` 数组整体序列化成一个 JSON 字符串塞进 tool_call 参数、导致整批解析失败，分批后单批失败不拖累其它批；某批持续失败其 id 不出现在返回字典（调用方对缺失 id 默认按"不等价"保守处理）；只有全部批次都失败才整体返回 None。

**System Prompt**（`_SYSTEM`，verbatim）：
> 你是流程定义抽取评测的语义裁判。给定若干处「模型抽取值」vs「金标准值」的差异，逐条判断两者**指向的信息是否一致**。判断从宽，只看实质信息，不抠字面：
> - 措辞/表达/详略不同但指同一件事 → 等价（equivalent=true）。例：「当前登录人」与「当前登录人姓名」指同一个值；「系统自动带出」与「自动带出」是同一机制的不同写法；一方更啰嗦但没多出新约束 → 都算等价。
> - 不要因为**写法风格、字段该放什么内容的格式偏好**（如「default_value 不该带动作描述」）就判不等价——那是格式规范，不是语义。只要两者说的是同一件事，就等价。
> - 只有真的**信息不同**才判不等价：模型漏了金标准有的关键信息、加了金标准没有的约束/条件、数值不同、逻辑/流向不同。
>
> 对每条按其 id 返回 equivalent 与一句简短 reason。只做语义判断，不改写、不臆造。拿不准时倾向等价。

**User Prompt**（`_user_prompt`）：逐条列 `[id=N] 位置：{location} · 属性：{attribute}\n  金标准：{expected!r}\n  模型抽取：{actual!r}`。

**确定性配套**：判为同义的属性从该维度的 `diff_attrs` 里扣掉，用确定性公式重算 `attribute_accuracy = (total_attrs - (D - K)) / total_attrs`（`total_attrs = matched × attr_count`，K=同义数，D=字面差异数），代入原打分公式重算该维度分与总分——折算逻辑本身是**确定性代码**（`_apply_semantic_judge`），LLM 只出"同义与否"这一个判断。CLI batch 评测基线不叠这层（保持纯确定性、可复现），只有产品页评测模式的运行会叠加。

---

## 七、流程总览编排（Orchestrator）——路由层，不是又一个业务 agent

**文件** `app/copilots/manage_service.py::ManageCopilotService` ｜ **形态**：一次分类 LLM + 确定性分发
**触发**：流程管理页副驾对话框。

**定位**：流程 owner 在流程管理页问的问题五花八门，这一层**判断该转给谁、直接调用对应的已有能力**，
**不是新造一个业务 agent，不做 agent 群聊**（子能力都是函数返回给它，不直连用户）。

**两段式**：
```
第一段（唯一一次分类 LLM 调用）：ManageCopilotAgent.classify(catalog, message, history)
   → 输出 route（六选一）+ target_workflow_id
第二段（纯代码分发，不再猜）：按 route 直接调用 §一~§七 里对应的能力
```

**分类输出 Schema**（`ManageRoute`）：`route ∈ {catalog, batch_fix, flow_structure, analytics, launch_guide, general}`、`target_workflow_id: str | None`、`general_reply: str`（仅 general 用）。

**分类调用方式**：`with_structured_output(ManageRoute, method="function_calling", include_raw=True)`。

**分类 System Prompt**（`_classify_system_prompt`，关键内容）：
> 你是企业 OA「流程管理」总览页的路由助手。owner 的问题分两种视角：①管理/设计视角——批量修复待处理项、某条流程整体是怎么设计的、某条流程运行效能如何；②员工/申请人视角（owner 常被员工问、于是替员工来问）——该走哪个流程、我这情况（给了具体天数/类型）会经过哪些环节、需要什么材料、符不符合条件。也可能是范围外的问题（如运维单据处理——目前没有流程级入口，归 general）。
>
> 判断关键：问"这条流程整体结构/分支逻辑"→ flow_structure；问"给了具体取值、我会怎么流转/符不符合"→ launch_guide（它能按取值确定性预演路径）。
>
> 给你一份流程目录（workflow_id/名称/是否有待处理项/是否支持效能问答）。若问题针对某条具体流程，从目录里选出它的 workflow_id 填 target_workflow_id（必须是目录里存在的 id，选不出就留空）。route=general 时用 general_reply 给一句范围说明，别编造你能做到的事。

**六条路由 · 信息源 · 复用了谁**：
| route | 信息源 | 怎么答 |
|---|---|---|
| `catalog` | `workflow_definitions` + `workflow_design_sessions`（SQLite，两次确定性查询） | 纯 Python 格式化，**零额外 LLM 调用** |
| `batch_fix` | 同上（哪些 `issue_count>0`） | 后端只报数；**前端**对每条有问题的流程复用 §二「流程设计 Agent」的诊断→pending edit→确认链路，逐条流程循环 |
| `flow_structure` | 该流程的 `definition_json`（完整 ProcessDefinition，从 `Slice1Service.workflow_definition_detail` 读） | orchestrator **自己再调一次裸模型**（`model.invoke`，非 §二 的 agent），用 owner 视角的讲解 prompt（见下） |
| `analytics` | `data/analytics/{process}/` 事件日志 | **进程内直调** §5.2「指标问答 Agent」的 `AnalyticsService.answer_question(message, history)`，原样透传 |
| `launch_guide` | 发起向导的目录 + preview_path | **进程内直调** §七「发起向导 Agent」的 `LaunchCopilotService.ask(message)`，透传 reply + 拼接"预演路径（确定性）：..." |
| `general` | 无 | 分类那次 LLM 调用里的 `general_reply` 字段直接返回；运维类问题归这里（暂不转发，因为运维 copilot 是按单个实例诊断的粒度，没有流程级入口） |

**`flow_structure` 独立的 System Prompt**（`_structure_system_prompt`，verbatim）：
> 你是企业 OA 的流程设计讲解助手，给流程 owner 解释某条流程当前是怎么设计的——字段、环节、审批人、提交路径。只依据给你的流程定义原文作答，不要编造定义里没有的内容；定义里没覆盖到的细节，直说"当前定义未体现"。用简洁中文回答，不用英文双引号。

（`flow_structure` 的 User Prompt 把该流程的表单字段清单 + 环节/处理人/提交路径清单 + 历史对话 + 用户问题拼进去，**不复用发起向导的 agent 人格**——发起向导的输出 schema 是申请人视角的，固定要"推荐流程+抽天数"，owner 问结构会错位，所以这里配了一套专门的讲解 prompt。）

**交互模型的两个关键点**：
1. **子能力"回复给 orchestrator"，不是直连用户**——`_flow_structure_answer`/`_analytics_answer`/`_launch_guide_answer` 都是函数返回 dict，orchestrator 再决定怎么呈现。
2. **轻量加工，不做 LLM 重写**：事实主体**原样透传**（尤其分析的数字，过一遍 LLM"重新组织"会有精度/幻觉风险，违反"确定的事不让 LLM 猜"）；orchestrator 只在末尾挂一句自己才知道的**跨模块钩子**（`_cross_hook`：该流程有没有待处理项），**纯确定性字符串拼接，不重跑 LLM**。

**多轮对话**：`ask(message, history)`——`history` 带进分类 prompt（解析"那这条呢"这类指代）+ `flow_structure`/`analytics` 的 user prompt；前端 `state.manageHistory` 累积 user/assistant 文本，每次请求带最近几轮（后端 `_render_history` 只取最近 6 轮，避免 prompt 无限增长）。

---

## 八、设计原则速记

1. **一次意图分类 + 确定性分发**，绝大多数 agent 不自主链式调工具、更不是 agent 群聊。**唯一的例外是指标问答（§3.2）**，而且是有意为之、不是妥协：它的工具调用是**有护栏的取数梯度**（typed 查询优先、SQL 兜底、每一级都有确定性防线），不是让 LLM 自由链式调用任意能力。
2. **确定的事不过 LLM**：数字/路径/diff/打分/校验全是确定性代码；LLM 只理解意图、出 typed 对象、组织语言。指标问答的 `run_sql` 是这条原则的边界案例——护栏管的是"能不能碰"（安全），不是"答得对不对"（正确），这个边界在 §3.2 里明说，不假装护栏无所不能。
3. **复用能力不复用人格**：跨页面同类问题落到同一个底层服务对象；不同视角配不同 prompt（§七 flow_structure vs launch_guide 就是同一份数据两套 prompt 的例子）。
4. **每个 LLM 调用都有降级路**：模型不就绪返回 None → 给"未就绪"答复，不崩不假装。
5. **结构化输出 + 解析兜底**是标配：`with_structured_output(include_raw=True)` + `_coerce` + repair/分批重试。
6. **评测锚在确定性产出**（§一节点①的抽取步），语义复评（§八）是叠加层、不动 CLI 基线主分。
