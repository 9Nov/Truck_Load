"""
Builds the Excel upload template for the TMMA Loading Channel Scheduler.

The workbook's FIRST sheet ("DO List") is what the app reads, so it holds the six
expected headers and nothing else - the planner types straight into it.  The other
sheets are reference material for whoever fills it in.

Used two ways:
  * app.py imports build_template_bytes() for the in-app download button
  * `py do_template.py` writes DO_Template.xlsx next to this file
"""
import io
from pathlib import Path

from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.datavalidation import DataValidation

from scheduler_engine import (
    CHANNEL_ELIGIBILITY,
    LOAD_LORRY,
    PRODUCTS,
    STD_MAP,
    TRANSPORT_COMPANIES,
    WEIGH_IN_LORRY,
    WEIGH_OUT_LORRY,
    YUSEN_STD_TOTAL,
)

SHEET_INPUT = "DO List"
SHEET_EXAMPLE = "Example"
SHEET_GUIDE = "Guide"
SHEET_LISTS = "Lists"

COLUMNS = [
    ("DO No.", 14, "เลขที่ DO — ห้ามซ้ำ (บังคับ)"),
    ("Product", 13, "เลือกจาก dropdown (บังคับ)"),
    ("Volume (ton)", 14, "ปริมาณเป็นตัน (บังคับ)"),
    ("Transport Co.", 16, "เลือกจาก dropdown — Yusen ใช้ตาราง standard คนละชุด (ดูท้าย sheet นี้)"),
    ("Requested Time", 18, "HH:MM = hard deadline ต้องโหลดเวลานั้นเป๊ะ · เว้นว่าง = ยืดหยุ่น"),
    ("Margin (min)", 14, "เวลาเผื่อเฉพาะ DO นี้ (นาที) เว้นว่าง = 0"),
    ("Plan truck", 14, "ทะเบียน/รหัสรถ (ไม่บังคับ) — ใช้จับคู่รถคันเดียวกันที่วิ่งหลายรอบ เว้นว่างได้"),
]
DATA_ROWS = 200  # rows pre-formatted with dropdowns / validation

EXAMPLE_ROWS = [
    ("DO1001", "MMA2", 29.0, "ศรีไทย", "08:00", 0, ""),
    ("DO1002", "MAA1", 22.0, "SV", "", 0, ""),
    ("DO1003", "MAA3", 14.0, "VIV", "10:30", 5, ""),
    ("DO1004", "i-BMA", 14.0, "Yusen", "", 0, ""),
    ("DO1005", "MMA1", 25.0, "Yusen", "", 10, ""),
    ("DO1006", "n-BMA1", 14.0, "VIV", "14:00", 0, "70-1234"),
]

HEAD_FILL = PatternFill("solid", fgColor="1F3864")
HEAD_FONT = Font(bold=True, color="FFFFFF", size=11)
TITLE_FONT = Font(bold=True, size=13, color="1F3864")
SUB_FONT = Font(bold=True, size=11, color="1F3864")
THIN = Side(style="thin", color="BFBFBF")
BORDER = Border(left=THIN, right=THIN, top=THIN, bottom=THIN)


def _write_header(ws, note_row=True):
    for i, (name, width, note) in enumerate(COLUMNS, start=1):
        cell = ws.cell(row=1, column=i, value=name)
        cell.fill = HEAD_FILL
        cell.font = HEAD_FONT
        cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
        cell.border = BORDER
        ws.column_dimensions[get_column_letter(i)].width = width
        if note_row:
            cell.comment = None
    ws.row_dimensions[1].height = 28
    ws.freeze_panes = "A2"
    ws.auto_filter.ref = f"A1:{get_column_letter(len(COLUMNS))}1"


def _add_validations(ws, last_row):
    lists = f"'{SHEET_LISTS}'"
    dv_product = DataValidation(
        type="list", formula1=f"={lists}!$A$2:$A${1 + len(PRODUCTS)}",
        allow_blank=True, showDropDown=False, errorTitle="Product ไม่ถูกต้อง",
        error="เลือกจากรายการเท่านั้น: " + ", ".join(PRODUCTS))
    dv_company = DataValidation(
        type="list", formula1=f"={lists}!$B$2:$B${1 + len(TRANSPORT_COMPANIES)}",
        allow_blank=True, showDropDown=False, errorTitle="Transport Co. ไม่ถูกต้อง",
        error="เลือกจากรายการเท่านั้น: " + ", ".join(TRANSPORT_COMPANIES))
    dv_volume = DataValidation(
        type="decimal", operator="between", formula1=0.1, formula2=60,
        allow_blank=True, errorTitle="Volume ไม่ถูกต้อง", error="ใส่ตัวเลข 0.1 - 60 ตัน")
    dv_margin = DataValidation(
        type="whole", operator="between", formula1=0, formula2=120,
        allow_blank=True, errorTitle="Margin ไม่ถูกต้อง", error="ใส่จำนวนเต็ม 0 - 120 นาที")

    for dv, col in ((dv_product, "B"), (dv_volume, "C"), (dv_company, "D"), (dv_margin, "F")):
        ws.add_data_validation(dv)
        dv.add(f"{col}2:{col}{last_row}")

    for row in range(2, last_row + 1):
        ws.cell(row=row, column=3).number_format = "0.0"
        ws.cell(row=row, column=5).number_format = "@"   # keep 08:00 as text, not a date
        ws.cell(row=row, column=6).number_format = "0"
        ws.cell(row=row, column=7).number_format = "@"   # keep a truck plate like "70-1234" as text


def _build_lists_sheet(wb):
    ws = wb.create_sheet(SHEET_LISTS)
    ws["A1"] = "Product"
    ws["B1"] = "Transport Co."
    for i, p in enumerate(PRODUCTS, start=2):
        ws.cell(row=i, column=1, value=p)
    for i, c in enumerate(TRANSPORT_COMPANIES, start=2):
        ws.cell(row=i, column=2, value=c)
    ws.column_dimensions["A"].width = 14
    ws.column_dimensions["B"].width = 16
    ws.sheet_state = "hidden"
    return ws


def _build_guide_sheet(wb):
    ws = wb.create_sheet(SHEET_GUIDE)
    ws.column_dimensions["A"].width = 22
    ws.column_dimensions["B"].width = 72
    r = 1

    def line(a="", b="", font=None):
        nonlocal r
        ws.cell(row=r, column=1, value=a).font = font or Font(bold=bool(a and not b))
        if b:
            cell = ws.cell(row=r, column=2, value=b)
            cell.alignment = Alignment(wrap_text=True, vertical="top")
        r += 1

    ws.cell(row=r, column=1, value="วิธีกรอก DO Template").font = TITLE_FONT
    r += 2
    for name, _w, note in COLUMNS:
        line(name, note)
    r += 1

    ws.cell(row=r, column=1, value="กติกาสำคัญ").font = SUB_FONT
    r += 1
    line("Requested Time", "ถ้ากรอก = hard deadline ระบบจะไม่เลื่อนให้เอง ถ้าชนกันจะแจ้งเป็น conflict "
                          "ให้ผู้วางแผนตัดสินใจ · ถ้าเว้นว่าง ระบบจะเลือกเวลาที่ดีที่สุดให้")
    line("", "เวลาต้องลงตัวทุก 5 นาที (เช่น 08:00, 08:05) ถ้าไม่ลงตัวระบบจะปัดใกล้สุดและแจ้งเตือน")
    line("Margin (min)", "บวกเพิ่มเฉพาะ DO นี้ ต่อจาก Buffer ของแถวนั้น "
                         "(ตั้งในแอป หน้า ⚙️ เวลามาตรฐาน คอลัมน์ Buffer)")
    line("แถวว่าง", "เว้นว่างได้ ระบบข้ามให้อัตโนมัติ · DO No. ห้ามซ้ำกัน")
    line("Sheet ที่อ่าน", f"แอปอ่านเฉพาะ sheet แรก ('{SHEET_INPUT}') เท่านั้น sheet อื่นเป็นข้อมูลอ้างอิง")
    r += 1

    ws.cell(row=r, column=1, value="Product ใช้ช่องโหลดไหนได้").font = SUB_FONT
    r += 1
    for p in PRODUCTS:
        line(p, "Channel " + " / ".join(CHANNEL_ELIGIBILITY[p]))
    line("", "Channel D ไม่อยู่ในระบบนี้ (ใช้กับ Truck Drum / Dry Container เท่านั้น)")
    r += 1

    ws.cell(row=r, column=1, value="เวลามาตรฐาน (LORRY)").font = SUB_FONT
    r += 1
    line("โครงสร้าง", f"Weight-In {WEIGH_IN_LORRY} นาที + Load (ตาม product × ขนาด) + "
                     f"Weight-Out {WEIGH_OUT_LORRY} นาที (+ Buffer ของแถว + Margin)")
    line("Bracket จาก Volume", "≤14 → 14MT · ≤22 → 20-22MT · ≤24 → 22-24MT · ≤25 → 24-25MT · >25 → 29MT")
    r += 1

    brackets = ["14MT", "20-22MT", "22-24MT", "24-25MT", "29MT"]
    ws.cell(row=r, column=1, value="Load (นาที)").font = SUB_FONT
    for j, b in enumerate(brackets, start=2):
        c = ws.cell(row=r, column=j, value=b)
        c.font = Font(bold=True)
        c.alignment = Alignment(horizontal="center")
        ws.column_dimensions[get_column_letter(j)].width = 11
    ws.column_dimensions["B"].width = 11
    r += 1
    for std_name, table in LOAD_LORRY.items():
        products = [p for p in PRODUCTS if STD_MAP[p] == std_name]
        ws.cell(row=r, column=1, value=f"{std_name}  ({', '.join(products)})")
        for j, b in enumerate(brackets, start=2):
            v = table.get(b)
            c = ws.cell(row=r, column=j, value=v if v is not None else "-")
            c.alignment = Alignment(horizontal="center")
        r += 1
    ws.column_dimensions["A"].width = 34
    line("", "")
    line("ช่องว่าง '-'", "หมายถึงไม่มีเวลามาตรฐานสำหรับขนาดนั้น — DO แบบนั้นจะจัดคิวไม่ได้")
    r += 1

    ws.cell(row=r, column=1, value="Yusen (ISO Tank) — ใช้คนละตาราง").font = SUB_FONT
    r += 1
    line("สำคัญ", "ถ้า Transport Co. = Yusen ระบบจะใช้เวลามาตรฐานชุดนี้แทนตาราง LORRY ด้านบน "
                 "เป็นเวลารวมทั้งรอบ (ตั้งแต่รถถึง Station จนออก) และ ไม่ขึ้นกับขนาดน้ำหนัก")
    line("", "Weight-In 15 + Load (Standard − 30) + Weight-Out 15 นาที "
             "และ Buffer ตั้งเป็น 0 เพราะ standard ชุดนี้เผื่อเวลามาในตัวแล้ว")
    for product in PRODUCTS:
        line(product, f"{YUSEN_STD_TOTAL[product]} นาที (ทุกขนาด)")
    return ws


def _build_example_sheet(wb):
    ws = wb.create_sheet(SHEET_EXAMPLE)
    _write_header(ws, note_row=False)
    for i, row in enumerate(EXAMPLE_ROWS, start=2):
        for j, value in enumerate(row, start=1):
            cell = ws.cell(row=i, column=j, value=value)
            cell.border = BORDER
            if j == 3:
                cell.number_format = "0.0"
            if j == 5:
                cell.number_format = "@"
                cell.alignment = Alignment(horizontal="center")
            if j == 7:
                cell.number_format = "@"
                cell.alignment = Alignment(horizontal="center")
    # the note sits outside the data columns so this sheet stays uploadable as-is
    note = ws.cell(row=2, column=len(COLUMNS) + 2,
                   value="ตัวอย่างการกรอก — DO1001 / DO1003 / DO1006 เป็น hard deadline "
                         "ส่วนแถวที่เว้น Requested Time ไว้ ระบบจะจัดเวลาให้เอง")
    note.font = Font(italic=True, color="7F7F7F")
    return ws


def build_template_bytes() -> bytes:
    wb = Workbook()
    ws = wb.active
    ws.title = SHEET_INPUT
    _write_header(ws)
    _add_validations(ws, DATA_ROWS + 1)
    _build_example_sheet(wb)
    _build_guide_sheet(wb)
    _build_lists_sheet(wb)
    wb.active = 0

    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


if __name__ == "__main__":
    out = Path(__file__).with_name("DO_Template.xlsx")
    out.write_bytes(build_template_bytes())
    print(f"wrote {out} ({out.stat().st_size:,} bytes)")
