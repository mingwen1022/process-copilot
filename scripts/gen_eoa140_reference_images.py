"""重生成 EOA140 案例的两个"仅参考"扫描件（旧线下签批流转截图 png + 旧纸质备案表 pdf）。

只重画这两个二进制参考件（原先用无 CJK 字形的默认字体→豆腐块），不动 EOA140 的
真实文本 source（docx/doc/xlsx/md/csv/txt）。这两个文件是噪音/参考，摄取时只触发
OCR warning、不喂给 LLM。

用法：uv run python scripts/gen_eoa140_reference_images.py
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw, ImageFont
from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas

RAW = Path("data/cases/EOA140_subsidiary_major_matter/raw_sources")

_CJK_FONT_CANDIDATES = [
    "/System/Library/Fonts/Supplemental/Arial Unicode.ttf",
    "/System/Library/Fonts/STHeiti Light.ttc",
    "/System/Library/Fonts/Supplemental/Songti.ttc",
    "/System/Library/Fonts/Hiragino Sans GB.ttc",
]


def cjk_font(size: int) -> ImageFont.FreeTypeFont:
    for path in _CJK_FONT_CANDIDATES:
        if Path(path).exists():
            try:
                return ImageFont.truetype(path, size)
            except Exception:
                continue
    return ImageFont.load_default()


def gen_flow_png() -> None:
    nodes = ["起草", "子公司内部人员审核", "子公司高管审批", "申请人分发", "对口部门专员分发", "公司领导批示"]
    title = cjk_font(28)
    body = cjk_font(22)
    img = Image.new("RGB", (760, 180 + len(nodes) * 120), "white")
    d = ImageDraw.Draw(img)
    d.text((40, 30), "（扫描件）子公司重大事项 · 旧线下签批流转", fill="black", font=title)
    d.text((40, 74), "仅作历史参考，不代表线上流程口径", fill="gray", font=cjk_font(18))
    y = 130
    for i, name in enumerate(nodes):
        d.rounded_rectangle([180, y, 580, y + 70], radius=12, outline="black", width=2, fill="#f4f4f4")
        d.text((210, y + 22), name, fill="black", font=body)
        if i < len(nodes) - 1:
            d.line([380, y + 70, 380, y + 120], fill="black", width=2)
            d.polygon([(374, y + 110), (386, y + 110), (380, y + 120)], fill="black")
        y += 120
    img.save(RAW / "07_旧线下签批流转截图_仅参考.png")


def gen_form_pdf() -> None:
    title = cjk_font(28)
    body = cjk_font(22)
    img = Image.new("RGB", (1000, 720), "white")
    d = ImageDraw.Draw(img)
    d.rectangle([30, 30, 970, 690], outline="black", width=2)
    d.text((60, 54), "（扫描件）子公司重大事项审批备案表 · 旧纸质版", fill="black", font=title)
    lines = [
        "标题：______________________________",
        "任务类型：□子公司审批类  □子公司备案类",
        "对口部门：____________   重要程度：□一般 □较高 □很高",
        "起草人：______   起草时间：__________",
        "子公司内部人员审核意见：______",
        "子公司高管审批意见：______",
        "公司领导批示：______",
        "（仅作历史参考，不代表线上流程口径）",
    ]
    for i, line in enumerate(lines):
        d.text((60, 120 + i * 64), line, fill="black", font=body)
    img_path = RAW / "_tmp_form.png"
    img.save(img_path)
    c = canvas.Canvas(str(RAW / "08_旧纸质备案表扫描版_仅参考.pdf"), pagesize=A4)
    w, h = A4
    c.drawImage(str(img_path), 40, h - 520, width=w - 80, height=460)
    c.showPage()
    c.save()
    img_path.unlink()


def main() -> None:
    gen_flow_png()
    gen_form_pdf()
    print("重生成 EOA140 参考件：")
    print("  - 07_旧线下签批流转截图_仅参考.png")
    print("  - 08_旧纸质备案表扫描版_仅参考.pdf")


if __name__ == "__main__":
    main()
