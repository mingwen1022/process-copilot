"""在办任务副驾（#2）的主动体检——确定性，不调 LLM。

打开在办任务时自动查两类问题，全部复用已有引擎，不是通用问答：
- 材料缺失：按 attachment 的 required_condition 对本实例表单值求值（condition_eval），
  应传而未上传的 → 提醒。这是实例级检查。
- 合规红线：对该流程定义跑确定性合规校验（复用 app.rag.compliance），把设计层面的
  合规问题也告诉审批人。这是设计级检查。

见 doc/流程参与者副驾-设计.md 第 5 节。
"""

from __future__ import annotations

from pydantic import BaseModel

from app.rag.compliance import deterministic_findings_for_draft
from app.runtime.condition_eval import evaluate_condition
from data.schema import InstanceContext


class MaterialFinding(BaseModel):
    attachment_type: str
    reason: str  # 为什么按制度需要它（哪个条件命中）


class TaskInspection(BaseModel):
    material_findings: list[MaterialFinding] = []
    compliance_findings: list[dict] = []  # 复用 ComplianceFinding 的 json

    @property
    def clean(self) -> bool:
        return not self.material_findings and not self.compliance_findings


def inspect_task(context: InstanceContext) -> TaskInspection:
    process = context.process

    # —— 材料缺失（实例级）——
    materials: list[MaterialFinding] = []
    for att in process.attachments or []:
        if not att.required_condition:
            continue
        result = evaluate_condition(att.required_condition, context.form_values)
        if result.matched and att.attachment_type not in context.uploaded_materials:
            materials.append(MaterialFinding(
                attachment_type=att.attachment_type,
                reason=f"当前情形命中「{att.required_condition}」，按制度须上传，但未见上传。",
            ))

    # —— 合规红线（设计级，复用合规引擎）——
    compliance = [f.model_dump(mode="json") for f in deterministic_findings_for_draft(process, process_id=process.meta.process_id)]

    return TaskInspection(material_findings=materials, compliance_findings=compliance)
