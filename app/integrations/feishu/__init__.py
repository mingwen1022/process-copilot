"""飞书审批对接：把设计 Agent 产出的 ProcessDefinition 翻译成飞书原生审批定义。

心法与全项目一致——翻译是**确定性代码**，不是 LLM：ProcessDefinition 和飞书审批
定义都是结构化的，映射是结构到结构的确定函数（见 translator.py）。飞书原生审批 API
只支持线性流程，所以翻译时把条件分支线性化并记录 note，交由上层（合规拦截 UI）提示。
"""

from app.integrations.feishu.translator import (
    FeishuTranslation,
    translate_to_feishu_approval,
)

__all__ = ["FeishuTranslation", "translate_to_feishu_approval"]
