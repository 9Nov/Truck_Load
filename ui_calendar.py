"""
Day-view calendar: one column per loading channel, time running down the page.

This is the same schedule the Plotly timeline shows, drawn the way the F7/F8
Truck Slot Board draws it - which is easier to read on the floor, because a
column is a bay and a vertical position is a clock time, so "what is channel B
doing at 14:00" is answered by looking at one spot instead of tracing a bar.

Each column carries its own header (what the bay can take, how busy it is, when
it frees up) and each event carries the three things a planner asks about a
truck: when, who, and how long. Idle stretches are drawn explicitly as hatched
"ว่าง N น." blocks so wasted bay time is visible rather than implied by a gap.

Pure HTML/CSS - no JS, no chart library. Rendered through st.markdown.
"""
import streamlit as st

from scheduler_engine import CHANNEL_ELIGIBILITY, CHANNELS, min_to_hhmm
from ui_theme import FAMILY_COLORS, PRODUCT_COLORS, esc as _esc

# Which products may use each bay, derived from the eligibility table so the
# header chips can never drift from the constraint the solver actually enforces.
CHANNEL_PRODUCTS = {c: [p for p, chans in CHANNEL_ELIGIBILITY.items() if c in chans]
                    for c in CHANNELS}

FAMILY_OF = {"MAA1": "MAA", "MAA2": "MAA", "MAA3": "MAA", "MMA1": "MMA", "MMA2": "MMA",
             "i-BMA": "i-BMA", "n-BMA1": "n-BMA", "n-BMA2": "n-BMA"}

GAP_MIN = 30          # only gaps this long are worth drawing as wasted bay time
SHORT_EVENT_PX = 52   # below this, the third line is dropped so text never spills

_CSS = """
<style>
.tmcal-scroll{ max-height:760px; overflow:auto; border:1px solid var(--line);
  border-radius:12px; background:var(--surface-2); }
.tmcal{ display:grid; column-gap:8px; row-gap:0; padding:0 10px 12px 0; min-width:640px; }

/* ---- column header ---- */
.tmcal .colh{ position:sticky; top:0; z-index:6; background:var(--surface);
  border:1px solid var(--line); border-bottom:0; border-radius:10px 10px 0 0;
  padding:9px 11px; display:grid; gap:5px; align-content:start; }
.tmcal .colh .r1{ display:flex; align-items:baseline; gap:9px; }
.tmcal .colh .big{ font-family:var(--mono); font-weight:700; font-size:20px; line-height:1; }
.tmcal .colh .k{ margin-left:auto; text-align:right; font-size:11px; color:var(--muted);
  line-height:1.35; }
.tmcal .colh .k b{ font-family:var(--mono); color:var(--ink-2); }
.tmcal .chips{ display:flex; gap:4px; flex-wrap:wrap; }
.tmcal .pc{ font-family:var(--mono); font-weight:600; font-size:10px; line-height:1;
  padding:3px 5px; border-radius:4px; border:1px solid currentColor; }
.tmcal .st{ font-family:var(--mono); font-weight:600; font-size:11.5px; line-height:1.3; }
.tmcal .st.free{ color:var(--ok); } .tmcal .st.busy{ color:var(--accent); }
.tmcal .meta{ font-size:11.5px; color:var(--muted); line-height:1.3; }
.tmcal .meta b{ font-family:var(--mono); color:var(--ink-2); font-weight:600; }

/* ---- axis + lane ---- */
.tmcal .axh{ position:sticky; top:0; z-index:6; background:var(--surface-2); }
.tmcal .axis{ position:relative; }
.tmcal .axis .tk{ position:absolute; right:9px; transform:translateY(-50%);
  font-family:var(--mono); font-weight:600; font-size:11px; color:var(--faint); }
.tmcal .lane{ position:relative; background:var(--surface-2); border:1px solid var(--line);
  border-top:0; border-radius:0 0 10px 10px; overflow:hidden; }
.tmcal .hr{ position:absolute; left:0; right:0; border-top:1px solid var(--line-2); }
.tmcal .hr.half{ border-top-style:dotted; }
.tmcal .brk{ position:absolute; left:0; right:0; background:#8497A4; opacity:.16; }
.tmcal .brklb{ position:absolute; left:6px; font-size:10px; color:var(--muted); }
.tmcal .nowl{ position:absolute; left:0; right:0; border-top:2px solid var(--crit); z-index:7; }
.tmcal .nowl::before{ content:""; position:absolute; left:-3px; top:-5px; width:8px; height:8px;
  border-radius:50%; background:var(--crit); }

/* ---- bay closed for maintenance ---- */
.tmcal .down{ position:absolute; left:0; right:0; overflow:hidden;
  background:repeating-linear-gradient(-45deg,#8497A422,#8497A422 6px,#8497A444 6px,#8497A444 12px);
  border-top:2px solid var(--muted); border-bottom:2px solid var(--muted); }
.tmcal .downlb{ position:absolute; left:7px; right:6px; font-family:var(--mono); font-weight:700;
  font-size:10.5px; color:var(--ink-2); text-align:center; overflow:hidden; }

/* ---- idle stretch ---- */
.tmcal .gap{ position:absolute; left:7px; right:6px; border:1px dashed var(--crit-line);
  border-radius:7px; color:var(--crit); font-family:var(--mono); font-weight:600; font-size:10.5px;
  display:grid; place-items:center; text-align:center; overflow:hidden; padding:2px;
  background:repeating-linear-gradient(-45deg,transparent,transparent 5px,
             var(--crit-soft) 5px,var(--crit-soft) 10px); }

/* ---- one truck ---- */
.tmcal .ev{ position:absolute; left:7px; right:6px; border-radius:7px; padding:5px 8px;
  overflow:hidden; background:var(--surface); border:1px solid var(--line);
  border-left:4px solid var(--accent); box-shadow:0 1px 2px rgba(14,23,32,.07);
  display:grid; gap:1px; align-content:start; }
.tmcal .ev .e1{ font-family:var(--mono); font-weight:700; font-size:11.5px; line-height:1.25;
  white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }
.tmcal .ev .e2{ font-size:11px; line-height:1.25; color:var(--ink-2);
  white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }
.tmcal .ev .e3{ font-family:var(--mono); font-weight:600; font-size:10px; line-height:1.25;
  color:var(--muted); white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }
.tmcal .ev.short .e3{ display:none; }
.tmcal .ev .tag{ position:absolute; right:5px; top:5px; font-size:9px; font-weight:700;
  padding:2px 6px; border-radius:4px; letter-spacing:.02em; }
.tmcal .ev.s-load{ border-color:var(--accent); border-width:2px; background:var(--accent-soft); }
.tmcal .ev.s-load .tag{ background:var(--accent); color:#fff; }
.tmcal .ev.s-here{ border-color:var(--ok); border-width:2px; background:var(--ok-soft); }
.tmcal .ev.s-here .tag{ background:var(--ok); color:#fff; }
.tmcal .ev.s-done{ opacity:.5; }
.tmcal .ev.s-done .tag{ background:var(--surface-3); color:var(--muted); }
.tmcal .ev.s-eta .tag{ background:var(--surface-3); color:var(--muted); }
.tmcal .ev.s-blind{ border-style:dashed; background:transparent; }
.tmcal .ev.s-blind .tag{ background:transparent; color:var(--muted); border:1px dashed var(--line); }
.tmcal .ev.s-late{ border-color:var(--crit); border-width:2px; background:var(--crit-soft); }
.tmcal .ev.s-late .tag{ background:var(--crit); color:#fff; }
.tmcal .ev .lock{ color:var(--warn); }
</style>
"""


def _fmt_min(m):
    return f"{int(round(m))}"


def _channel_head(ch, rows, summary, now_min, cfg=None):
    """What this bay can take, how hard it worked, and when it is free again."""
    chips = "".join(
        f'<span class="pc" style="color:{PRODUCT_COLORS[p]};'
        f'background:{PRODUCT_COLORS[p]}14">{_esc(p)}</span>'
        for p in CHANNEL_PRODUCTS[ch])

    util = next((s["Utilisation %"] for s in summary if s["Channel"] == ch), 0)
    trips = len(rows)

    idle = 0
    for a, b in zip(rows, rows[1:]):
        idle += max(0, b["start"] - a["end"])

    if now_min is not None:
        current = next((r for r in rows if r["start"] <= now_min < r["end"]), None)
        if current:
            state = (f'<div class="st busy">ไม่ว่างถึง {current["end_hhmm"]} · '
                     f'{_esc(current["product"])}</div>')
        else:
            nxt = next((r for r in rows if r["start"] > now_min), None)
            state = ('<div class="st free">ว่างอยู่ตอนนี้'
                     + (f' · คิวหน้า {nxt["start_hhmm"]}' if nxt else " · หมดคิวแล้ว")
                     + "</div>")
    elif rows:
        state = (f'<div class="st busy">{rows[0]["start_hhmm"]} – {rows[-1]["end_hhmm"]}</div>')
    else:
        state = '<div class="st free">ไม่มีคิวลงช่องนี้</div>'

    down = cfg.blackout_minutes(ch) if cfg is not None else 0
    down_html = (f'<div class="meta" style="color:var(--crit)">⛔ ปิดซ่อมวันนี้ '
                 f'<b style="color:var(--crit)">{_fmt_min(down)}</b> นาที</div>') if down else ""
    return (
        f'<div class="colh">'
        f'<div class="r1"><span class="big">{_esc(ch)}</span>'
        f'<span class="k">ใช้ช่อง <b>{util}%</b><br>ของเวลาทำงาน</span></div>'
        f'<div class="chips">{chips}</div>'
        f'{state}'
        f'<div class="meta"><b>{trips}</b> เที่ยว · ช่องว่างระหว่างคิว <b>{_fmt_min(idle)}</b> น.</div>'
        f'{down_html}'
        f'</div>')


def _event_state(r, now_min):
    """(css class, tag text) — says what is true about this truck right now."""
    if now_min is None:
        if r["is_fixed"]:
            return "s-eta", "ล็อกเวลา"
        return "", ""
    if r.get("is_locked"):
        if r["end"] <= now_min:
            return "s-done", "เสร็จแล้ว"
        return "s-load", "กำลังโหลด"
    eta = r.get("earliest")
    if eta is None:
        return "s-blind", "ไม่รู้ตำแหน่ง"
    if eta <= now_min:
        return "s-here", "รถรออยู่"
    if eta > r["start"]:
        return "s-late", f"ถึง {min_to_hhmm(eta)}"
    return "s-eta", f"ถึง {min_to_hhmm(eta)}"


def _event(r, top, height, now_min):
    cls, tag = _event_state(r, now_min)
    short = " short" if height < SHORT_EVENT_PX else ""
    colour = PRODUCT_COLORS.get(r["product"], "#0B5FA5")
    lock = '<span class="lock">🔒</span> ' if r["is_fixed"] else ""
    margin = f' (+{r["margin"]})' if r.get("margin") else ""
    tag_html = f'<span class="tag">{_esc(tag)}</span>' if tag else ""
    done_mark = " ✓" if cls == "s-done" else ""
    return (
        f'<div class="ev {cls}{short}" style="top:{top:.1f}px;height:{height:.1f}px;'
        f'border-left-color:{colour}">{tag_html}'
        f'<div class="e1">{lock}{r["start_hhmm"]}–{r["end_hhmm"]} '
        f'<span style="color:{colour}">{_esc(r["product"])}</span>{done_mark}</div>'
        f'<div class="e2">{_esc(r.get("company") or "—")}</div>'
        f'<div class="e3">{_esc(r["id"])} · {r["duration_min"]} น.{margin}</div>'
        f'</div>')


def render(schedule, cfg, summary, *, now_min=None, px_per_hour: int = 62):
    """Draw the whole board. `schedule` and `summary` come straight from the solver."""
    ppm = px_per_hour / 60.0
    start, end = cfg.window_start_min, cfg.window_end_min
    height = (end - start) * ppm

    def y(minute):
        return (minute - start) * ppm

    # --- shared lane furniture: hour rules, half-hour dots, breaks, now line ---
    rules = []
    hour = start - (start % 60) + (60 if start % 60 else 0)
    while hour <= end:
        rules.append(f'<div class="hr" style="top:{y(hour):.1f}px"></div>')
        if hour + 30 <= end:
            rules.append(f'<div class="hr half" style="top:{y(hour + 30):.1f}px"></div>')
        hour += 60
    for bs, be in cfg.breaks_min:
        if be <= start or bs >= end:
            continue
        top, hgt = y(max(bs, start)), (min(be, end) - max(bs, start)) * ppm
        rules.append(f'<div class="brk" style="top:{top:.1f}px;height:{hgt:.1f}px"></div>')
        rules.append(f'<div class="brklb" style="top:{top + 1:.1f}px">พัก {min_to_hhmm(bs)}</div>')
    if now_min is not None and start <= now_min <= end:
        rules.append(f'<div class="nowl" style="top:{y(now_min):.1f}px"></div>')
    furniture = "".join(rules)

    # --- time axis ---
    ticks = []
    hour = start - (start % 60) + (60 if start % 60 else 0)
    while hour <= end:
        # the first label sits on the container's top edge, where translateY(-50%)
        # would push half of it out of view
        nudge = ' transform:none;' if y(hour) < 1 else ""
        ticks.append(f'<span class="tk" style="top:{y(hour):.1f}px;{nudge}">'
                     f'{min_to_hhmm(hour)}</span>')
        hour += 60
    axis_head = '<div class="axh"></div>'
    axis_body = f'<div class="axis" style="height:{height:.1f}px">{"".join(ticks)}</div>'

    heads, lanes = [], []
    for ch in CHANNELS:
        rows = sorted([r for r in schedule if r["channel"] == ch], key=lambda r: r["start"])
        heads.append(_channel_head(ch, rows, summary, now_min, cfg))

        body = [furniture]
        for bs, be, why in cfg.blackouts_for(ch):
            top, hgt = y(bs), (be - bs) * ppm
            body.append(f'<div class="down" style="top:{top:.1f}px;height:{hgt:.1f}px"></div>')
            body.append(f'<div class="downlb" style="top:{top + max(1, hgt / 2 - 7):.1f}px">'
                        f'⛔ ปิดช่อง {_fmt_min(be - bs)} น.'
                        + (f'<br>{_esc(why)}' if why and hgt > 34 else "") + '</div>')
        for a, b in zip(rows, rows[1:]):
            gap = b["start"] - a["end"]
            # a gap that is only the lunch/dinner break is not wasted bay time
            closed = [(x, y_) for x, y_, _ in cfg.blackouts_for(ch)]
            overlap = sum(max(0, min(be, b["start"]) - max(bs, a["end"]))
                          for bs, be in list(cfg.breaks_min) + closed)
            if gap - overlap >= GAP_MIN:
                body.append(
                    f'<div class="gap" style="top:{y(a["end"]):.1f}px;'
                    f'height:{gap * ppm - 2:.1f}px">ว่าง {_fmt_min(gap)} น.</div>')
        for r in rows:
            body.append(_event(r, y(r["start"]), max(18.0, (r["end"] - r["start"]) * ppm - 2),
                               now_min))
        lanes.append(f'<div class="lane" style="height:{height:.1f}px">{"".join(body)}</div>')

    # grid row 1 = axis spacer + column headers, row 2 = time axis + lanes, so the
    # headers and the lanes stay aligned however tall the headers grow
    grid = (f'<div class="tmcal" style="grid-template-columns:56px '
            f'repeat({len(CHANNELS)},minmax(190px,1fr))">'
            + axis_head + "".join(heads)
            + axis_body + "".join(lanes) + "</div>")

    st.markdown(_CSS.replace("var(--mono)", '"IBM Plex Mono",ui-monospace,Menlo,monospace')
                + f'<div class="tmcal-scroll">{grid}</div>', unsafe_allow_html=True)


def legend_entries():
    return [(colour, name) for name, colour in FAMILY_COLORS.items()]
