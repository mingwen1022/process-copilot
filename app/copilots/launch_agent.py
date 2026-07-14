"""发起问答副驾（#4）的 agent。纯只读：不改任何实例、不产生副作用。

分工照旧——LLM 解析意图 + 组织回答，确定性代码算路径。agent 输出：推荐哪个流程 +
从用户处境里抽出的假设属性（供 condition_eval 预演路径）+ 自然语言答复。路径预演本身
由 launch_catalog.preview_path 确定性完成，不靠 LLM 读条件。
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from app.models.bedrock import create_bedrock_chat_model
from app.models.text import is_legacy_text_generator
from app.copilots.launch_catalog import CatalogProcess


class LaunchAnswer(BaseModel):
    reply: str = Field(description="给用户的自然语言答复：推荐哪个流程、为什么、是否符合、需要什么材料")
    recommended_process_id: str | None = Field(default=None, description="推荐的流程 id，必须是目录中某个；无法确定则 None")
    leave_type: str | None = Field(default=None, description="若涉及请假：从用户处境抽出的请假类型")
    leave_days: float | None = Field(default=None, description="若涉及请假：从用户处境抽出的请假天数")


def _system_prompt() -> str:
    return """你是企业 OA 的发起问答助手，帮还没发起流程的员工搞清楚：该走哪个流程、我这情况会
经过哪些环节、需要什么材料、符不符合条件。你只答疑，不发起、不改任何东西。

给你：可选流程目录（id/名称/用途）+ 检索到的相关制度规则 + 用户的处境描述。
你要：
- recommended_process_id：从目录里选最匹配的一个（必须是目录中的 id）；确实无法判断才留空。
- 若涉及请假：从处境里抽出 leave_type（请假类型）和 leave_days（天数），供系统预演审批路径。
- reply：用简洁中文回答。涉及"符不符合/要什么材料"时，**只依据给你的规则**，无规则依据就说
  "建议咨询 HR/流程负责人"，不要编造制度。不要臆造目录里没有的流程。不用英文双引号。

注意：审批会经过哪些环节由系统据流程定义确定性算出，你不用在 reply 里自己推演路径细节，
说清推荐哪个流程 + 关键条件（如天数分档）+ 材料/资格即可。"""


def _user_prompt(catalog: list[CatalogProcess], rules: str, user_message: str) -> str:
    cat_lines = "\n".join(f"  - {c.process_id} | {c.name}：{c.description}" for c in catalog)
    return f"""可选流程目录：
{cat_lines}

检索到的相关制度规则：
{rules or "（未检索到相关规则）"}

用户处境：{user_message}

请推荐流程、抽出请假类型/天数（若涉及请假）、并作答。"""


class LaunchCopilotAgent:
    def __init__(self, model: Any | None = None) -> None:
        self.model = model or create_bedrock_chat_model()
        self.structured_model = (
            None
            if is_legacy_text_generator(self.model)
            else self.model.with_structured_output(LaunchAnswer, method="function_calling", include_raw=True)
        )

    def answer(self, catalog: list[CatalogProcess], rules: str, user_message: str) -> LaunchAnswer | None:
        if self.structured_model is None:
            return None
        response = self.structured_model.invoke(
            [
                {"role": "system", "content": _system_prompt()},
                {"role": "user", "content": _user_prompt(catalog, rules, user_message)},
            ]
        )
        return _coerce(response)


def _coerce(response: Any) -> LaunchAnswer | None:
    parsed: Any = None
    parsing_error: Any = None
    if isinstance(response, dict):
        parsed = response.get("parsed")
        parsing_error = response.get("parsing_error")
    elif isinstance(response, LaunchAnswer):
        parsed = response
    else:
        parsed = getattr(response, "parsed", None)
        parsing_error = getattr(response, "parsing_error", None)
    if parsing_error or parsed is None:
        return None
    if not isinstance(parsed, LaunchAnswer):
        try:
            parsed = LaunchAnswer.model_validate(parsed)
        except Exception:  # noqa: BLE001
            return None
    return parsed
