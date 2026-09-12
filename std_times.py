"""
Editing surface for the two standard-time tables.

The tables themselves live in scheduler_engine (the solver owns them); this module
only turns them into editable rows and back, and enforces the two rules that keep an
edited value usable by the model:

  * every figure lands on the 5-minute grid the MILP is built on
  * a whole-cycle time must leave at least one slot of actual loading after the
    30 minutes of weigh-in and weigh-out

No Streamlit here, so the conversions and the validation are unit-testable.
"""
import json

from scheduler_engine import (
    LORRY_STD_DEFAULT,
    LORRY_STD_FROM_ORIGINAL,
    SLOT,
    SRC_ACTUAL,
    SRC_STANDARD,
    STD_MAP,
    WEIGH_OVERHEAD,
    YUSEN_STD_DEFAULT,
    YUSEN_STD_FROM_ORIGINAL,
    StdEntry,
)

GROUP = "กลุ่ม / สินค้า"
BRACKET = "ขนาด"
ORIGINAL = "Standard เดิม (นาที)"
TOTAL = "ค่าที่ใช้ (นาที)"
BUFFER = "Buffer (นาที)"
SOURCE = "ที่มาของค่า"
REFERENCE = "อ้างอิง"

LORRY_COLS = [GROUP, BRACKET, ORIGINAL, TOTAL, BUFFER, SOURCE, REFERENCE]
YUSEN_COLS = [GROUP, ORIGINAL, TOTAL, BUFFER, SOURCE, REFERENCE]

BRACKET_ORDER = ["14MT", "20-22MT", "22-24MT", "24-25MT", "29MT"]
MIN_TOTAL = WEIGH_OVERHEAD + SLOT   # 35: weigh-in + weigh-out + one slot of loading

# Which products feed each LORRY standard group, for the reference column.
GROUP_PRODUCTS = {}
for _product, _group in STD_MAP.items():
    GROUP_PRODUCTS.setdefault(_group, []).append(_product)


def _reference(entry: StdEntry) -> str:
    bits = []
    if entry.p75_raw is not None:
        bits.append(f"P75 {entry.p75_raw:g} · n={entry.n:,}")
    if entry.note:
        bits.append(entry.note)
    return " · ".join(bits)


def lorry_rows(table=None, original=None, default_buffer=0) -> list:
    """One row per (standard group, weight bracket) that has a standard at all.

    `default_buffer` is what to show for an entry that carries no buffer of its own
    (the rollback table does that, so the config-level buffer still governs there)."""
    table = table or LORRY_STD_DEFAULT
    original = original or LORRY_STD_FROM_ORIGINAL
    rows = []
    for group in LORRY_STD_DEFAULT:
        for bracket in BRACKET_ORDER:
            entry = table.get(group, {}).get(bracket)
            if entry is None:
                continue
            std = original.get(group, {}).get(bracket)
            rows.append({
                GROUP: f"{group}  ({', '.join(GROUP_PRODUCTS.get(group, []))})",
                BRACKET: bracket,
                ORIGINAL: std.total_min if std else None,
                TOTAL: entry.total_min,
                BUFFER: (entry.buffer_min if entry.buffer_min is not None
                         else default_buffer),
                SOURCE: entry.source,
                REFERENCE: _reference(entry),
            })
    return rows


def yusen_rows(table=None, original=None, default_buffer=0) -> list:
    table = table or YUSEN_STD_DEFAULT
    original = original or YUSEN_STD_FROM_ORIGINAL
    rows = []
    for product, entry in table.items():
        std = original.get(product)
        rows.append({
            GROUP: product,
            ORIGINAL: std.total_min if std else None,
            TOTAL: entry.total_min,
            BUFFER: (entry.buffer_min if entry.buffer_min is not None
                     else default_buffer),
            SOURCE: entry.source,
            REFERENCE: _reference(entry),
        })
    return rows


def _clean(value, fallback, label, notes):
    """Snap a figure onto the 5-minute grid, telling the planner when it moved."""
    try:
        minutes = int(round(float(value)))
    except (TypeError, ValueError):
        notes.append(f"{label}: '{value}' ไม่ใช่ตัวเลข — ใช้ค่าเดิม {fallback} นาที")
        return fallback
    snapped = int(round(minutes / SLOT)) * SLOT
    if snapped != minutes:
        notes.append(f"{label}: {minutes} ไม่ใช่จำนวนเท่าของ {SLOT} — ปัดเป็น {snapped} นาที")
    return snapped


def rows_to_lorry(rows, reference=None) -> tuple:
    """Editor rows -> {group: {bracket: StdEntry}}. Returns (table, notes)."""
    reference = reference or LORRY_STD_DEFAULT
    table, notes = {}, []
    for row in rows:
        group = str(row.get(GROUP) or "").split("(")[0].strip()
        bracket = str(row.get(BRACKET) or "").strip()
        base = reference.get(group, {}).get(bracket)
        if base is None:
            continue
        label = f"{group} {bracket}"
        total = _clean(row.get(TOTAL), base.total_min, label, notes)
        if total < MIN_TOTAL:
            notes.append(f"{label}: {total} นาทีน้อยเกินไป — Weight-In+Out กิน "
                         f"{WEIGH_OVERHEAD} นาทีอยู่แล้ว ตั้งเป็น {MIN_TOTAL} นาที")
            total = MIN_TOTAL
        buffer_min = max(0, _clean(row.get(BUFFER), base.buffer_min or 0,
                                   f"{label} buffer", notes))
        table.setdefault(group, {})[bracket] = StdEntry(
            total, base.source, buffer_min, base.p75_raw, base.n, base.note)
    return table, notes


def rows_to_yusen(rows, reference=None) -> tuple:
    reference = reference or YUSEN_STD_DEFAULT
    table, notes = {}, []
    for row in rows:
        product = str(row.get(GROUP) or "").strip()
        base = reference.get(product)
        if base is None:
            continue
        total = _clean(row.get(TOTAL), base.total_min, product, notes)
        if total < MIN_TOTAL:
            notes.append(f"{product}: {total} นาทีน้อยเกินไป — ตั้งเป็น {MIN_TOTAL} นาที")
            total = MIN_TOTAL
        buffer_min = max(0, _clean(row.get(BUFFER), base.buffer_min or 0,
                                   f"{product} buffer", notes))
        table[product] = StdEntry(total, base.source, buffer_min,
                                  base.p75_raw, base.n, base.note)
    return table, notes


def changed_rows(table, baseline) -> list:
    """Which cells the planner moved away from the shipped defaults."""
    out = []
    for group, brackets in (table or {}).items():
        if isinstance(brackets, StdEntry):          # the Yusen shape
            base = (baseline or {}).get(group)
            if base and (brackets.total_min != base.total_min
                         or (brackets.buffer_min or 0) != (base.buffer_min or 0)):
                out.append((group, "", base, brackets))
            continue
        for bracket, entry in brackets.items():
            base = (baseline or {}).get(group, {}).get(bracket)
            if base and (entry.total_min != base.total_min
                         or (entry.buffer_min or 0) != (base.buffer_min or 0)):
                out.append((group, bracket, base, entry))
    return out


def to_json(lorry, yusen) -> str:
    """Everything needed to reproduce these tables elsewhere."""
    def dump(entry):
        return {"total_min": entry.total_min, "buffer_min": entry.buffer_min,
                "load_min": entry.load_min, "source": entry.source,
                "p75_raw": entry.p75_raw, "n": entry.n, "note": entry.note}

    payload = {
        "unit": "minutes, whole cycle (weigh-in + load + weigh-out)",
        "weigh_overhead_min": WEIGH_OVERHEAD,
        "slot_min": SLOT,
        "sources": {"actual": SRC_ACTUAL, "standard": SRC_STANDARD},
        "lorry": {g: {b: dump(e) for b, e in brackets.items()}
                  for g, brackets in (lorry or LORRY_STD_DEFAULT).items()},
        "yusen": {p: dump(e) for p, e in (yusen or YUSEN_STD_DEFAULT).items()},
    }
    return json.dumps(payload, ensure_ascii=False, indent=2)


def to_csv(lorry, yusen) -> str:
    lines = ["table,group,bracket,total_min,load_min,buffer_min,source,p75_raw,n,note"]

    def row(table_name, group, bracket, e):
        note = e.note.replace('"', "'")
        return (f'{table_name},{group},{bracket},{e.total_min},{e.load_min},'
                f'{e.buffer_min if e.buffer_min is not None else ""},"{e.source}",'
                f'{e.p75_raw if e.p75_raw is not None else ""},{e.n},"{note}"')

    for group, brackets in (lorry or LORRY_STD_DEFAULT).items():
        for bracket, entry in brackets.items():
            lines.append(row("LORRY", group, bracket, entry))
    for product, entry in (yusen or YUSEN_STD_DEFAULT).items():
        lines.append(row("ISO_TANK", product, "", entry))
    return "\n".join(lines) + "\n"
