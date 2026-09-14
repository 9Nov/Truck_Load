"""
Click-to-reschedule detail card - the replacement for the calendar's old drag-
and-drop. A planner picks one flexible DO, sees a card that mirrors the F7/F8
"scheduling detail" mock-up (route line, current plan, a table of every other
channel/time window that DO could still fit into), clicks the window they want,
confirms in a modal, and the solver re-places everything from that constraint -
exactly like the drag feature did, just triggered by a click instead of a drag.

Only flexible DOs (not a hard deadline, not already loading/frozen) are offered:
those are the only ones a planner is allowed to move at all.
"""
import streamlit as st

from scheduler_engine import CHANNEL_ELIGIBILITY, SLOT, WEIGH_IN_LORRY, WEIGH_OUT_LORRY, min_to_hhmm
from ui_theme import esc as _esc

RESULT_KEY = "_reschedule_result"


def _free_windows(job, schedule, cfg):
    """Every (channel, start, end) gap this DO's own duration still fits into,
    across every channel it is eligible for -- computed with the DO's own current
    slot removed, so its present position shows up as part of a wider free gap
    rather than blocking itself."""
    duration = job["duration_min"]
    windows = []
    for ch in CHANNEL_ELIGIBILITY.get(job["product"], []):
        blocked = [(r["start"], r["end"]) for r in schedule
                   if r["channel"] == ch and r["id"] != job["id"]]
        blocked += list(cfg.breaks_min)
        blocked += [(bs, be) for bs, be, _ in cfg.blackouts_for(ch)]
        blocked.sort()

        cursor, gaps = cfg.window_start_min, []
        for bs, be in blocked:
            bs, be = max(bs, cfg.window_start_min), min(be, cfg.window_end_min)
            if be <= bs:
                continue
            if bs > cursor:
                gaps.append((cursor, bs))
            cursor = max(cursor, be)
        if cursor < cfg.window_end_min:
            gaps.append((cursor, cfg.window_end_min))

        for gs, ge in gaps:
            if ge - gs >= duration:
                windows.append({"channel": ch, "start": gs, "end": ge})
    windows.sort(key=lambda w: (w["start"], w["channel"]))
    return windows


def _breakdown_note(job):
    load_min = job["duration_min"] - WEIGH_IN_LORRY - WEIGH_OUT_LORRY - (job.get("margin") or 0)
    note = f"ชั่งเข้า {WEIGH_IN_LORRY} + โหลด {load_min} ({job['bracket']}, {job.get('std_source', '')})"
    if job.get("margin"):
        note += f" + เผื่อเวลา {job['margin']}"
    note += f" + ชั่งออก {WEIGH_OUT_LORRY} = {job['duration_min']} นาที"
    return note


def _render_card(job, windows):
    """The light-blue detail card: title, route line, current plan, breakdown."""
    current = (f'<b>ตอนนี้: เข้าช่อง {_esc(job["channel"])} เวลา {_esc(job["start_hhmm"])} '
               f'→ จะเสร็จ {_esc(job["end_hhmm"])}</b>')

    def row(i, w):
        is_current = w["channel"] == job["channel"] and w["start"] <= job["start"] < w["end"]
        bg = "#EAF2FB" if is_current else ("#FAFBFC" if i % 2 else "#FFFFFF")
        cell = 'style="padding:6px 10px"'
        return (f'<tr style="background:{bg}">'
                f'<td {cell}>{_esc(job["product"])}</td><td {cell}>ช่อง {_esc(w["channel"])}</td>'
                f'<td {cell}>{job["duration_min"]} นาที</td>'
                f'<td {cell}>{_esc(min_to_hhmm(w["start"]))}–{_esc(min_to_hhmm(w["end"]))}</td></tr>')

    rows_html = "".join(row(i, w) for i, w in enumerate(windows))
    st.markdown(
        '<div style="background:var(--accent-soft);border:1px solid var(--accent-line);'
        'border-radius:12px;padding:14px 16px;margin-bottom:10px">'
        f'<div style="font-weight:700;font-size:16px;color:var(--ink)">'
        f'{_esc(job["id"])} · {_esc(job["product"])} · {_esc(job.get("company") or "-")}</div>'
        f'<div style="font-size:12.5px;color:var(--muted);margin:2px 0 8px">'
        f'{_esc(job.get("bracket", ""))} · {job["volume"]:g} ตัน · {_esc(job.get("std_source", ""))}</div>'
        f'<div style="font-size:13.5px;margin-bottom:4px">{current}</div>'
        f'<div style="font-size:11.5px;color:var(--muted);margin-bottom:10px">{_esc(_breakdown_note(job))}</div>'
        '<table style="width:100%;border-collapse:collapse;background:var(--surface);'
        'border-radius:8px;overflow:hidden;font-size:12.5px">'
        '<tr style="background:var(--surface-3);text-align:left">'
        '<th style="padding:6px 10px">สินค้า</th><th style="padding:6px 10px">ช่องที่โหลดได้</th>'
        '<th style="padding:6px 10px">ตารางเวลามาตรฐาน</th>'
        '<th style="padding:6px 10px">Slot เวลาที่ว่าง</th></tr>'
        f'{rows_html}</table></div>', unsafe_allow_html=True)


@st.dialog("ยืนยันการย้ายคิว")
def _confirm_dialog(job, window, cfg):
    st.write(f"**ตอนนี้:** ช่อง {job['channel']} · {job['start_hhmm']}–{job['end_hhmm']}")
    st.write(f"**ย้ายไป:** ช่อง {window['channel']} · ว่างระหว่าง "
             f"{min_to_hhmm(window['start'])}–{min_to_hhmm(window['end'])}")

    duration = job["duration_min"]
    latest_start = window["end"] - duration
    options = list(range(window["start"], latest_start + 1, SLOT)) or [window["start"]]
    labels = [f"{min_to_hhmm(t)}–{min_to_hhmm(t + duration)}" for t in options]
    default_idx = 0
    picked_label = st.selectbox("เลือกเวลาที่จะเริ่มโหลด", labels, index=default_idx,
                                help="เลือกเวลาเริ่มที่ต้องการภายในช่วงที่ว่าง — ระบบจะจองเต็มรอบให้อัตโนมัติ")
    picked_start = options[labels.index(picked_label)]

    c1, c2 = st.columns(2)
    if c1.button("✅ ยืนยัน", type="primary", width="stretch"):
        st.session_state[RESULT_KEY] = {"do_id": job["id"], "channel": window["channel"],
                                        "start_min": picked_start}
        st.rerun()
    if c2.button("✖️ ยกเลิก", width="stretch"):
        st.rerun()


def render(job, schedule, cfg):
    """Draw the card + clickable slot buttons for one DO. Returns
    {"do_id", "channel", "start_min"} the instant a confirm goes through
    (consume it once, like the old drag result), else None."""
    windows = _free_windows(job, schedule, cfg)
    _render_card(job, windows)

    if not windows:
        st.caption("ไม่มีช่วงว่างอื่นให้ย้ายแล้ววันนี้")
        return None

    st.caption("👆 คลิกช่วงที่ต้องการเพื่อเปิดหน้าต่างยืนยัน")
    cols = st.columns(min(4, len(windows)) or 1)
    for i, w in enumerate(windows):
        is_current = w["channel"] == job["channel"] and w["start"] <= job["start"] < w["end"]
        label = f"ช่อง {w['channel']} · {min_to_hhmm(w['start'])}–{min_to_hhmm(w['end'])}"
        if is_current:
            label = "📍 " + label
        if cols[i % len(cols)].button(label, key=f"slot_{job['id']}_{i}",
                                      type="primary" if is_current else "secondary",
                                      width="stretch"):
            _confirm_dialog(job, w, cfg)

    result = st.session_state.pop(RESULT_KEY, None)
    return result
