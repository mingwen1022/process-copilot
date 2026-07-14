"""图片 source 的视觉转录：把截图（聊天记录/系统截图/表格等）忠实转录为文本。

用多模态 Claude（Bedrock Converse）做"看图说字"——只转录、保结构、不推断，把
理解和抽取留给下游 extraction agent（守住"确定性 vs LLM"里"转录 vs 抽取"的边界）。
"""

from __future__ import annotations

import base64
from pathlib import Path
from typing import Any, Callable

from langchain_core.messages import HumanMessage

_MIME_BY_SUFFIX = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".gif": "image/gif",
    ".bmp": "image/bmp",
    ".tiff": "image/tiff",
    ".tif": "image/tiff",
}

TRANSCRIBE_PROMPT = (
    "这是一张业务材料截图（可能是聊天记录、其他业务系统截图、表格、扫描件等）。"
    "请忠实转录图中的全部文字内容，并保留结构：\n"
    "- 聊天记录：标注发言人与时间（若有），逐条转录。\n"
    "- 表格：按行列转录，保留表头与单元格对应。\n"
    "- 系统截图/表单：保留字段名与对应的值。\n"
    "- 标题、时间戳、编号等照录。\n"
    "只做转录，不要推断、不要补充图中没有的信息，也不要给出总结或评价。"
    "如果图中有‘仅供参考’‘历史’‘草稿’‘线下’等标注，一并如实转录。"
    "如果图片无法辨认或没有可读文字，只回复：[无法辨认的图片内容]。"
)

ImageTranscriber = Callable[[Path, str], str]


def _mime_for(path: Path) -> str:
    return _MIME_BY_SUFFIX.get(path.suffix.lower(), "image/png")


def transcribe_image(model: Any, image_path: str | Path) -> str:
    """调多模态模型转录单张图片，返回转录文本（失败抛异常，由调用方降级处理）。"""
    path = Path(image_path)
    b64 = base64.b64encode(path.read_bytes()).decode("ascii")
    message = HumanMessage(
        content=[
            {"type": "text", "text": TRANSCRIBE_PROMPT},
            {"type": "image_url", "image_url": {"url": f"data:{_mime_for(path)};base64,{b64}"}},
        ]
    )
    response = model.invoke([message])
    content = response.content
    if isinstance(content, list):
        return "".join(part.get("text", "") if isinstance(part, dict) else str(part) for part in content).strip()
    return str(content).strip()


def build_bedrock_image_transcriber() -> ImageTranscriber:
    """构造一个 (image_path, source_type) -> 转录文本 的转录器，懒加载 Bedrock 视觉模型。"""
    from app.models.bedrock import create_bedrock_chat_model

    cached: dict[str, Any] = {}

    def transcriber(image_path: Path, source_type: str) -> str:
        if "model" not in cached:
            cached["model"] = create_bedrock_chat_model()
        return transcribe_image(cached["model"], image_path)

    return transcriber
