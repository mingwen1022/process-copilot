"""
流程设计 Agent - 标准化流程要素数据模型
版本: V1.0
说明: 第一版 Demo 精简版，去除风险点/运维要求/外部系统关联等高复杂度字段
"""

from __future__ import annotations
from typing import List, Optional, Literal
from enum import Enum
from pydantic import BaseModel, Field


# ──────────────────────────────────────────────
# 枚举类型定义
# ──────────────────────────────────────────────

class ComponentType(str, Enum):
    """表单字段组件类型"""
    TEXT          = "单行文本"
    TEXTAREA      = "多行文本"
    SELECT_SINGLE = "下拉单选"
    SELECT_MULTI  = "下拉多选"
    RADIO         = "单选按钮"
    DATE          = "日期组件"
    DATETIME      = "日期时间组件"
    NUMBER        = "数字"
    EMPLOYEE      = "员工选择"
    DEPARTMENT    = "部门选择"
    READONLY      = "只读文本"
    FILE_NUMBER   = "文号"
    LINK          = "文字链接"
    ATTACHMENT    = "附件"


class HandlerMode(str, Enum):
    """处理人选择方式"""
    SINGLE         = "单选-单人处理"
    MULTI_PARALLEL = "多选-并行处理"
    MULTI_PREEMPT  = "多选-抢办"
    ALL_PARALLEL   = "全选-并行处理"
    ALL_PREEMPT    = "全选-抢办"


class HandlerSource(str, Enum):
    """处理人部门来源"""
    OWN_DEPT      = "本部门"
    OWN_COMPANY   = "本公司"
    OWN_LINE      = "本条线"
    SPECIFIC_DEPT = "特定部门"
    FORM_FIELD    = "表单字段指定"
    UNSPECIFIED   = "不指定"


class RoleType(str, Enum):
    """角色类型"""
    PROCESS  = "流程角色"   # 仅用于流程流转
    BUSINESS = "业务角色"   # 可用于流程流转、菜单权限


# ──────────────────────────────────────────────
# 子模型
# ──────────────────────────────────────────────

class OpinionConfig(BaseModel):
    """环节意见配置"""
    conclusive_required: bool = Field(
        description="是否需要结论性意见（同意/不同意），必填则用户须选择后方可提交"
    )
    detail_required: bool = Field(
        description="意见详情文本框是否必填"
    )
    conclusive_options: List[str] = Field(
        default=["同意", "不同意"],
        description="结论性意见的选项列表"
    )


class SubmitPath(BaseModel):
    """单条提交路径"""
    path_name: str = Field(description="路径显示名称，如'送部门主管审批'、'退回起草'")
    condition: Optional[str] = Field(
        default=None,
        description="路径触发条件表达式，None 表示无条件；退回类路径通常有条件"
    )
    target_node_id: str = Field(
        description="目标节点ID；'END'表示流程正常结束，'DRAFT'表示退回起草"
    )


class HandlerConfig(BaseModel):
    """环节处理人配置"""
    mode: HandlerMode = Field(description="处理人选择方式")
    source: HandlerSource = Field(description="处理人部门来源")
    role: str = Field(
        description="处理角色，如'部门主管'、'一级主/辅负责人'或自定义流程角色名"
    )
    source_field: Optional[str] = Field(
        default=None,
        description="当 source=FORM_FIELD 时，指定从哪个表单字段取处理人"
    )


class FormField(BaseModel):
    """表单字段配置"""
    seq: int = Field(description="字段序号")
    field_name: str = Field(description="字段名称（表单中展示的名称）")
    required_stages: List[str] = Field(
        description="必填的环节 node_id 列表；空列表表示非必填；['all'] 表示所有环节必填"
    )
    visible_stages: List[str] = Field(
        description="可见的环节 node_id 列表；['all'] 表示所有环节可见"
    )
    editable_stages: List[str] = Field(
        description="可编辑的环节 node_id 列表；空列表表示只读"
    )
    component_type: ComponentType = Field(description="组件类型")
    logic_description: Optional[str] = Field(
        default=None,
        description="字段显示逻辑/联动逻辑/校验逻辑说明"
    )
    default_value: Optional[str] = Field(
        default=None,
        description="字段默认值或自动生成规则说明"
    )
    options: Optional[List[str]] = Field(
        default=None,
        description="下拉/单选组件的选项列表"
    )
    placeholder: Optional[str] = Field(
        default=None,
        description="输入框提示文字"
    )
    max_length: Optional[int] = Field(
        default=None,
        description="文本字段最大字符数限制"
    )


class FlowNode(BaseModel):
    """审批环节配置"""
    node_id: str = Field(description="环节唯一标识（英文，用于路径引用）")
    node_name: str = Field(description="环节名称（中文，用于展示）")
    is_draft: bool = Field(default=False, description="是否为起草环节")
    handler: Optional[HandlerConfig] = Field(
        default=None,
        description="处理人配置；起草环节为 None"
    )
    opinion: Optional[OpinionConfig] = Field(
        default=None,
        description="意见配置；起草环节通常为 None 或仅有非必填意见详情"
    )
    opinion_label: Optional[str] = Field(
        default=None,
        description="意见域名称，如'申请部门意见'、'部门领导意见'"
    )
    time_limit_days: Optional[int] = Field(
        default=None,
        description="限时处理天数；None 表示不限时"
    )
    submit_paths: List[SubmitPath] = Field(
        description="该环节的所有提交路径列表"
    )


class AttachmentConfig(BaseModel):
    """附件配置"""
    attachment_type: str = Field(description="附件类型名称")
    upload_stages: List[str] = Field(description="支持上传的环节 node_id 列表；['all'] 表示所有环节")
    required_stages: List[str] = Field(description="必传的环节 node_id 列表；空列表表示非必传")
    required_condition: Optional[str] = Field(
        default=None,
        description="必传的业务条件表达式，如'请假类型=病假 且 请假天数>=3'；None 表示只按 required_stages 判断，"
        "不附加业务条件。表达式写法与 SubmitPath.condition 一致。"
    )


class RoleConfig(BaseModel):
    """角色配置"""
    role_name: str = Field(description="角色名称")
    role_type: RoleType = Field(description="角色类型")
    members: List[str] = Field(description="挂接人员列表（姓名或工号）")
    department: Optional[str] = Field(default=None, description="角色所属部门")


class ProcessMeta(BaseModel):
    """流程元信息"""
    process_id: str = Field(description="流程编号，如 'LEAVE-001'")
    process_name: str = Field(description="流程名称")
    version: str = Field(default="V1.0.0", description="版本号")
    responsible_dept: str = Field(description="流程责任部门")
    description: str = Field(description="流程简要描述")
    applicant_scope: str = Field(description="适用范围/可发起人员范围")
    entry_point: str = Field(default="OA系统", description="流程发起入口")


# ──────────────────────────────────────────────
# 顶层模型
# ──────────────────────────────────────────────

class ProcessDefinition(BaseModel):
    """
    标准化流程定义（唯一数据源）
    所有可读版本（Excel、Mermaid流程图）均由此模型生成
    """
    meta: ProcessMeta
    form_fields: List[FormField] = Field(description="表单字段列表，按展示顺序排列")
    flow_nodes: List[FlowNode] = Field(description="审批环节列表，按流转顺序排列")
    attachments: Optional[List[AttachmentConfig]] = Field(
        default=None,
        description="附件配置列表；None 表示无特殊附件要求"
    )
    roles: Optional[List[RoleConfig]] = Field(
        default=None,
        description="自定义角色配置；使用通用角色的流程可为 None"
    )

    def get_node_by_id(self, node_id: str) -> Optional[FlowNode]:
        """根据 node_id 查找环节"""
        return next((n for n in self.flow_nodes if n.node_id == node_id), None)

    def get_node_name(self, node_id: str) -> str:
        """根据 node_id 获取环节名称，处理特殊节点"""
        if node_id == "END":
            return "流程结束"
        if node_id == "DRAFT":
            return "退回起草"
        node = self.get_node_by_id(node_id)
        return node.node_name if node else node_id

    def to_mermaid(self, simplified: bool = True) -> str:
        """
        生成 Mermaid 流程图代码
        simplified=True: 仅展示主要节点和关键分叉（不展示同意/不同意细节）
        simplified=False: 展示所有路径（调试用）
        """
        lines = ["flowchart TD"]
        lines.append('    START([开始]) --> draft[起草]')

        if simplified:
            # 简化模式：只遍历非退回路径，找到关键分叉
            for node in self.flow_nodes:
                if node.is_draft:
                    continue
                # 找非退回的提交路径
                forward_paths = [
                    p for p in node.submit_paths
                    if p.target_node_id not in ("DRAFT",)
                    and not (p.path_name.startswith("结束本人") or p.path_name.startswith("增加"))
                ]
                targets = list({p.target_node_id for p in forward_paths})
                prev_node = self._find_predecessor(node.node_id)

                for tgt in targets:
                    tgt_name = self.get_node_name(tgt)
                    src_label = node.node_id
                    if tgt == "END":
                        lines.append(f'    {src_label}[{node.node_name}] --> END([流程结束])')
                    else:
                        lines.append(f'    {src_label}[{node.node_name}] --> {tgt}[{tgt_name}]')
        else:
            for node in self.flow_nodes:
                for path in node.submit_paths:
                    tgt = path.target_node_id
                    label = path.path_name
                    tgt_name = self.get_node_name(tgt)
                    src = node.node_id
                    if tgt == "END":
                        lines.append(f'    {src}[{node.node_name}] -->|{label}| END([流程结束])')
                    elif tgt == "DRAFT":
                        lines.append(f'    {src}[{node.node_name}] -->|{label}| draft[起草]')
                    else:
                        lines.append(f'    {src}[{node.node_name}] -->|{label}| {tgt}[{tgt_name}]')

        return "\n".join(lines)

    def _find_predecessor(self, node_id: str) -> Optional[str]:
        for node in self.flow_nodes:
            for path in node.submit_paths:
                if path.target_node_id == node_id:
                    return node.node_id
        return None
