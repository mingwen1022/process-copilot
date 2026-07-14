from __future__ import annotations

import json
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

from langchain_core.tools import StructuredTool

from app.io_utils import write_json
from app.models.text import generate_text
from app.tracing import serialize_llm_call, write_trace
from data.schema import ProcessDefinition


def build_artifact_tools(model_or_provider: Any) -> list[StructuredTool]:
    return [
        build_drawio_generation_tool(model_or_provider),
    ]


def build_drawio_generation_tool(model_or_provider: Any) -> StructuredTool:
    def generate_drawio_from_process(process_json: str, output_path: str, out_dir: str) -> str:
        process = ProcessDefinition.model_validate_json(process_json)
        result = generate_drawio_with_llm(process, output_path, out_dir=out_dir, model_or_provider=model_or_provider)
        return json.dumps(result, ensure_ascii=False)

    return StructuredTool.from_function(
        func=generate_drawio_from_process,
        name="generate_drawio_from_process",
        description=(
            "Generate an editable draw.io .drawio XML file from ProcessDefinition JSON. "
            "Arguments: process_json, output_path, out_dir."
        ),
    )


def generate_drawio_with_llm(
    process: ProcessDefinition,
    output_path: str | Path,
    *,
    out_dir: str | Path,
    model_or_provider: Any,
) -> dict[str, Any]:
    out_path = Path(output_path)
    work_dir = Path(out_dir)
    work_dir.mkdir(parents=True, exist_ok=True)
    raw_output_path = work_dir / "drawio_raw_output.txt"
    report_path = work_dir / "drawio_validation_report.json"
    messages_path = work_dir / "drawio_messages.json"

    system_prompt = _drawio_system_prompt()
    user_prompt = _drawio_user_prompt(process)
    raw_output = generate_text(model_or_provider, system_prompt, user_prompt)
    raw_output_path.write_text(raw_output, encoding="utf-8")
    write_trace(
        serialize_llm_call(
            agent="artifact_generation_agent",
            step="drawio_generation",
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            assistant_output=raw_output,
            metadata={"output_path": str(out_path)},
        ),
        messages_path,
    )

    try:
        drawio_xml = extract_drawio_xml(raw_output)
        drawio_xml = enforce_drawio_black_fonts(drawio_xml)
        validation = validate_drawio_xml(drawio_xml)
        out_path.write_text(drawio_xml, encoding="utf-8")
    except ValueError as exc:
        validation = {"status": "error", "validator": "drawio_xml", "message": str(exc)}
        out_path.write_text(raw_output, encoding="utf-8")

    report = {
        **validation,
        "output_path": str(out_path),
        "raw_output_path": str(raw_output_path),
        "messages_path": str(messages_path),
    }
    write_json(report, report_path)
    return report


def _drawio_system_prompt() -> str:
    return """你是 draw.io / diagrams.net 流程图文件生成器。

要求：
1. 只输出完整 .drawio XML，不要输出 Markdown、解释或代码块。
2. 根节点必须是 <mxfile>，内部包含 <diagram>、<mxGraphModel>、<root>。
3. 生成的文件应能被 draw.io 打开编辑。
4. 使用矩形表示审批环节，菱形表示关键业务分叉，椭圆表示开始/结束。
5. 保留关键路径条件标签，例如金额门槛、天数门槛、印章类型、并行会签。
6. 不展示所有退回路径细节，除非退回路径是业务主干的一部分。
7. 所有节点、边标签、说明文字的字体必须为黑色，style 中统一使用 fontColor=#000000；不要使用白色或浅色字体。
8. 形状填充色必须足够浅，保证黑色文字在 draw.io 深色或浅色画布上都清晰可读。
"""


def _drawio_user_prompt(process: ProcessDefinition) -> str:
    return f"""请基于以下 ProcessDefinition JSON 生成简洁、可编辑、布局清晰的 draw.io XML 文件。

ProcessDefinition JSON:
{json.dumps(process.model_dump(mode="json"), ensure_ascii=False, indent=2)}
"""


def extract_drawio_xml(raw_output: str) -> str:
    text = raw_output.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        text = "\n".join(lines).strip()

    start = text.find("<mxfile")
    end = text.rfind("</mxfile>")
    if start == -1 or end == -1:
        raise ValueError("LLM draw.io output did not contain a complete <mxfile>...</mxfile> document")
    return text[start : end + len("</mxfile>")].strip() + "\n"


def validate_drawio_xml(drawio_xml: str) -> dict[str, Any]:
    try:
        root = ET.fromstring(drawio_xml)
    except ET.ParseError as exc:
        raise ValueError(f"draw.io XML is not parseable: {exc}") from exc

    if _local_name(root.tag) != "mxfile":
        raise ValueError(f"draw.io root element must be mxfile, got {_local_name(root.tag)}")
    if root.find(".//diagram") is None:
        raise ValueError("draw.io XML missing <diagram>")
    if root.find(".//mxGraphModel") is None:
        raise ValueError("draw.io XML missing <mxGraphModel>")
    if root.find(".//root") is None:
        raise ValueError("draw.io XML missing <root>")
    return {"status": "success", "validator": "drawio_xml", "message": "draw.io XML passed basic validation"}


def enforce_drawio_black_fonts(drawio_xml: str) -> str:
    root = ET.fromstring(drawio_xml)
    for element in root.iter():
        if "style" in element.attrib:
            element.set("style", _set_style_value(element.attrib["style"], "fontColor", "#000000"))
        if "value" in element.attrib:
            element.set("value", _normalize_value_font_colors(element.attrib["value"]))
    return ET.tostring(root, encoding="unicode") + "\n"


def _set_style_value(style: str, key: str, value: str) -> str:
    parts = [part for part in style.split(";") if part]
    updated: list[str] = []
    found = False
    for part in parts:
        if part.split("=", 1)[0] == key:
            updated.append(f"{key}={value}")
            found = True
        else:
            updated.append(part)
    if not found:
        updated.append(f"{key}={value}")
    return ";".join(updated) + ";"


def _normalize_value_font_colors(value: str) -> str:
    replacements = {
        "#fff": "#000000",
        "#FFF": "#000000",
        "#ffffff": "#000000",
        "#FFFFFF": "#000000",
        "color:white": "color:#000000",
        "color: white": "color:#000000",
        "color=&quot;white&quot;": "color=&quot;#000000&quot;",
        "color=\"white\"": "color=\"#000000\"",
    }
    normalized = value
    for old, new in replacements.items():
        normalized = normalized.replace(old, new)
    return normalized


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]
