"""原子规则数据模型（模块3 · 规则RAG · Phase 1）。

把知识库文档拆成可检索、可校验、可引用到子句的**原子规则**。心法与全项目一致：
结构化能判的走确定性（check_type=deterministic + 一个可枚举的 check kind），
定性的才交 LLM-judge。适用条件复用 app.runtime.condition_eval（与流程路由条件
同一套表达式），对案例属性/流程上下文求值。

deterministic 规则的 requirement 用"check kind + params"表达，kind 是**有限集**，
每个 kind 对应 Phase 3 里一个确定性检查函数——这样规则可穷举测试、不会"错得很
安静"。定性规则没有 requirement，只有 statement 文本，交 LLM-judge。
"""

from __future__ import annotations

from enum import Enum
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field


class RuleDimension(str, Enum):
    COMPANY_POLICY = "company_policy"
    DESIGN_STANDARD = "design_standard"
    ORG_ROLE = "org_role"
    PROCESS_PLAYBOOK = "process_playbook"
    OPS_BASELINE = "ops_baseline"


class CheckKind(str, Enum):
    """确定性检查种类（有限集）。每个 kind 对应 Phase 3 的一个检查函数。"""

    HAS_DRAFT_AND_END = "has_draft_and_end"  # 必须有起草环节 + 通向END的路径
    MUST_HAVE_NODE = "must_have_node"  # 必须存在处理角色/名称匹配关键词的审批环节
    MUST_ROUTE_THROUGH_NODE = "must_route_through_node"  # 高风险情形必须流经某环节，不能被改道绕过（条件可达性）
    ATTACHMENT_REQUIRED_CONDITION = "attachment_required_condition"  # 某附件须按业务条件必传
    FIELD_REQUIRED_AT_DRAFT = "field_required_at_draft"  # 某字段须在起草环节必填
    HANDLER_MODE_FOR_NODE = "handler_mode_for_node"  # 某类环节的处理人方式须符合
    READONLY_AUTOFILL_FIELD = "readonly_autofill_field"  # 自动带出字段须为只读
    VALUE_THRESHOLD = "value_threshold"  # 表单字段值须满足阈值（如差旅标准：酒店<=800/晚）——审批内容风险


class RuleProvenance(BaseModel):
    source_doc: str = Field(description="来源文档 doc_id（对应 data/knowledge 里某份）")
    clause: str = Field(description="子句定位，如 '二、审批层级 第4条'，用于引用到子句级")


class RuleRequirement(BaseModel):
    """确定性可判要求（仅 check_type=deterministic 用）。"""

    kind: CheckKind
    params: dict[str, Any] = Field(default_factory=dict, description="检查参数，随 kind 而定")


class AtomicRule(BaseModel):
    rule_id: str = Field(description="稳定唯一 id，如 leave.sick_leave_certificate")
    title: str = Field(default="", description="中文短标题，供人读列表展示（rule_id 是机器可读 id，不做标题用）")
    dimension: RuleDimension
    statement: str = Field(description="规则的自然语言表述（要求本身），供展示与 LLM-judge")
    applies_to_processes: list[str] = Field(default_factory=list, description="锚定的真实流程 id（可评测）")
    applies_to_domains: list[str] = Field(default_factory=list, description="适用业务域（广度检索用）")
    applicability_condition: Optional[str] = Field(
        default=None,
        description="该规则在什么条件下适用（condition_eval 表达式，对案例属性求值）；None=总适用。"
        "例：'请假类型=病假 且 请假天数>=3'",
    )
    check_type: Literal["deterministic", "llm_judge"]
    requirement: Optional[RuleRequirement] = Field(
        default=None, description="确定性检查规格；check_type=llm_judge 时为 None"
    )
    trigger_attributes: list[str] = Field(
        default_factory=list,
        description=(
            "仅 check_type=llm_judge 用：这次编辑的 diff 里，flow_nodes.changed 下某个环节的"
            "changes[*].attribute 命中这里列的属性名（如 node_name、handler），或 submit_paths 有"
            "变化且这里包含 'submit_paths'，才认为这次编辑可能影响这条定性规则、值得为它打一次"
            "LLM-judge；环节增删（flow_nodes.added/removed）总是触发，不受这个列表限制。空列表="
            "保守起见任何真实改动都触发（没声明触发条件时不敢随便跳过）。"
        ),
    )
    governs: list[str] = Field(
        default_factory=list,
        description=(
            "这条规则'管辖'哪些具体词——字段名/环节角色关键词/路由条件里的字段名（如'采购金额'、"
            "'采购委员会'、'公章'）。用于'主动提醒'：一次编辑改动过的东西（字段名/环节名/路径名/"
            "改动的条件文本/取值）文本上命中这里任一词，就在待确认卡里把这条规则摆出来当提醒——"
            "只提醒'你碰到了这条制度'，不代表违规（违规判定另走 requirement/llm_judge）。空列表时"
            "回退到用 requirement.params 里的各种 *_keywords 兜底。"
        ),
    )
    provenance: RuleProvenance
    severity: Literal["high", "medium"] = "medium"


class RuleSet(BaseModel):
    """一组原子规则（一个 json 文件的顶层结构）。"""

    rules: list[AtomicRule] = Field(default_factory=list)
