"""
Shared Streamlit helpers: DO-table handling, Gantt rendering, result tables and
the Excel export.  Imported by both pages in app.py (plan / re-plan) so the two
views always show the same numbers in the same shape.
"""
import datetime as dt
import io

import pandas as pd
import plotly.express as px
import streamlit as st

from gps_eta import STATUS_UNKNOWN
from ui_theme import (
    CRIT,
    FONT_MONO,
    FONT_SANS,
    INK,
    LINE_2,
    MUTED,
    PRODUCT_COLORS,
    SURFACE,
)
from scheduler_engine import (
    CHANNELS,
    LORRY_STD_DEFAULT,
    PRODUCTS,
    SLOT,
    WEIGH_IN_LORRY,
    WEIGH_OUT_LORRY,
    YUSEN_STD_DEFAULT,
    hhmm_to_min,
    job_total_slots,
    min_to_hhmm,
)

COLS = ["DO No.", "Product", "Volume (ton)", "Transport Co.", "Requested Time", "Margin (min)"]
GPS_COLS = ["DO No.", "Lat", "Lon", "Last seen"]
BLACKOUT_LABEL = "⛔ ปิดช่อง"

# Product colours live in ui_theme so the chart, the legend and the CSS cannot drift apart.
# Still grouped by family: MAA red/orange, MMA purple, i-BMA blue, n-BMA green.
BASE_DATE = dt.date(2000, 1, 1)  # arbitrary anchor so Plotly can draw a time axis

STATUS_WAITING = "รอเข้า"
STATUS_LOADING = "กำลังโหลด"
STATUS_DONE = "เสร็จแล้ว"
STATUS_CANCELLED = "ยกเลิก"
STATUSES = [STATUS_WAITING, STATUS_LOADING, STATUS_DONE, STATUS_CANCELLED]


# ---------------------------------------------------------------------------
# DO table
# ---------------------------------------------------------------------------
def empty_df() -> pd.DataFrame:
    return pd.DataFrame({c: pd.Series(dtype="object") for c in COLS})


def _blank(v):
    return v is None or (isinstance(v, float) and pd.isna(v))


def _map_columns(raw: pd.DataFrame, alias: dict, targets: list) -> pd.DataFrame:
    lookup = {}
    for col in raw.columns:
        key = str(col).strip().lower()
        for target, names in alias.items():
            if key == target.lower() or key in names:
                lookup.setdefault(target, col)
    n = len(raw)
    data = {t: (raw[lookup[t]].tolist() if t in lookup else [None] * n) for t in targets}
    return pd.DataFrame(data, columns=targets)


def clean_time_cell(v) -> str:
    if _blank(v):
        return ""
    if isinstance(v, (dt.time, dt.datetime)):
        return f"{v.hour:02d}:{v.minute:02d}"
    m = hhmm_to_min(v)
    return min_to_hhmm(m) if m is not None else str(v).strip()


def normalise_upload(raw: pd.DataFrame) -> pd.DataFrame:
    """Map an uploaded sheet onto the six expected columns, tolerating common
    header spellings ('DO', 'do_no', 'Volume', 'Requested', ...)."""
    alias = {
        "DO No.": ["do no.", "do no", "do_no", "do", "dono", "do number", "เลขที่ do"],
        "Product": ["product", "prod", "grade", "สินค้า"],
        "Volume (ton)": ["volume (ton)", "volume", "vol", "ton", "tons", "qty", "quantity", "ปริมาณ"],
        "Transport Co.": ["transport co.", "transport co", "transport", "company", "carrier",
                          "transporter", "ผู้ขนส่ง", "บริษัทขนส่ง"],
        "Requested Time": ["requested time", "requested", "request time", "time", "req time",
                           "เวลาที่ต้องการ", "เวลา"],
        "Margin (min)": ["margin (min)", "margin", "margin min", "extra", "buffer", "เผื่อเวลา"],
    }
    out = _map_columns(raw, alias, COLS)
    out["Requested Time"] = out["Requested Time"].map(clean_time_cell)
    out["Volume (ton)"] = pd.to_numeric(out["Volume (ton)"], errors="coerce")
    out["Margin (min)"] = pd.to_numeric(out["Margin (min)"], errors="coerce").fillna(0)
    for c in ("DO No.", "Product", "Transport Co."):
        out[c] = out[c].map(lambda v: "" if _blank(v) else str(v).strip())
    return out


def normalise_gps_upload(raw: pd.DataFrame) -> pd.DataFrame:
    """Map a GPS export onto DO No. / Lat / Lon / Last seen."""
    alias = {
        "DO No.": ["do no.", "do no", "do_no", "do", "truck", "plate", "vehicle", "ทะเบียน"],
        "Lat": ["lat", "latitude", "gps_lat", "y", "ละติจูด"],
        "Lon": ["lon", "lng", "long", "longitude", "gps_lon", "x", "ลองจิจูด"],
        "Last seen": ["last seen", "last_seen", "timestamp", "time", "updated", "fix time",
                      "เวลาล่าสุด"],
    }
    out = _map_columns(raw, alias, GPS_COLS)
    out["DO No."] = out["DO No."].map(lambda v: "" if _blank(v) else str(v).strip())
    out["Lat"] = pd.to_numeric(out["Lat"], errors="coerce")
    out["Lon"] = pd.to_numeric(out["Lon"], errors="coerce")
    out["Last seen"] = out["Last seen"].map(clean_time_cell)
    return out


def rows_to_jobs(df: pd.DataFrame, cfg=None):
    """Convert the edited DO table into solver jobs. Returns (jobs, errors, warnings).

    `cfg` is only needed so the "no standard time for this size" warning reflects the
    standard-time tables actually in force, not the shipped defaults.
    """
    lorry_std = getattr(cfg, "lorry_std", None) or None
    yusen_std = getattr(cfg, "yusen_std", None) or None
    jobs, errors, warnings = [], [], []
    seen = set()
    for i, row in df.iterrows():
        line = i + 2  # 1-based, allowing for a header row when the planner reads a file
        do_no = str(row.get("DO No.") or "").strip()
        product = str(row.get("Product") or "").strip()
        vol_raw = row.get("Volume (ton)")
        if not do_no and not product and (pd.isna(vol_raw) or not str(vol_raw).strip()):
            continue
        if not do_no:
            errors.append(f"Row {line}: DO No. is blank.")
            continue
        if do_no in seen:
            errors.append(f"Row {line}: duplicate DO No. '{do_no}'.")
            continue
        seen.add(do_no)
        if product not in PRODUCTS:
            errors.append(f"{do_no}: product '{product}' is not one of {', '.join(PRODUCTS)}.")
            continue
        try:
            volume = float(vol_raw)
        except (TypeError, ValueError):
            errors.append(f"{do_no}: volume '{vol_raw}' is not a number.")
            continue
        if not volume > 0:
            errors.append(f"{do_no}: volume must be greater than 0.")
            continue
        company = str(row.get("Transport Co.") or "").strip()
        total_slots, _, bracket, _ = job_total_slots(
            product, volume, 0, 0, transport_co=company,
            lorry_std=lorry_std, yusen_std=yusen_std)
        if total_slots is None:
            warnings.append(f"{do_no}: {product} has no standard time for a {bracket} load "
                            f"({volume:g} t) - it cannot be scheduled.")

        raw_time = row.get("Requested Time")
        requested = None
        if raw_time is not None and str(raw_time).strip() not in ("", "nan", "NaT", "None"):
            requested = hhmm_to_min(raw_time)
            if requested is None:
                errors.append(f"{do_no}: requested time '{raw_time}' is not a valid HH:MM.")
                continue
            if requested % SLOT:
                snapped = int(round(requested / SLOT)) * SLOT
                warnings.append(f"{do_no}: requested {min_to_hhmm(requested)} is not on the "
                                f"{SLOT}-minute grid - it will be treated as {min_to_hhmm(snapped)}.")

        margin = row.get("Margin (min)")
        try:
            margin = 0 if margin is None or pd.isna(margin) else int(round(float(margin)))
        except (TypeError, ValueError):
            margin = 0
        if margin % SLOT:
            warnings.append(f"{do_no}: margin {margin} min is not a multiple of {SLOT}; "
                            f"the load time is rounded to the nearest {SLOT}-minute slot.")

        jobs.append({"id": do_no, "product": product, "volume": volume,
                     "company": company, "requested": requested, "margin": margin})
    return jobs, errors, warnings


# ---------------------------------------------------------------------------
# rendering
# ---------------------------------------------------------------------------
@st.cache_data(show_spinner=False)
def template_bytes() -> bytes:
    """The blank upload workbook, built once per session."""
    from do_template import build_template_bytes
    return build_template_bytes()


def to_ts(minutes) -> dt.datetime:
    return dt.datetime.combine(BASE_DATE, dt.time()) + dt.timedelta(minutes=int(minutes))


def _bar_type(r) -> str:
    if r.get("is_locked"):
        return f"FROZEN ({r.get('status') or 'กำลังโหลด/เสร็จแล้ว'})"
    return "FIXED (hard deadline)" if r["is_fixed"] else "flexible"


def _bar_label(r) -> str:
    if r.get("is_locked"):
        return "▪ " + r["id"]
    return ("🔒 " if r["is_fixed"] else "") + r["id"]


def gantt_figure(schedule, cfg, show_weigh_lane: bool, now_min=None):
    rows = []
    for r in schedule:
        rows.append({
            "Channel": f"Channel {r['channel']}",
            "Start": to_ts(r["start"]), "Finish": to_ts(r["end"]),
            "DO No.": r["id"], "Product": r["product"],
            "Volume (ton)": r["volume"], "Transport Co.": r["company"],
            "Window": f"{r['start_hhmm']} - {r['end_hhmm']}",
            "Duration (min)": r["duration_min"], "Bracket": r["bracket"],
            "Standard": r.get("std_source", ""),
            "ETA": r.get("earliest_hhmm") or "-",
            "Type": _bar_type(r), "Label": _bar_label(r),
        })
    for ch in CHANNELS:
        for bs, be, why in cfg.blackouts_for(ch):
            rows.append({
                "Channel": f"Channel {ch}", "Start": to_ts(bs), "Finish": to_ts(be),
                "DO No.": "-", "Product": BLACKOUT_LABEL, "Volume (ton)": 0,
                "Transport Co.": why or "ปิดซ่อม",
                "Window": f"{min_to_hhmm(bs)} - {min_to_hhmm(be)}",
                "Duration (min)": be - bs, "Bracket": why or "ปิดซ่อม",
                "Standard": "", "ETA": "-", "Type": "ปิดช่อง", "Label": "⛔ ปิดช่อง",
            })

    if show_weigh_lane:
        for r in schedule:
            for phase, span in (("Weigh-In", r["weigh_in_res"]), ("Weigh-Out", r["weigh_out_res"])):
                rows.append({
                    "Channel": "Weigh station (shared)",
                    "Start": to_ts(span[0]), "Finish": to_ts(span[1]),
                    "DO No.": r["id"], "Product": r["product"],
                    "Volume (ton)": r["volume"], "Transport Co.": r["company"],
                    "Window": f"{min_to_hhmm(span[0])} - {min_to_hhmm(span[1])}",
                    "Duration (min)": span[1] - span[0], "Bracket": phase,
                    "Standard": r.get("std_source", ""), "ETA": "-",
                    "Type": phase, "Label": "",
                })

    df = pd.DataFrame(rows)
    order = [f"Channel {c}" for c in CHANNELS]  # display order, top to bottom
    if show_weigh_lane:
        order.append("Weigh station (shared)")

    fig = px.timeline(
        df, x_start="Start", x_end="Finish", y="Channel", color="Product",
        color_discrete_map={**PRODUCT_COLORS, BLACKOUT_LABEL: "#5B6F7D"}, text="Label",
        hover_data={"DO No.": True, "Product": True, "Volume (ton)": True,
                    "Transport Co.": True, "Window": True, "Duration (min)": True,
                    "Bracket": True, "Standard": True, "ETA": True, "Type": True,
                    "Start": False, "Finish": False, "Channel": False},
    )
    # px.timeline flips the category list on its own and normalises autorange differently across
    # plotly versions; pin the axis explicitly so Channel A always sits at the top.
    fig.update_yaxes(title=None, categoryorder="array", categoryarray=order[::-1], autorange=True)
    fig.update_traces(textposition="inside", insidetextanchor="middle",
                      textfont=dict(size=10, family=FONT_MONO, color="#FFFFFF"),
                      marker_line_color="rgba(14,23,32,.35)", marker_line_width=1)

    for bs, be in cfg.breaks_min:
        fig.add_vrect(x0=to_ts(bs), x1=to_ts(be), fillcolor="#8497A4", opacity=0.22,
                      layer="below", line_width=0,
                      annotation_text=f"พัก {min_to_hhmm(bs)}", annotation_position="top left",
                      annotation_font=dict(size=10, color=MUTED, family=FONT_SANS))

    if now_min is not None:
        fig.add_vrect(x0=to_ts(cfg.window_start_min - 15), x1=to_ts(now_min),
                      fillcolor="#37474f", opacity=0.1, layer="below", line_width=0)
        # add_vline() + annotation averages the x values, which blows up on a date axis,
        # so the line and its label are drawn separately
        fig.add_shape(type="line", xref="x", yref="paper", x0=to_ts(now_min), x1=to_ts(now_min),
                      y0=0, y1=1, line=dict(color=CRIT, width=2))
        fig.add_annotation(x=to_ts(now_min), xref="x", yref="paper", y=1.02, yanchor="bottom",
                           text=f"● ตอนนี้ {min_to_hhmm(now_min)}", showarrow=False,
                           font=dict(color=CRIT, size=11, family=FONT_MONO))

    fig.update_xaxes(
        range=[to_ts(cfg.window_start_min - 15), to_ts(cfg.window_end_min + 15)],
        tickformat="%H:%M", dtick=30 * 60 * 1000, title=None, showgrid=True,
        gridcolor=LINE_2, tickfont=dict(family=FONT_MONO, size=11, color=MUTED),
    )
    fig.update_yaxes(tickfont=dict(family=FONT_SANS, size=12, color=INK))
    fig.update_layout(
        height=270 + (110 if show_weigh_lane else 0),
        margin=dict(l=10, r=10, t=34, b=10),
        paper_bgcolor=SURFACE, plot_bgcolor="#F2F5F8",
        font=dict(family=FONT_SANS, color=INK, size=12),
        hoverlabel=dict(font=dict(family=FONT_SANS, size=12), bordercolor=LINE_2),
        legend_title_text="",
        legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="left", x=0,
                    font=dict(size=11)),
        bargap=0.28,
    )
    return fig


def schedule_dataframe(schedule, with_eta: bool = False) -> pd.DataFrame:
    rows = []
    for r in schedule:
        row = {
            "Channel": r["channel"], "Start": r["start_hhmm"], "End": r["end_hhmm"],
            "DO No.": r["id"], "Product": r["product"], "Volume (ton)": r["volume"],
            "Transport Co.": r["company"],
            "Type": ("FROZEN" if r.get("is_locked") else ("FIXED" if r["is_fixed"] else "flexible")),
            "Requested": r["requested_hhmm"] or "-",
            "Load window": f"{min_to_hhmm(r['load_start'])} - {min_to_hhmm(r['load_end'])}",
            "Duration (min)": r["duration_min"],
            "Standard": r.get("std_source", ""), "Bracket": r["bracket"],
            "Margin (min)": r["margin"],
        }
        if with_eta:
            row["ETA"] = r.get("earliest_hhmm") or "-"
            row["Status"] = r.get("status") or ""
        rows.append(row)
    return pd.DataFrame(rows)


def dropped_dataframe(dropped, with_eta: bool = False) -> pd.DataFrame:
    rows = []
    for d in dropped:
        row = {
            "DO No.": d["id"], "Product": d["product"], "Volume (ton)": d["volume"],
            "Transport Co.": d.get("company", ""),
            "Type": "FIXED" if d.get("is_fixed") else "flexible",
            "Requested": d.get("requested_hhmm") or "-",
            "Standard": d.get("std_source", ""),
            "Reason": d["reason"], "Action": "Move to the next day",
        }
        if with_eta:
            row["ETA"] = d.get("earliest_hhmm") or "-"
        rows.append(row)
    return pd.DataFrame(rows)


def settings_frame(cfg, travel=None, now_min=None) -> pd.DataFrame:
    rows = [
        ("Loading window start", min_to_hhmm(cfg.window_start_min)),
        ("Last truck finished by", min_to_hhmm(cfg.window_end_min)),
        *[(f"Break {i + 1}", f"{min_to_hhmm(b[0])} - {min_to_hhmm(b[1])}")
          for i, b in enumerate(cfg.breaks_min)],
        # No "Realistic buffer" row any more: there is no single buffer to report.
        # Every planning figure carries its own, listed in the Standard times sheet.
        ("Shared camera/weighbridge per weigh phase (min)", cfg.weigh_resource_min),
        ("Weight-In / Weight-Out (min)", f"{WEIGH_IN_LORRY} / {WEIGH_OUT_LORRY}"),
        ("Buffer", "per row - see the 'Standard times' sheet"),
    ]
    for c in CHANNELS:
        for bs, be, why in cfg.blackouts_for(c):
            rows.append((f"ปิดช่อง {c}",
                         f"{min_to_hhmm(bs)} - {min_to_hhmm(be)}" + (f" ({why})" if why else "")))
    if now_min is not None:
        rows.append(("Re-planned at", min_to_hhmm(now_min)))
    if travel is not None:
        rows += [
            ("Plant lat / lon", f"{travel.plant_lat} / {travel.plant_lon}"),
            ("Average road speed (km/h)", travel.avg_speed_kmh),
            ("Detour factor", travel.detour_factor),
            ("Gate buffer (min)", travel.gate_buffer_min),
        ]
    return pd.DataFrame(rows, columns=["Setting", "Value"])


def std_times_frame(cfg) -> pd.DataFrame:
    """The planning figures this plan was actually costed with, one row per cell.

    This is what the old single "Realistic buffer" setting pretended to describe. The
    buffer is per row, so the export has to show it per row - otherwise a plan cannot
    be reproduced from its own Excel file.
    """
    rows = []

    def add(table, group, bracket, e):
        buffer_min = e.buffer_min or 0
        rows.append({
            "Table": table, "Group": group, "Bracket": bracket,
            "Cycle (min)": e.total_min, "Buffer (min)": buffer_min,
            "Planned total (min)": e.total_min + buffer_min,
            "Load (min)": e.load_min + buffer_min,
            "Source": e.source, "Reference": (f"P75 {e.p75_raw:g} · n={e.n}"
                                              if e.p75_raw is not None else ""),
            "Note": e.note,
        })

    for group, brackets in (cfg.lorry_std or LORRY_STD_DEFAULT).items():
        for bracket, entry in brackets.items():
            add("LORRY", group, bracket, entry)
    for product, entry in (cfg.yusen_std or YUSEN_STD_DEFAULT).items():
        add("ISO Tank (Yusen)", product, "-", entry)
    return pd.DataFrame(rows)


def build_excel(result, cfg, travel=None, gps_table=None) -> bytes:
    buf = io.BytesIO()
    now_min = result.get("now_min")
    with_eta = now_min is not None
    with pd.ExcelWriter(buf, engine="openpyxl") as xl:
        sched = schedule_dataframe(result["schedule"], with_eta=with_eta)
        (sched if not sched.empty else pd.DataFrame(columns=["Channel"])).to_excel(
            xl, sheet_name="Schedule", index=False)
        drop = dropped_dataframe(result["dropped"], with_eta=with_eta)
        (drop if not drop.empty else pd.DataFrame(columns=["DO No."])).to_excel(
            xl, sheet_name="Not scheduled", index=False)
        pd.DataFrame(result["channel_summary"]).to_excel(xl, sheet_name="Channel summary", index=False)
        if result.get("baseline_diff"):
            pd.DataFrame(result["baseline_diff"]).to_excel(xl, sheet_name="Changes", index=False)
        if gps_table is not None and not gps_table.empty:
            gps_table.to_excel(xl, sheet_name="Truck ETA", index=False)
        std_times_frame(cfg).to_excel(xl, sheet_name="Standard times", index=False)
        settings_frame(cfg, travel, now_min).to_excel(xl, sheet_name="Settings", index=False)
    return buf.getvalue()


def eta_table(pending, etas, now_min) -> pd.DataFrame:
    """Who is where, and can they still make their slot? Sorted soonest-first so the
    top of the table is literally 'who can fill the next gap'."""
    rows = []
    for p in pending:
        e = etas.get(p["id"], {"status": STATUS_UNKNOWN})
        eta_min = e.get("eta_min")
        planned = p.get("planned_start")
        late = None if (eta_min is None or planned is None) else eta_min - planned
        rows.append({
            "DO No.": p["id"], "Product": p.get("product", ""),
            "Status": e.get("status", STATUS_UNKNOWN),
            "Distance (km)": e.get("road_km"),
            "Travel (min)": e.get("travel_min"),
            "ETA": min_to_hhmm(eta_min) if eta_min is not None else "-",
            "แผนเดิม": min_to_hhmm(planned) if planned is not None else "-",
            "ช้ากว่าแผน (นาที)": "" if late is None else f"{late:+d}",
            "GPS อายุ (นาที)": e.get("age_min"),
            "สัญญาณเก่า": "⚠️" if e.get("stale") else "",
        })
    df = pd.DataFrame(rows)
    if not df.empty:
        df = df.sort_values("ETA", kind="stable").reset_index(drop=True)
    return df
