"""一次性生成请假申请 demo 案例的多格式 raw_sources（镜像 EOA140 的多源杂乱场景）。

格式覆盖：docx / txt / xlsx / doc(html) / md / csv / png / pdf。
注入 texture：早期 vs 后续拍板口径冲突、故意留待确认项、无关噪音。
字段名 / 节点名 / 请假类型严格对齐 standard/target.json，便于去硬编码后的 agent 抽取。

用法：uv run python scripts/gen_leave_request_sources.py
"""

from __future__ import annotations

import csv
import io
from pathlib import Path

from docx import Document
from openpyxl import Workbook
from PIL import Image, ImageDraw, ImageFont
from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas

RAW = Path("data/cases/leave_request/raw_sources")

_CJK_FONT_CANDIDATES = [
    "/System/Library/Fonts/Supplemental/Arial Unicode.ttf",
    "/System/Library/Fonts/STHeiti Light.ttc",
    "/System/Library/Fonts/Supplemental/Songti.ttc",
    "/System/Library/Fonts/Hiragino Sans GB.ttc",
]


def cjk_font(size: int) -> ImageFont.FreeTypeFont:
    """加载含中文字形的字体，避免中文渲染成豆腐块。"""
    for path in _CJK_FONT_CANDIDATES:
        if Path(path).exists():
            try:
                return ImageFont.truetype(path, size)
            except Exception:
                continue
    return ImageFont.load_default()


def reset_dir() -> None:
    RAW.mkdir(parents=True, exist_ok=True)
    for p in RAW.iterdir():
        if p.is_file():
            p.unlink()


def gen_docx() -> None:
    doc = Document()
    doc.add_heading("员工请假申请流程 上线需求汇总", level=1)
    doc.add_paragraph("提出部门：人力资源部　｜　适用范围：公司全体在职员工　｜　发起入口：OA 系统（移动端与 PC 端均可发起）")
    doc.add_heading("一、审批主链路", level=2)
    doc.add_paragraph("起草（申请人本人）→ 部门主管审批 → 部门总经理审批 → 条线分管领导审批 → 结束。")
    doc.add_paragraph("是否往上继续上报，由下面的“按类型和时长”规则决定，不是每次都走完整链路。")
    doc.add_heading("二、按请假类型与时长的上报规则（最终口径，以本汇总为准）", level=2)
    doc.add_paragraph("1. 部门主管审批通过后：若为事假或病假且请假天数≤3天，到部门主管即结束，不再上报；其他情况送部门总经理审批。")
    doc.add_paragraph("2. 部门总经理审批通过后：若请假天数≤7天，到部门总经理即结束；若请假天数>7天，或属于婚假/产假/陪产假，必须再送条线分管领导审批。")
    doc.add_paragraph("3. 条线分管领导审批通过后，流程结束。")
    doc.add_paragraph("4. 任何一级审批选择“不同意”，均退回起草人重新修改。")
    doc.add_heading("三、请假类型（固定可选项）", level=2)
    doc.add_paragraph("年假、事假、病假、婚假、产假、陪产假、丧假。（注：调休不在本流程，单独走考勤系统。）")
    doc.add_heading("四、发起页填写内容", level=2)
    doc.add_paragraph("申请人、所属部门由系统按登录人自动带出，申请人不可编辑。")
    doc.add_paragraph("申请人需填写：请假类型、开始日期、结束日期、请假天数、请假事由、工作交接人、联系电话。其中工作交接人从员工列表选择。")
    doc.save(RAW / "01_请假流程需求汇总_人力资源部.docx")


def gen_email_txt() -> None:
    text = """From: 人力资源部-周敏 <zhoumin@example.local>
To: OA平台项目组
Subject: HR 周邮件包（含请假流程，仅其一）
Date: 2026-05-18

各位，本周 HR 待办几项，请假流程是其中一项，其余见附注。

【请假流程·早期讨论口径——以后续需求汇总为准，本段已作废】
- 最初设想：请假≤5天部门主管审批即可，超过5天才上部门总经理；后经 5/26 例会调整为"事假/病假≤3天主管结束、≤7天总经理结束、>7天或特殊假上分管领导"。请以《请假流程需求汇总》最终口径为准。

【其它 HR 事项（与请假流程无关）】
- 年度体检安排将于下月开放预约，另行通知。
- 年会报名表已发各部门，6月底截止。

请假流程的字段和审批层级细节见单独的字段矩阵与群聊记录。
周敏
"""
    (RAW / "02_HR周邮件包_请假是其中一项.txt").write_text(text, encoding="utf-8")


def gen_xlsx() -> None:
    wb = Workbook()
    ws = wb.active
    ws.title = "请假表单字段"
    ws.append(["字段", "组件形式", "是否必填", "说明"])
    rows = [
        ["申请人", "只读文本", "自动", "系统带出当前登录人，不可编辑"],
        ["所属部门", "只读文本", "自动", "系统带出申请人所属部门，不可编辑"],
        ["请假类型", "下拉单选", "必填", "年假/事假/病假/婚假/产假/陪产假/丧假"],
        ["开始日期", "日期组件", "必填", "yyyy-MM-dd"],
        ["结束日期", "日期组件", "必填", "yyyy-MM-dd"],
        ["请假天数", "数字", "必填", "申请人填写，单位天"],
        ["请假事由", "多行文本", "必填", "简述事由"],
        ["工作交接人", "员工选择", "必填", "从员工列表选择交接人"],
        ["联系电话", "单行文本", "必填", "请假期间联系方式"],
    ]
    for r in rows:
        ws.append(r)
    ws2 = wb.create_sheet("审批权限矩阵")
    ws2.append(["审批环节", "处理人来源", "处理人角色", "处理方式", "意见"])
    for r in [
        ["部门主管审批", "本部门", "部门主管", "单人处理", "同意/不同意"],
        ["部门总经理审批", "本部门", "部门总经理", "单人处理", "同意/不同意"],
        ["条线分管领导审批", "本条线", "条线分管领导", "单人处理", "同意/不同意"],
    ]:
        ws2.append(r)
    wb.save(RAW / "03_请假字段与审批权限矩阵.xlsx")


def gen_doc_html() -> None:
    html = """<html><head><meta charset="utf-8"><title>请假流程讨论群聊</title></head><body>
<h3>企业微信讨论记录 - 请假流程上线</h3>
<p>[李主管] 起草人填完送我审批，事假病假短的我这层就批了吧，3天以内的不用惊动总经理。</p>
<p>[周敏-HR] 对，事假/病假≤3天主管结束；其他的、或者超过7天、婚假产假陪产假这些，才往总经理和分管领导走。</p>
<p>[张交接] 表单里那个交接人字段，名字就叫"工作交接人"，从员工列表选，别叫代理人，容易和别的流程混。</p>
<p>[王运营] 病假要不要强制传就诊证明？这个还没定，先别写死。</p>
<p>[李主管] 调休别塞进来，调休走考勤系统的，跟请假两码事。</p>
<p>[周敏-HR] 退回就退回起草人重填；每级审批都要给同意/不同意的结论。</p>
</body></html>"""
    (RAW / "04_综合群聊记录_含请假讨论.doc").write_text(html, encoding="utf-8")


def gen_md() -> None:
    md = """# 请假流程 移动端、附件与角色说明

## 端侧
- 移动端与 PC 端均可发起请假申请。

## 附件
- 可上传证明材料（如病假就诊记录）。是否对病假强制要求，待 HR 内部确认（见上线问题清单）。

## 审批角色
- 部门主管：本部门部门主管，单人处理。
- 部门总经理：本部门部门总经理，单人处理。
- 条线分管领导：分管该部门所在条线的分管领导，单人处理。

每一级审批均需给出"同意 / 不同意"结论性意见；不同意退回起草。
"""
    (RAW / "05_移动端与附件角色说明.md").write_text(md, encoding="utf-8")


def gen_csv() -> None:
    with (RAW / "06_上线问题清单_待确认.csv").open("w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(["编号", "问题", "当前状态", "备注"])
        w.writerow(["ISSUE-01", "病假是否强制上传就诊证明、是否设为病假且≥3天必传", "待确认", "HR 内部有分歧，先不写死"])
        w.writerow(["ISSUE-02", "请假天数按自然日还是工作日、是否自动扣除周末节假日", "待确认", "财务与考勤口径未统一，先由申请人手工填"])
        w.writerow(["ISSUE-03", "各审批环节是否配置处理时限/超时催办、本期是否上线", "待确认", "HR 倾向加，本期未定"])
        w.writerow(["ISSUE-04", "年会报名表回收进度", "进行中", "与请假流程无关，勿混入"])


def gen_png() -> None:
    img = Image.new("RGB", (900, 620), "white")
    d = ImageDraw.Draw(img)
    title = cjk_font(30)
    body = cjk_font(24)
    d.rectangle([20, 20, 880, 600], outline="black", width=2)
    d.text((40, 44), "（扫描件）员工请假单 · 旧纸质版", fill="black", font=title)
    lines = [
        "姓名：______   部门：______   请假类型：______",
        "开始日期：______   结束日期：______   天数：____",
        "请假事由：____________________________",
        "工作交接人：______   联系电话：____________",
        "部门主管签字：______   部门总经理签字：______",
        "条线分管领导签字：______",
        "（仅作历史参考，不代表线上流程口径）",
    ]
    for i, line in enumerate(lines):
        d.text((40, 110 + i * 66), line, fill="black", font=body)
    img.save(RAW / "07_旧纸质请假单扫描_仅参考.png")


def gen_pdf() -> None:
    # 先做一张扫描感图片，再嵌入 PDF（无文本层 → 触发 requires_ocr）
    img = Image.new("RGB", (1000, 720), "white")
    d = ImageDraw.Draw(img)
    title = cjk_font(30)
    body = cjk_font(22)
    d.rectangle([30, 30, 970, 690], outline="black", width=2)
    d.text((60, 54), "（扫描件）请假登记台账 · 历史归档", fill="black", font=title)
    headers = "日期        姓名      类型     天数    审批结果"
    d.text((60, 120), headers, fill="black", font=body)
    d.line([60, 158, 940, 158], fill="gray", width=1)
    for i in range(6):
        y = 200 + i * 72
        d.text((60, y - 26), f"2026-0{i+1}-1{i}   员工{i+1}    事假     {i+1}      同意", fill="black", font=body)
        d.line([60, y, 940, y], fill="gray", width=1)
    img_path = RAW / "_tmp_ledger.png"
    img.save(img_path)
    c = canvas.Canvas(str(RAW / "08_旧请假台账扫描_仅参考.pdf"), pagesize=A4)
    w, h = A4
    c.drawImage(str(img_path), 40, h - 500, width=w - 80, height=440)
    c.showPage()
    c.save()
    img_path.unlink()


def gen_chat_png() -> None:
    """含真实信息的群聊截图（非'仅参考'噪音）——验证视觉转录能读入并影响输出。"""
    title = cjk_font(24)
    name = cjk_font(20)
    body = cjk_font(20)
    msgs = [
        ("周敏·HR", "10:02", "请假审批链路定了：起草→部门主管→部门总经理→条线分管领导，按类型和时长跳级。"),
        ("李主管", "10:05", "事假病假3天以内我这层就批了，别往上报总经理。"),
        ("王运营", "10:08", "病假到底要不要强制传就诊证明？还是没定，先别写死。"),
        ("周敏·HR", "10:10", "对，病假证明先留待确认。另外调休别塞进请假流程，走考勤系统。"),
    ]
    img = Image.new("RGB", (760, 120 + len(msgs) * 110), "#ededed")
    d = ImageDraw.Draw(img)
    d.rectangle([0, 0, 760, 56], fill="#4a4a4a")
    d.text((20, 16), "企业微信 · 请假流程上线讨论群", fill="white", font=title)
    y = 80
    for speaker, time, text in msgs:
        d.text((24, y), f"{speaker}  {time}", fill="#666666", font=name)
        d.rounded_rectangle([24, y + 30, 736, y + 92], radius=10, fill="white", outline="#d0d0d0")
        d.text((38, y + 44), text, fill="black", font=body)
        y += 110
    img.save(RAW / "10_群聊截图_审批与病假证明讨论.png")


def gen_noise_txt() -> None:
    text = """行政通知（与请假流程无关，仅作干扰项）

1. 2026 年度员工体检安排：将于下月开放预约，分批进行，具体名单另行通知。
2. 公司年会报名：年会定于 12 月，报名表已下发，请各部门于 6 月底前汇总报名人数。
3. 调休提醒：调休、倒休统一在考勤系统办理，不在请假流程内。

以上均非请假申请流程内容。
"""
    (RAW / "09_噪音_年会报名与体检通知.txt").write_text(text, encoding="utf-8")


def main() -> None:
    reset_dir()
    gen_docx()
    gen_email_txt()
    gen_xlsx()
    gen_doc_html()
    gen_md()
    gen_csv()
    gen_png()
    gen_pdf()
    gen_chat_png()
    gen_noise_txt()
    files = sorted(p.name for p in RAW.iterdir() if p.is_file())
    print(f"生成 {len(files)} 个 source 文件：")
    for f in files:
        print("  -", f)


if __name__ == "__main__":
    main()
