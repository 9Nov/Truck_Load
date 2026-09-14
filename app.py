"""
TMMA Loading Channel Scheduler - Streamlit front end.

Two modes over the same MILP engine:

  📋 วางแผนต้นวัน   - build the day's first-draft plan from the DO list
  🔄 ปรับแผนระหว่างวัน - rolling-horizon re-optimisation: freeze what has already
                       happened, feed each waiting truck's GPS ETA in as a release
                       time, and re-solve only the rest of the day while staying
                       as close to the committed plan as the constraints allow

Everything the planner can tune lives in the sidebar and is passed into the
solver as a SchedulerConfig / TravelConfig - nothing is hard-coded here.

Run:  streamlit run app.py
"""
import datetime as dt

import pandas as pd
import streamlit as st

from gps_eta import (
    STATUS_AT_PLANT,
    STATUS_NEAR,
    TravelConfig,
    fleet_etas,
    is_placeholder_plant,
    resolve_plant_latlon,
)
from scheduler_engine import (
    CHANNEL_ELIGIBILITY,
    CHANNELS,
    LORRY_STD_DEFAULT,
    LORRY_STD_FROM_ORIGINAL,
    PRODUCTS,
    SRC_ACTUAL,
    SLOT,
    TRANSPORT_COMPANIES,
    TRUCK_GAP_MIN_DEFAULT,
    SchedulerConfig,
    StabilityWeights,
    baseline_from_schedule,
    build_and_solve,
    YUSEN_STD_DEFAULT,
    YUSEN_STD_FROM_ORIGINAL,
    hhmm_to_min,
    is_yusen,
    min_to_hhmm,
)
from ui_common import (
    COLS,
    GPS_COLS,
    STATUS_CANCELLED,
    STATUS_DONE,
    STATUS_LOADING,
    STATUS_WAITING,
    STATUSES,
    build_excel,
    clean_time_cell,
    dropped_dataframe,
    empty_df,
    eta_table,
    gantt_figure,
    normalise_gps_upload,
    normalise_upload,
    rows_to_jobs,
    schedule_dataframe,
    template_bytes,
)
import maintenance
import std_times
import swap_planner
import ui_calendar
import ui_reschedule
import ui_theme
from ui_theme import FAMILY_COLORS

st.set_page_config(page_title="กระดานจัดคิวรถเข้าช่องโหลด", page_icon="🚚", layout="wide")
ui_theme.inject()

MODE_PLAN = "📋 วางแผนต้นวัน"
MODE_REPLAN = "🔄 ปรับแผนระหว่างวัน"
MODE_AUTO = "⚡ Auto — ให้ระบบจัดให้ทั้งหมด"
MODE_MANUAL = "🙋 Manual — เลือกคันแทนเอง"
MODE_STD = "⚙️ เวลามาตรฐาน"


# ---------------------------------------------------------------------------
# session state
# ---------------------------------------------------------------------------
DEFAULTS = {
    "do_df": None, "result": None, "run_meta": None, "upload_token": 0,
    "baseline": None, "baseline_schedule": None, "baseline_meta": None,
    "status_df": None, "status_sig": None, "gps_df": None, "gps_token": 0,
    "replan_result": None, "replan_meta": None,
    "swap_decisions": [], "swap_dismissed": [], "maintenance": [],
    "std_lorry_rows": None, "std_yusen_rows": None,
    "plan_overrides": {},
}
for key, value in DEFAULTS.items():
    if key not in st.session_state:
        st.session_state[key] = value
if st.session_state.do_df is None:
    st.session_state.do_df = empty_df()
# The planning tables start at the measured P75 defaults, NOT the ISO standard.
if st.session_state.std_lorry_rows is None:
    st.session_state.std_lorry_rows = std_times.lorry_rows()
if st.session_state.std_yusen_rows is None:
    st.session_state.std_yusen_rows = std_times.yusen_rows()


def std_total(cfg) -> int:
    """Sum of every planning figure in force (cycle + its own buffer).

    Used as a cheap fingerprint of the two standard-time tables: if a planner edits
    any cell, this number moves, which is what lets the app notice that the plan on
    screen was built with different numbers.
    """
    total = sum(e.total_min + (e.buffer_min or 0)
                for brackets in (cfg.lorry_std or LORRY_STD_DEFAULT).values()
                for e in brackets.values())
    return total + sum(e.total_min + (e.buffer_min or 0)
                       for e in (cfg.yusen_std or YUSEN_STD_DEFAULT).values())


def cfg_label(cfg) -> str:
    """Everything that can change the plan, in one line.

    It has to include the standard-time tables: editing those is the single biggest
    thing that changes a plan, and before they were editable this label only tracked
    the sidebar, so a table edit left a stale plan on screen with no warning.
    """
    breaks = "".join(f" พัก{min_to_hhmm(b)}" for b, _ in cfg.breaks_min)
    return (f"{min_to_hhmm(cfg.window_start_min)}–{min_to_hhmm(cfg.window_end_min)},"
            f"{breaks}, weigh {cfg.weigh_resource_min}m, std Σ{std_total(cfg)}m"
            + (f", ปิดช่อง {len(cfg.blackouts)} ช่วง" if cfg.blackouts else "")
            + (f", gap รถ {cfg.truck_gap_min}m" if cfg.truck_gap_min else ""))


# ---------------------------------------------------------------------------
# sidebar
# ---------------------------------------------------------------------------
with st.sidebar.container(border=True):
    st.markdown('<div class="tm-modelabel">โหมดการใช้งาน</div>', unsafe_allow_html=True)
    mode = st.radio("โหมด", [MODE_PLAN, MODE_REPLAN, MODE_STD],
                    label_visibility="collapsed")
st.sidebar.header("⚙️ ตั้งค่าระบบ")
st.sidebar.caption("ค่าทั้งหมดนี้ถูกส่งเข้า solver โดยตรง ไม่มีการ hard-code ใน engine")

with st.sidebar.expander("Operating window", expanded=True):
    plan_date = st.date_input("วันที่วางแผน", dt.date.today(), format="DD/MM/YYYY",
                              help="ใช้ตัดสินว่าช่วงซ่อมบำรุงช่วงไหนมีผลกับวันนี้บ้าง")
    start_t = st.time_input("เริ่มโหลด (first start)", dt.time(8, 0), step=300)
    end_t = st.time_input("คันสุดท้ายต้องเสร็จไม่เกิน (hard end)", dt.time(23, 30), step=300)

with st.sidebar.expander("Breaks", expanded=False):
    use_b1 = st.checkbox("ช่วงพักที่ 1", value=True)
    b1c = st.columns(2)
    b1s = b1c[0].time_input("เริ่ม", dt.time(12, 0), step=300, key="b1s", disabled=not use_b1)
    b1e = b1c[1].time_input("จบ", dt.time(12, 30), step=300, key="b1e", disabled=not use_b1)
    use_b2 = st.checkbox("ช่วงพักที่ 2", value=True)
    b2c = st.columns(2)
    b2s = b2c[0].time_input("เริ่ม", dt.time(19, 0), step=300, key="b2s", disabled=not use_b2)
    b2e = b2c[1].time_input("จบ", dt.time(19, 30), step=300, key="b2e", disabled=not use_b2)

window_start_min, window_end_min = hhmm_to_min(start_t), hhmm_to_min(end_t)
maint_items = maintenance.from_rows(st.session_state.maintenance)
active = maintenance.active_on(maint_items, plan_date)

with st.sidebar.expander(f"⛔ ช่วงปิดช่อง (ซ่อมบำรุง) — {len(maint_items)} รายการ",
                         expanded=False):
    st.caption("ปิดเป็นรายช่อง ช่องอื่นยังทำงานตามปกติ · ซ่อมข้ามวันได้ "
               "ระบบจะตัดเฉพาะส่วนที่ตรงกับวันที่วางแผนมาใช้")
    with st.form("add_maintenance", clear_on_submit=True):
        # full-width rows: a side-by-side date+time pair gets squeezed to a few
        # pixels in the sidebar and the value becomes unreadable
        m_ch = st.selectbox("ช่องที่ปิด", CHANNELS)
        m_d1 = st.date_input("ปิดตั้งแต่วันที่", plan_date, format="DD/MM/YYYY")
        m_t1 = st.time_input("เวลาที่เริ่มปิด", start_t, step=300)
        m_d2 = st.date_input("เปิดอีกครั้งวันที่", plan_date, format="DD/MM/YYYY",
                             help="วันเดียวกันได้ถ้าซ่อมเสร็จในวันนั้น · "
                                  "เลือกวันหลังได้ถ้าต้องหยุดหลายวัน")
        m_t2 = st.time_input("เวลาที่เปิดอีกครั้ง", end_t, step=300)
        m_why = st.text_input("เหตุผล", placeholder="เช่น ซ่อมแขนโหลด, calibrate meter")
        if st.form_submit_button("➕ เพิ่มช่วงปิด", width="stretch", type="primary"):
            item = maintenance.Maintenance(m_ch, m_d1, m_t1, m_d2, m_t2, m_why.strip())
            problems = item.errors()
            if problems:
                for problem in problems:
                    st.error(problem, icon="🚫")
            else:
                st.session_state.maintenance.append(maintenance.to_rows([item])[0])
                st.rerun()

    if maint_items:
        st.divider()
        for i, item in enumerate(maint_items):
            on_today = item.covers(plan_date)
            row = st.columns([5, 1])
            row[0].markdown(
                f'<div class="tm-maint{"" if on_today else " off"}">'
                f'<b>ช่อง {ui_theme.esc(item.channel)}</b> · {ui_theme.esc(item.describe())}'
                + (f'<br><span class="why">{ui_theme.esc(item.reason)}</span>'
                   if item.reason else "")
                + ("" if on_today else '<br><span class="why">ไม่ตรงกับวันที่วางแผน</span>')
                + "</div>", unsafe_allow_html=True)
            if row[1].button("✕", key=f"del_maint_{i}", help="ลบรายการนี้"):
                st.session_state.maintenance.pop(i)
                st.rerun()

blackouts = maintenance.to_blackouts(maint_items, plan_date, window_start_min, window_end_min)
blackout_errors = [f"ช่วงปิดช่อง: {e}" for item in maint_items for e in item.errors()]

with st.sidebar.expander("Timing", expanded=False):
    # Realistic Buffer used to live here as two sidebar numbers, but every row of the
    # planning tables carries its own buffer, so those numbers could never actually
    # reach the solver. The buffer is now edited where it is used: one column per row
    # on the Standard Time page.
    st.caption("⏱️ **เวลาเผื่อ (Realistic Buffer) ย้ายไปหน้า ⚙️ เวลามาตรฐานแล้ว** — "
               "อยู่ในคอลัมน์ *Buffer (นาที)* แก้ได้เป็นรายแถว "
               "เพราะแต่ละแถวต้องเผื่อไม่เท่ากัน (แถวที่มาจาก P75 เผื่อมาในตัวแล้ว)")
    weigh_res_min = st.number_input(
        "กล้อง + ตาชั่ง ใช้จริงต่อรอบ Weight-In/Out (นาที)",
        min_value=5, max_value=15, value=10, step=5,
        help="ของ 15 นาทีเต็ม ส่วนที่เหลือเป็นเอกสาร/ขับเข้าออก "
             "ช่องโหลดยังถูกจองเต็ม 15 + Load + 15 นาทีเหมือนเดิม")
    truck_gap_min = st.number_input(
        "รถคันเดียวกัน (Plan truck) ต้องเว้นห่างกันอย่างน้อย (นาที)",
        min_value=0, max_value=600, value=TRUCK_GAP_MIN_DEFAULT, step=5,
        help="ใช้เมื่อกรอกคอลัมน์ Plan truck ในไฟล์ DO — รถคันเดียวกันวิ่งหลายรอบ "
             "ต้องมีเวลาขับออก กลับรถ แล้วต่อคิวใหม่ ตั้งเป็น 0 เพื่อปิดเงื่อนไขนี้")

with st.sidebar.expander("Solver", expanded=False):
    time_limit = st.number_input(
        "Time limit (วินาที)", min_value=10, max_value=900, value=300, step=10,
        help="วันที่มี Yusen เยอะ ช่องโหลดจะแน่นกว่ามาก (cycle 100–110 นาที) การพิสูจน์ว่า optimal "
             "อาจใช้เวลาเกิน 2 นาที ถ้าชนเพดานนี้ ระบบยังให้แผนที่ใช้ได้จริง เพียงแต่ยังพิสูจน์ไม่ได้ว่าดีที่สุด")
    show_weigh_lane = st.checkbox("แสดงเลนกล้อง/ตาชั่งใน Gantt", value=True)

breaks = []
if use_b1:
    breaks.append((hhmm_to_min(b1s), hhmm_to_min(b1e)))
if use_b2:
    breaks.append((hhmm_to_min(b2s), hhmm_to_min(b2e)))

lorry_std, std_notes = std_times.rows_to_lorry(st.session_state.std_lorry_rows)
yusen_std, yusen_notes = std_times.rows_to_yusen(st.session_state.std_yusen_rows)
std_notes += yusen_notes

cfg = SchedulerConfig(
    window_start_min=window_start_min,
    window_end_min=window_end_min,
    breaks_min=breaks,
    # realistic_buffer_min / yusen_buffer_min are deliberately not set here: the two
    # tables below already carry a buffer on every row, so the config-level values
    # would never be consulted. They stay in SchedulerConfig only as a fallback for
    # callers that invoke the engine without tables (tests, batch scripts).
    weigh_resource_min=int(weigh_res_min),
    blackouts=blackouts,
    lorry_std=lorry_std,
    yusen_std=yusen_std,
    truck_gap_min=int(truck_gap_min),
)
cfg_problems = cfg.validate() + blackout_errors

travel = TravelConfig()
stability = StabilityWeights()
if mode == MODE_REPLAN:
    with st.sidebar.expander("🛰️ GPS / ETA", expanded=True):
        # The real coordinates never live in source: they come from deploy-time
        # secrets (.streamlit/secrets.toml locally, the Cloud secrets manager when
        # hosted) so the public repo only ever ships a placeholder.
        try:
            secret_lat, secret_lon = resolve_plant_latlon(st.secrets)
        except Exception:  # noqa: BLE001 - no secrets configured is the normal case
            secret_lat, secret_lon = resolve_plant_latlon(None)
        plant_lat = st.number_input("Plant latitude", value=secret_lat,
                                    format="%.6f", step=0.0001)
        plant_lon = st.number_input("Plant longitude", value=secret_lon,
                                    format="%.6f", step=0.0001)
        st.caption("พิกัดตาชั่งของโรงงาน — ระยะทางและ ETA ของรถทุกคันวัดจากจุดนี้ "
                   "ตั้งค่าจริงไว้ที่ deploy-time secrets แล้วจะขึ้นเป็นค่าเริ่มต้นให้เอง "
                   "หรือจะพิมพ์ทับที่นี่ก็ได้")
        speed = st.number_input("ความเร็วเฉลี่ยบนถนน (km/h)", min_value=10.0, max_value=110.0,
                                value=45.0, step=5.0)
        detour = st.number_input("Detour factor (ระยะถนน ÷ ระยะเส้นตรง)", min_value=1.0,
                                 max_value=2.5, value=1.35, step=0.05)
        gate_buffer = st.number_input("เผื่อเวลาหน้าประตู/เข้าคิว (นาที)", min_value=0,
                                      max_value=60, value=10, step=5)
        stale_after = st.number_input("ถือว่าสัญญาณเก่าเมื่อเกิน (นาที)", min_value=5,
                                      max_value=120, value=20, step=5)
    travel = TravelConfig(plant_lat=plant_lat, plant_lon=plant_lon, avg_speed_kmh=speed,
                          detour_factor=detour, gate_buffer_min=int(gate_buffer),
                          stale_after_min=int(stale_after))

    with st.sidebar.expander("⚖️ ความนิ่งของแผน", expanded=False):
        st.caption("ยิ่งสูง ยิ่งพยายามไม่ขยับแผนเดิม — ไม่กระทบจำนวน DO ที่จัดได้ "
                   "(ถูกล็อกไว้ตั้งแต่ phase 1 แล้ว) กระทบแค่ว่าจะเลือกคำตอบไหนในบรรดาที่ดีเท่ากัน")
        w_channel = st.slider("โทษเมื่อย้ายช่องโหลด", 0, 1000, 200, 50,
                              help="คนขับถูกบอกช่องไปแล้ว การย้ายช่องมีต้นทุนการสื่อสาร")
        w_delay = st.slider("โทษต่อการเลื่อนออกไป 5 นาที", 0, 50, 8, 1,
                            help="ดันคิวให้ช้ากว่าที่แจ้งไว้")
        w_advance = st.slider("โทษต่อการดึงเข้ามาเร็วขึ้น 5 นาที", 0, 50, 0, 1,
                              help="ปกติตั้ง 0 — รถที่ถูกดึงเข้ามาเร็วขึ้นคือรถที่ ETA ยืนยันแล้วว่าถึงทัน "
                                   "การดึงเข้ามาจึงเป็นการอุดช่องว่างที่รถสายทิ้งไว้")
    stability = StabilityWeights(channel_change=int(w_channel), delay=int(w_delay),
                                 advance=int(w_advance), early=1)
    cfg_problems += [f"GPS: {p}" for p in travel.validate()]

st.sidebar.divider()
down_note = maintenance.summarise(maint_items, plan_date,
                                 cfg.window_start_min, cfg.window_end_min)
st.sidebar.caption(
    f"{plan_date:%d/%m/%Y} · {min_to_hhmm(cfg.window_start_min)}–"
    f"{min_to_hhmm(cfg.window_end_min)} · {cfg.total_slots} slots × {SLOT} min"
    + (f"\n\n⛔ ปิดช่องวันนี้ {down_note}" if down_note else ""))


# ---------------------------------------------------------------------------
# shared result rendering
# ---------------------------------------------------------------------------
def _do_df_row(do_id):
    """Index of a DO No. in the editable DO table, or None if it is gone."""
    df = st.session_state.do_df
    mask = df["DO No."].astype(str) == str(do_id)
    return mask[mask].index[0] if mask.any() else None


def _resolve_now(cfg, time_limit):
    """Re-run the solver against the current DO table and stash the result --
    the same thing "Run Scheduler" does, used here so confirming a fix inside a
    dialog takes effect immediately instead of leaving the planner to notice the
    table changed and press the button again."""
    jobs, _errors, _warnings = rows_to_jobs(st.session_state.do_df, cfg)
    for j in jobs:
        override = st.session_state.plan_overrides.get(j["id"])
        if override:
            j["pin_channel"], j["requested"] = override
    t0 = dt.datetime.now()
    try:
        result = build_and_solve(jobs, config=cfg, time_limit=float(time_limit))
    except Exception as exc:  # noqa: BLE001 - never leave the planner with a blank page
        result = {"error": "engine_exception", "message": str(exc), "schedule": [],
                  "dropped": [], "validation": [], "channel_summary": [], "config": cfg}
    elapsed = (dt.datetime.now() - t0).total_seconds()
    st.session_state.result = result
    st.session_state.run_meta = {"elapsed": elapsed, "n_jobs": len(jobs),
                                 "at": dt.datetime.now().strftime("%H:%M:%S"),
                                 "cfg_label": cfg_label(cfg)}
    if not result.get("error"):
        st.session_state.baseline = baseline_from_schedule(result["schedule"])
        st.session_state.baseline_schedule = result["schedule"]
        st.session_state.baseline_meta = {"at": st.session_state.run_meta["at"],
                                          "cfg_label": cfg_label(cfg)}
        st.session_state.status_sig = None
        st.session_state.replan_result = None


@st.dialog("ปรับเวลาที่ขอไว้")
def _relax_dialog(do_id, cfg, time_limit):
    row_idx = _do_df_row(do_id)
    if row_idx is None:
        st.warning(f"ไม่พบ {do_id} ในตาราง DO แล้ว — อาจถูกลบหรือแก้ไขไปแล้ว")
        if st.button("ปิด", width="stretch"):
            st.rerun()
        return

    df = st.session_state.do_df
    current = str(df.loc[row_idx, "Requested Time"] or "").strip()
    st.write(f"**{do_id}** — เวลาที่ขอไว้ตอนนี้: **{current or 'ยืดหยุ่น (ไม่ได้ล็อก)'}**")
    choice = st.radio(
        "เลือกการปรับ",
        ["ปลดล็อกเวลา (ให้ระบบเลือกเวลาที่ดีที่สุดให้)", "เปลี่ยนเป็นเวลาอื่น"],
        index=0 if not current else 1)
    new_time = ""
    if choice.startswith("เปลี่ยน"):
        default_t = dt.time(8, 0)
        parsed = hhmm_to_min(current)
        if parsed is not None:
            default_t = dt.time(parsed // 60 % 24, parsed % 60)
        t = st.time_input("เวลาที่ต้องการ", default_t, step=300)
        new_time = f"{t.hour:02d}:{t.minute:02d}"

    c1, c2 = st.columns(2)
    if c1.button("✅ ยืนยัน แล้วจัดคิวใหม่", type="primary", width="stretch"):
        st.session_state.do_df.loc[row_idx, "Requested Time"] = new_time
        with st.spinner("กำลังจัดคิวใหม่ …"):
            _resolve_now(cfg, time_limit)
        st.rerun()
    if c2.button("✖️ ยกเลิก", width="stretch"):
        st.rerun()


def render_conflict(result, cfg=None, time_limit=None):
    """cfg/time_limit enable the click-to-fix dialog (plan mode, where a conflict
    can be resolved by editing the DO table and re-solving immediately). Without
    them (the re-plan page, which keeps its own separate result/session-state and
    job-building from GPS + frozen jobs, not the DO table) this falls back to the
    old plain read-only list."""
    st.error("⛔ **Hard-deadline conflict — ระบบไม่เลื่อนเวลาให้เอง**\n\n" + result["message"])
    candidates = result["candidates_to_relax"]
    if cfg is not None and time_limit is not None:
        st.markdown("**DO ที่ขัดแย้งกัน (ปลดตัวใดตัวหนึ่งแล้วทั้งวันจะจัดได้) — คลิกเพื่อปรับเวลา:**")
        cols = st.columns(min(4, len(candidates)) or 1)
        for i, do_id in enumerate(candidates):
            if cols[i % len(cols)].button(f"🔧 {do_id}", key=f"relax_{do_id}", width="stretch"):
                _relax_dialog(do_id, cfg, time_limit)
    else:
        st.markdown("**DO ที่ขัดแย้งกัน (ปลดตัวใดตัวหนึ่งแล้วทั้งวันจะจัดได้):**")
        st.dataframe(pd.DataFrame({"DO No. ที่ต้องพิจารณาเปลี่ยนเวลา": candidates}),
                     width="stretch", hide_index=True)
    st.info("ผู้วางแผนต้องเป็นคนตัดสินใจว่าจะเปลี่ยน Requested Time ของ DO ไหน "
            "แล้วสั่งจัดคิวใหม่ — ระบบจะไม่เลือกดร็อปเองเงียบ ๆ")
    if result.get("fixed_infeasible"):
        st.warning("DO ที่เวลาที่ขอใช้ไม่ได้: " +
                   ", ".join(f"{j['id']} ({j.get('infeasible_reason', '')})"
                             for j in result["fixed_infeasible"]), icon="⚠️")
    if result.get("no_std"):
        st.warning("DO ที่ไม่มี standard time สำหรับ product/ขนาดนี้: " +
                   ", ".join(j["id"] for j in result["no_std"]), icon="⚠️")


def render_selfcheck(result):
    problems = result.get("validation") or []
    if problems:
        st.error("❗ Self-check ตรวจพบความผิดปกติในคำตอบของ solver — **อย่าเพิ่งนำไปใช้** "
                 "และแจ้งผู้ดูแลระบบ:\n\n" + "\n".join(f"- {p}" for p in problems))
        return False
    st.success(f"✅ Self-check ผ่าน — ไม่มีรถชนกันในช่องโหลด ไม่มีรถใช้กล้อง/ตาชั่งพร้อมกัน "
               f"และไม่มีคิวไหนเริ่มก่อนที่รถจะมาถึงได้ ({len(result['schedule'])} คัน) · "
               f"solver: {result.get('status', '')}")
    if not result.get("proven_optimal", True):
        st.warning("⏱️ Solver หยุดที่ **time limit** ก่อนพิสูจน์ว่าเป็นคำตอบที่ดีที่สุด — "
                   "แผนนี้ยังใช้งานได้จริง (ผ่าน hard constraint ทุกข้อ) แต่อาจมีแผนที่ดีกว่านี้อยู่ "
                   "ถ้าต้องการคำตอบที่พิสูจน์ได้ ให้เพิ่ม Time limit ใน sidebar แล้วรันใหม่", icon="⏱️")
    return True


def _movable_rows(schedule):
    """DOs a planner may still send through the click-to-reschedule flow.

    A DO the planner already moved once is pinned (pin_channel + requested) so
    the solver keeps it exactly there, which also makes it read as `is_fixed` --
    same as a genuine hard-deadline DO from the upload. Without the
    plan_overrides check below that would make a moved DO un-reschedulable after
    its first move; checking plan_overrides tells the two apart so only a real
    upload-side hard deadline (never in plan_overrides) stays off-limits."""
    overrides = st.session_state.get("plan_overrides", {})
    return [r for r in schedule if not r.get("is_locked")
            and (not r["is_fixed"] or r["id"] in overrides)]


def render_results(result, res_cfg, *, now_min=None, gps_table=None, key: str = "plan"):
    schedule, dropped = result["schedule"], result["dropped"]
    with_eta = now_min is not None

    tab_names = ["🗓️ กระดานคิว", "📋 ตารางผลลัพธ์", "📈 การใช้งานช่องโหลด",
                 f"⚠️ จัดไม่ได้วันนี้ ({len(dropped)})", "⬇️ Export"]
    if with_eta:
        tab_names.insert(1, f"🔀 สิ่งที่เปลี่ยนไป ({len(result.get('baseline_diff', []))})")
        tab_names.insert(2, "🛰️ รถแต่ละคัน")
    tabs = st.tabs(tab_names)
    i = 0

    with tabs[i]:
        if schedule:
            vc = st.columns([2, 1.4, 2.6])
            view = vc[0].radio("มุมมอง", ["🗓️ ปฏิทินรายช่อง", "📊 Timeline แนวนอน"],
                               horizontal=True, label_visibility="collapsed", key=f"view_{key}")
            if view.startswith("🗓️"):
                zoom = vc[1].select_slider(
                    "ความสูง", options=[44, 62, 84, 112], value=62, label_visibility="collapsed",
                    format_func=lambda v: f"{v} px/ชม.", key=f"zoom_{key}",
                    help="ยืด/ย่อความสูงของแกนเวลา")
                if key == "plan":
                    movable_ids = {r["id"] for r in _movable_rows(schedule)}
                    st.caption("👆 คลิกการ์ดคิวที่ไม่มี 🔒 เพื่อเลือกไปย้าย (ดูช่วงว่างและยืนยันได้ที่ขั้น 4 ด้านล่าง)")
                    picked = ui_calendar.render(schedule, res_cfg, result["channel_summary"],
                                                now_min=now_min, px_per_hour=int(zoom),
                                                clickable_ids=movable_ids,
                                                selected_id=st.session_state.get("reschedule_pick"))
                    if picked:
                        # Always rerun, even re-picking the already-selected card: the
                        # calendar's click bridge mounts a fresh listener only once it
                        # re-renders (see ui_calendar's nonce comment) -- skipping the
                        # rerun here would leave the OLD, already-fired listener on
                        # screen with nothing live to catch the NEXT click.
                        st.session_state["reschedule_pick"] = picked
                        st.rerun()
                else:
                    ui_calendar.render(schedule, res_cfg, result["channel_summary"],
                                      now_min=now_min, px_per_hour=int(zoom))
            else:
                st.plotly_chart(gantt_figure(schedule, res_cfg, show_weigh_lane, now_min=now_min),
                                width="stretch", key=f"gantt_{key}")
            entries = [(c, name) for name, c in FAMILY_COLORS.items()]
            note = ("🔒 คิวที่ล็อกเวลาไว้ · แถบเทา ช่วงพัก · แถบลายทางเทา ช่องปิดซ่อม · "
                    "กรอบลายทางแดง ช่องว่างที่เสียไป · "
                    "ระบบนี้ดูแลเฉพาะช่อง A/B/C — ช่อง D เป็นของ Truck Drum / Dry container")
            if with_eta:
                note = ("กรอบฟ้า กำลังโหลด · กรอบเขียว รถรออยู่แล้ว · กรอบแดง จะมาสาย · "
                        "เส้นประ ไม่รู้ตำแหน่ง · เส้นแดงนอน เวลาปัจจุบัน · ") + note
            ui_theme.legend(entries, note)
        else:
            st.info("ไม่มี DO ที่จัดคิวได้")
    i += 1

    if with_eta:
        with tabs[i]:
            diff = result.get("baseline_diff") or []
            if diff:
                st.dataframe(pd.DataFrame(diff), width="stretch", hide_index=True)
                st.caption("เทียบกับแผนที่ committed ไว้ — แถวที่ไม่เปลี่ยนจะไม่แสดง")
            else:
                st.success("แผนใหม่ไม่ต่างจากแผนเดิมเลย 👍")
        i += 1
        with tabs[i]:
            if gps_table is not None and not gps_table.empty:
                st.dataframe(gps_table, width="stretch", hide_index=True)
                st.caption("เรียงตาม ETA — แถวบนสุดคือรถที่พร้อมเสียบคิวก่อน")
            else:
                st.info("ยังไม่มีข้อมูล GPS")
        i += 1

    with tabs[i]:
        if schedule:
            st.dataframe(schedule_dataframe(schedule, with_eta=with_eta),
                         width="stretch", hide_index=True)
        else:
            st.info("ไม่มี DO ที่จัดคิวได้")
    i += 1

    with tabs[i]:
        summary = pd.DataFrame(result["channel_summary"])
        st.dataframe(summary, width="stretch", hide_index=True)
        if not summary.empty:
            st.bar_chart(summary.set_index("Channel")["Utilisation %"])
        st.caption("Utilisation = busy minutes ÷ เวลาทำงานสุทธิ (หักช่วงพักแล้ว) · "
                   "busy นับเต็มช่วง 15 + Load + 15 นาที")
    i += 1

    with tabs[i]:
        if dropped:
            st.error(f"มี {len(dropped)} DO ที่จัดไม่ได้ภายในวันนี้ — **ต้องเลื่อนไปวันถัดไป** "
                     "(ห้ามย้ายไป Channel D ซึ่งใช้กับ Truck Drum / Dry Container เท่านั้น)", icon="⚠️")
            st.dataframe(dropped_dataframe(dropped, with_eta=with_eta),
                         width="stretch", hide_index=True)
            st.caption("ช่องที่ DO เหล่านี้ใช้ได้: " + " · ".join(
                f"{d['id']}→{'/'.join(CHANNEL_ELIGIBILITY[d['product']])}" for d in dropped))
        else:
            st.success("จัดคิวได้ครบทุก DO ภายในวันนี้ 🎉")
    i += 1

    with tabs[i]:
        st.download_button(
            "⬇️ ดาวน์โหลดผลลัพธ์เป็น Excel",
            data=build_excel(result, res_cfg, travel if with_eta else None,
                             gps_table if with_eta else None),
            file_name=(f"tmma_{'replan' if with_eta else 'schedule'}_"
                       f"{dt.date.today():%Y%m%d}.xlsx"),
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            width="stretch", key=f"dl_{key}",
        )
        with st.expander("รายละเอียด solver"):
            st.json({"status": result.get("status"), "variables": result.get("n_vars"),
                     "constraints": result.get("n_constraints"),
                     "flexible DOs scheduled": result.get("max_scheduled_flex"),
                     "flexible DOs total": result.get("n_flex_total"),
                     "fixed DOs": result.get("n_fixed"), "frozen DOs": result.get("n_locked"),
                     "slot size (min)": SLOT})


# ---------------------------------------------------------------------------
# manual swap workflow (used by the re-plan page)
# ---------------------------------------------------------------------------
STAT_LABEL = {
    swap_planner.ON_TIME: ("ok", "ตรงเวลา / มาก่อน"),
    swap_planner.SLIGHTLY_LATE: ("slight", "สายเล็กน้อย"),
    swap_planner.LATE: ("late", "จะสาย"),
    swap_planner.NO_SIGNAL: ("blind", "ไม่มีสัญญาณ"),
}


def _truck_line(row, policy):
    """One inbound truck, rendered the way the floor board shows it."""
    tone, label = STAT_LABEL[row["status"]]
    if row["status"] == swap_planner.LATE:
        label = f"จะสาย {row['late_by']} นาที — เกิน {policy.late_threshold_min} นาที"
    elif row["status"] == swap_planner.SLIGHTLY_LATE:
        label = f"สาย {row['late_by']} นาที — ยังอยู่ในเกณฑ์"
    eta_txt = (min_to_hhmm(row["eta"]) if row["eta"] is not None
               else "ยังไม่มีสัญญาณ")
    colour = ui_theme.PRODUCT_COLORS.get(row["product"], "#0B5FA5")
    at_plant = ' <span class="tm-badge">จอดรออยู่แล้ว</span>' if row.get("at_plant") else ""
    return (
        f'<div class="tm-row"><span class="tm-chb">{ui_theme.esc(row["planned_channel"])}</span>'
        f'<span class="main">'
        f'<span class="l1"><span class="do">{ui_theme.esc(row["id"])}</span>'
        f'<span style="color:{colour};font-weight:700">{ui_theme.esc(row["product"])}</span>'
        f'<span class="who">{ui_theme.esc(row.get("company") or "—")}</span>{at_plant}</span>'
        f'<span class="l2">นัด <b>{min_to_hhmm(row["planned_start"])}</b> · '
        f'ETA ล่าสุด <b>{ui_theme.esc(eta_txt)}</b></span></span>'
        f'<span class="tm-stat {tone}">{ui_theme.esc(label)}</span></div>')


def _candidate_line(cand, target, policy):
    colour = ui_theme.PRODUCT_COLORS.get(cand["product"], "#0B5FA5")
    where = ('<span class="tm-badge">จอดรออยู่แล้ว</span>' if cand["at_plant"]
             else f'<span class="tm-badge q">เรียกเข้าได้ {min_to_hhmm(cand["ready"])}</span>')
    same = ('<span class="tm-badge q">อยู่ช่อง %s อยู่แล้ว</span>' % ui_theme.esc(target["planned_channel"])
            if cand["same_channel"] else "")
    if cand["pulled_forward"] > 0:
        trade = (f'<span class="tm-badge">ดึงเข้ามาเร็วขึ้น {cand["pulled_forward"]} นาที</span>')
    else:
        trade = ('<span class="tm-badge" style="background:var(--warn-soft);color:var(--warn);'
                 f'border-color:var(--warn-line)">คันนี้จะถูกเลื่อนออก {-cand["pulled_forward"]} นาที</span>')
    return (
        f'<div class="tm-row" style="padding:2px 0">'
        f'<span class="main"><span class="l1">'
        f'<span class="do">{ui_theme.esc(cand["id"])}</span>'
        f'<span style="color:{colour};font-weight:700">{ui_theme.esc(cand["product"])}</span>'
        f'<span class="who">{ui_theme.esc(cand.get("company") or "—")}</span>{where}{same}</span>'
        f'<span class="l2">ETA <b>{min_to_hhmm(cand["eta"])}</b> · '
        f'พร้อมเข้า <b>{min_to_hhmm(cand["ready"])}</b> · '
        f'เดิมนัดไว้ {min_to_hhmm(cand["planned_start"])} ช่อง {ui_theme.esc(cand["planned_channel"])}'
        f'</span></span></div>')


def render_swap_board(board, policy, now_min):
    """The Manual lane: show who will miss their slot, who could take it, and let
    the planner commit. Each commitment becomes a solver constraint, not a UI edit."""
    decisions = st.session_state.swap_decisions
    dismissed = st.session_state.swap_dismissed
    taken = {d["replacement_id"] for d in decisions} | {d["target_id"] for d in decisions}

    late = [r for r in board if r["status"] == swap_planner.LATE and r["id"] not in dismissed
            and r["id"] not in taken]
    blind = [r for r in board if r["status"] == swap_planner.NO_SIGNAL]

    ui_theme.sums([
        (sum(1 for r in board if r["status"] == swap_planner.ON_TIME), "ตรงเวลา / มาก่อน",
         "ไม่ต้องทำอะไร", "ok"),
        (sum(1 for r in board if r["status"] == swap_planner.SLIGHTLY_LATE), "สายเล็กน้อย",
         f"ไม่เกิน {policy.late_threshold_min} นาที · ปล่อยไว้", "quiet"),
        (len(late), "ต้องตัดสินใจ", "สายเกินเกณฑ์ — หาคันแทนได้", "crit" if late else "quiet"),
        (len(blind), "ไม่มีสัญญาณ", "ยืนยันกับคนขับก่อน", "warn dashed" if blind else "quiet"),
        (len(decisions), "ตัดสินใจแล้ว", "รอกดจัดคิวใหม่", "accent" if decisions else "quiet"),
    ])

    if decisions:
        st.markdown("**การตัดสินใจที่รออยู่**")
        for i, d in enumerate(list(decisions)):
            c = st.columns([7, 1.2])
            c[0].markdown(
                f'<div class="tm-note">✓ ช่อง <b>{ui_theme.esc(d["channel"])}</b> '
                f'เวลา <b>{min_to_hhmm(d["start"])}</b> — ให้ <b>{ui_theme.esc(d["replacement_id"])}'
                f'</b> ({ui_theme.esc(d["rep_product"])}) เข้าแทน '
                f'<b>{ui_theme.esc(d["target_id"])}</b> ({ui_theme.esc(d["target_product"])})<br>'
                f'{ui_theme.esc(d["note"])}</div>', unsafe_allow_html=True)
            if c[1].button("✕ ยกเลิก", key=f"undo_{i}_{d['target_id']}", width="stretch"):
                st.session_state.swap_decisions.remove(d)
                st.rerun()
        st.divider()

    if not late:
        if dismissed:
            st.caption(f"ปิดการเตือนไว้ {len(dismissed)} คัน — "
                       "กด 'ล้างการตัดสินใจ' ด้านล่างเพื่อให้กลับมาเตือนอีกครั้ง")
        st.success("ไม่มีคันไหนที่สายเกินเกณฑ์และยังไม่ได้ตัดสินใจ", icon="✅")
    for row in late:
        st.markdown(_truck_line(row, policy), unsafe_allow_html=True)
        cands = swap_planner.find_candidates(row, board, now_min, policy)
        cands = [c for c in cands if c["id"] not in taken]
        with st.expander(f"🔍 วิเคราะห์รถทดแทนของ {row['id']} — เจอ {len(cands)} คัน"):
            st.caption(
                f"คันที่พร้อมเข้าก่อน {min_to_hhmm(row['planned_start'] - policy.lead_min)} "
                f"(เร็วกว่าคิวเดิมอย่างน้อย {policy.lead_min} นาที · รวมคันที่จอดรออยู่หน้าโรงงานแล้ว) "
                f"— เลือกคันที่จะให้เข้าก่อน")
            if not cands:
                st.info("ไม่มีคันไหนเข้าเงื่อนไข — ลองลดเกณฑ์ 'พร้อมก่อนคิว' หรือ 'แจ้งล่วงหน้า' "
                        "ในกล่องตั้งค่าด้านบน หรือปล่อยให้คิวนี้สายตามเดิม", icon="ℹ️")
            for cand in cands:
                cc = st.columns([7, 1.6])
                cc[0].markdown(_candidate_line(cand, row, policy), unsafe_allow_html=True)
                if cc[1].button("เลือกคันนี้แทน", key=f"pick_{row['id']}_{cand['id']}",
                                type="primary", width="stretch"):
                    st.session_state.swap_decisions.append({
                        "target_id": row["id"], "target_product": row["product"],
                        "replacement_id": cand["id"], "rep_product": cand["product"],
                        "channel": row["planned_channel"], "start": row["planned_start"],
                        "note": swap_planner.candidate_note(row, cand, policy, now_min),
                    })
                    st.rerun()
                cc[0].markdown(
                    f'<div class="tm-note">{ui_theme.esc(swap_planner.candidate_note(row, cand, policy, now_min))}</div>',
                    unsafe_allow_html=True)
            if st.button(f"ปิด ไม่สลับ — ปล่อย {row['id']} สายตามเดิม",
                         key=f"skip_{row['id']}", width="stretch"):
                st.session_state.swap_dismissed.append(row["id"])
                st.rerun()

    if blind:
        st.warning("ไม่มีสัญญาณ GPS: " + ", ".join(r["id"] for r in blind[:12])
                   + (" …" if len(blind) > 12 else "")
                   + " — ระบบจะถือว่าพร้อมตั้งแต่ตอนนี้ ถ้าไม่จริงให้ใส่ ETA ที่รู้แน่ในตารางขั้น 1",
                   icon="⚪")


def _std_editor(rows, cols, key, editable_cols):
    """One standard-time table. Only the two numeric columns are editable; the rest
    is provenance the planner should be able to see but not quietly overwrite."""
    config = {}
    for col in cols:
        if col == std_times.TOTAL:
            config[col] = st.column_config.NumberColumn(
                col, min_value=std_times.MIN_TOTAL, max_value=600, step=5,
                help="เวลารวมทั้งรอบ (Weight-In 15 + Load + Weight-Out 15) · "
                     f"ต้องเป็นจำนวนเท่าของ {SLOT} นาที")
        elif col == std_times.BUFFER:
            config[col] = st.column_config.NumberColumn(
                col, min_value=0, max_value=60, step=5,
                help="เวลาเผื่อที่บวกเพิ่มจากค่าข้างซ้าย — **นี่คือค่าเผื่อเดียวที่ solver ใช้** "
                     "(ไม่มี Realistic Buffer กลางใน sidebar แล้ว) · "
                     "แถวที่มาจากข้อมูลจริง (P75) ตั้งเป็น 0 เพราะเผื่อรวมอยู่ในตัวเลขแล้ว")
        elif col == std_times.ORIGINAL:
            config[col] = st.column_config.NumberColumn(col, disabled=True,
                                                        help="ค่าตามตาราง standard เดิม")
        else:
            config[col] = st.column_config.TextColumn(col, disabled=True)
    return st.data_editor(pd.DataFrame(rows, columns=cols), width="stretch",
                          hide_index=True, key=key, column_config=config,
                          disabled=[c for c in cols if c not in editable_cols])


def page_standard_times():
    n_actual = sum(1 for r in st.session_state.std_lorry_rows + st.session_state.std_yusen_rows
                   if r[std_times.SOURCE] == SRC_ACTUAL)
    n_total = len(st.session_state.std_lorry_rows) + len(st.session_state.std_yusen_rows)
    changed = (std_times.changed_rows(lorry_std, LORRY_STD_DEFAULT)
               + std_times.changed_rows(yusen_std, YUSEN_STD_DEFAULT))
    ui_theme.hero(
        "เวลามาตรฐานที่ใช้วางแผน",
        "ตัวเลขที่ solver ใช้คิดว่าหนึ่งเที่ยวกินเวลาเท่าไร — แก้ได้จากหน้านี้ มีผลกับการจัดคิวทันที",
        f"{n_actual}/{n_total}", "แถวที่มาจากข้อมูลจริง",
        "warn" if changed else "ok",
        f"แก้จากค่าเริ่มต้นแล้ว {len(changed)} แถว" if changed else "ใช้ค่าเริ่มต้นทั้งหมด",
        "กดรีเซ็ตด้านล่างเพื่อกลับไปค่าเริ่มต้น" if changed else
        "ค่าเริ่มต้น = P75 ของเวลาจริง 12 เดือนล่าสุด")

    with st.container(border=True):
        ui_theme.head("ที่มา", "ค่าเริ่มต้นมาจากไหน",
                      "Lorry 3,601 เที่ยว · Yusen 1,213 เที่ยว · 12 เดือนล่าสุด")
        st.markdown(
            "ค่าเริ่มต้นของทุกแถวคือ **P75 ของเวลาที่รถใช้จริง** ปัดขึ้นลงเป็นจำนวนเท่าของ "
            f"{SLOT} นาที ไม่ใช่ตาราง standard เดิม · แถวที่ **ไม่มีเที่ยวจริงเลย** "
            "จึงยังใช้ standard เดิมและถูกทำเครื่องหมายไว้\n\n"
            "**ทำไมใช้ P75 ไม่ใช่ค่ากลาง** — โมเดลจัดคิวเป็นแบบ deterministic "
            "(หนึ่ง DO = เวลาค่าเดียว ไม่จำลองความแปรปรวน) และวางคิวต่อกันชนิดชิดกัน "
            "ตัวเลขที่ใส่เข้าไปจึงเท่ากับ *โอกาสที่จะไม่ล้นไปทับคิวถัดไป* โดยตรง · "
            "ถ้าใช้ค่ากลางจะมีโอกาสราว 50% ที่เที่ยวจริงใช้เวลานานกว่าที่จัดไว้แล้วดันคิวถัดไปช้าตาม "
            "ส่วน P75 เหลือความเสี่ยงราว 25% ซึ่งเป็นระดับเดียวกับที่ standard ของ Yusen "
            "ตั้งไว้อยู่แล้ว\n\n"
            "เพราะ P75 มีเวลาเผื่ออยู่ในตัวแล้ว แถวที่มาจากข้อมูลจริงจึงตั้ง **Buffer = 0** "
            "ส่วนแถวที่ยังใช้ standard เดิมตั้ง **Buffer = 10** ตามเดิม\n\n"
            "**เวลาเผื่ออยู่ที่นี่ที่เดียว** — คอลัมน์ *Buffer (นาที)* ด้านล่างคือค่าเผื่อเดียวที่ "
            "solver ใช้จริง เดิมมีช่อง Realistic Buffer ใน sidebar ด้วย แต่ค่านั้นถูกแถวในตารางนี้ "
            "ทับเสมอจนไม่เคยมีผล จึงถูกถอดออกเพื่อไม่ให้เข้าใจผิดว่าปรับแล้วแผนจะเปลี่ยน")

    with st.container(border=True):
        ui_theme.head("LORRY", "ศรีไทย / SV / VIV — แยกตามขนาดบรรทุก",
                      "ตัวเลขคือเวลารวมทั้งรอบ · Load = ค่านี้ − 30 นาที")
        edited_lorry = _std_editor(st.session_state.std_lorry_rows, std_times.LORRY_COLS,
                                   "std_lorry_editor", {std_times.TOTAL, std_times.BUFFER})
        rows = edited_lorry.to_dict("records")
        if rows != st.session_state.std_lorry_rows:
            st.session_state.std_lorry_rows = rows
            st.rerun()

    with st.container(border=True):
        ui_theme.head("ISO TANK", "Yusen — ไม่แยกตามขนาด",
                      "รถหัวลากลากตู้ขนาดมาตรฐาน ขนาดบรรทุกจึงไม่เปลี่ยนเวลา")
        edited_yusen = _std_editor(st.session_state.std_yusen_rows, std_times.YUSEN_COLS,
                                   "std_yusen_editor", {std_times.TOTAL, std_times.BUFFER})
        rows = edited_yusen.to_dict("records")
        if rows != st.session_state.std_yusen_rows:
            st.session_state.std_yusen_rows = rows
            st.rerun()

    for note in std_notes:
        st.warning(note, icon="↧")

    with st.container(border=True):
        ui_theme.head("จัดการ", "รีเซ็ต / สำรองค่า",
                      f"ตอนนี้ต่างจากค่าเริ่มต้น {len(changed)} แถว")
        if changed:
            st.dataframe(pd.DataFrame([
                {"กลุ่ม": g, "ขนาด": b or "-", "ค่าเริ่มต้น": f"{base.total_min} (+{base.buffer_min or 0})",
                 "ค่าที่ใช้อยู่": f"{cur.total_min} (+{cur.buffer_min or 0})",
                 "ต่าง (นาที)": f"{(cur.total_min + (cur.buffer_min or 0)) - (base.total_min + (base.buffer_min or 0)):+d}"}
                for g, b, base, cur in changed]), width="stretch", hide_index=True)

        b = st.columns(4)
        if b[0].button("↺ รีเซ็ตเป็นค่าเริ่มต้น", width="stretch", type="primary",
                       help="กลับไปที่ P75 ของข้อมูลจริง (ไม่ใช่ standard เดิม) รวม Buffer ต่อแถว"):
            st.session_state.std_lorry_rows = std_times.lorry_rows()
            st.session_state.std_yusen_rows = std_times.yusen_rows()
            st.rerun()
        if b[1].button("⏮ กลับไปใช้ Standard เดิม", width="stretch",
                       help="ใช้ตาราง ISO standard ต้นฉบับทั้งหมด พร้อม Buffer 10 นาทีทุกแถว"):
            # the old app paired the LORRY standard with a 10-minute buffer and the
            # Yusen standard with none; rolling back restores that pairing too
            st.session_state.std_lorry_rows = std_times.lorry_rows(
                LORRY_STD_FROM_ORIGINAL, default_buffer=10)
            st.session_state.std_yusen_rows = std_times.yusen_rows(
                YUSEN_STD_FROM_ORIGINAL, default_buffer=0)
            st.rerun()
        b[2].download_button("⬇️ JSON", data=std_times.to_json(lorry_std, yusen_std),
                             file_name=f"standard_times_{dt.date.today():%Y%m%d}.json",
                             mime="application/json", width="stretch")
        b[3].download_button("⬇️ CSV", data=std_times.to_csv(lorry_std, yusen_std),
                             file_name=f"standard_times_{dt.date.today():%Y%m%d}.csv",
                             mime="text/csv", width="stretch")
        st.caption("ค่าที่แก้ที่นี่อยู่ใน session ของเบราว์เซอร์นี้เท่านั้น — "
                   "ดาวน์โหลดเก็บไว้ถ้าต้องการใช้ซ้ำหรือส่งต่อ")


# ---------------------------------------------------------------------------
# page: plan the day
# ---------------------------------------------------------------------------
def _plan_status(result):
    """The one-line verdict shown in the hero bar."""
    if result is None:
        return "info", "ยังไม่ได้จัดคิว", "กรอกรายการ DO แล้วกดปุ่มจัดคิว"
    if result.get("error") == "hard_deadline_conflict":
        return "crit", "เวลาที่ล็อกไว้ชนกัน", "ต้องมีคนตัดสินใจว่าจะเลื่อน DO ไหน"
    if result.get("error"):
        return "crit", "จัดคิวไม่สำเร็จ", "ดูรายละเอียดด้านล่าง"
    if result.get("validation"):
        return "crit", "ผลลัพธ์ไม่ผ่านการตรวจซ้ำ", "ห้ามนำแผนนี้ไปใช้ แจ้งผู้ดูแลระบบ"
    n_ok, n_drop = len(result["schedule"]), len(result["dropped"])
    last = max((r["end"] for r in result["schedule"]), default=None)
    finish = f"คันสุดท้ายออกจากช่อง {min_to_hhmm(last)}" if last else ""
    if n_drop:
        return "warn", f"จัดได้ {n_ok} คัน · ต้องเลื่อนไปวันถัดไป {n_drop} คัน", finish
    return "ok", f"จัดครบทั้ง {n_ok} คันภายในวันนี้", finish


def page_plan():
    subtitle = ("แผนตั้งต้นของวัน — คิดจากเวลามาตรฐาน ช่องที่สินค้าลงได้ "
                "และกล้อง+ตาชั่งที่ใช้ร่วมกันได้ทีละคัน")
    if blackouts:
        subtitle += f" · ⛔ วันนี้ปิดช่อง {maintenance.summarise(maint_items, plan_date, cfg.window_start_min, cfg.window_end_min)}"
    ui_theme.hero(
        f"กระดานจัดคิวรถเข้าช่องโหลด A / B / C — {plan_date:%d/%m/%Y}",
        subtitle,
        f"{min_to_hhmm(cfg.window_start_min)}–{min_to_hhmm(cfg.window_end_min)}",
        "ช่วงเวลาที่โหลดได้วันนี้", *_plan_status(st.session_state.result))

    # ---- 1. the DO list ----------------------------------------------------
    is_empty = st.session_state.do_df.dropna(how="all").empty
    with st.container(border=True):
        ui_theme.head("ขั้น 1", "วันนี้มีรถต้องเข้ากี่คัน",
                      "อัปโหลดไฟล์ DO ของวันนั้น แล้วแก้เพิ่มเติมในตารางได้")
        bc = st.columns([1, 1, 4])
        if bc[0].button("➕ เพิ่มแถว", width="stretch"):
            blank = pd.DataFrame([{c: ("" if c != "Volume (ton)" else None) for c in COLS}],
                                 columns=COLS)
            blank["Margin (min)"] = 0
            st.session_state.do_df = pd.concat([st.session_state.do_df, blank], ignore_index=True)
            st.rerun()
        if bc[1].button("🗑️ เคลียร์ตาราง", width="stretch", disabled=is_empty):
            st.session_state.do_df = empty_df()
            st.session_state.result = None
            st.rerun()

        # open by default while there is nothing to plan - that is the way in
        with st.expander("📤 อัปโหลดรายการ DO จาก Excel / CSV", expanded=is_empty):
            st.download_button("📄 ดาวน์โหลด Excel template (ว่างพร้อม dropdown + คู่มือ)",
                               data=template_bytes(), file_name="DO_Template.xlsx",
                               mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
            st.caption("template มี 3 sheet: **DO List** (กรอกตรงนี้ แอปอ่านเฉพาะ sheet แรก) · "
                       "Example (ตัวอย่างการกรอก) · Guide (ช่องที่ใช้ได้ต่อ product + เวลามาตรฐาน)")
            st.divider()
            up_mode = st.radio("โหมด", ["แทนที่ตารางเดิม", "เพิ่มต่อท้าย"], horizontal=True)
            upload = st.file_uploader("เลือกไฟล์ .xlsx / .xls / .csv", type=["xlsx", "xls", "csv"],
                                      key=f"uploader_{st.session_state.upload_token}")
            st.caption("หัวคอลัมน์ที่รองรับ: " + " · ".join(COLS) +
                       " (ชื่อใกล้เคียง เช่น DO / Volume / Requested ก็อ่านได้)")
            if upload is not None:
                try:
                    raw = (pd.read_csv(upload) if upload.name.lower().endswith(".csv")
                           else pd.read_excel(upload))
                    incoming = normalise_upload(raw)
                    st.session_state.do_df = (incoming if up_mode == "แทนที่ตารางเดิม"
                                              else pd.concat([st.session_state.do_df, incoming],
                                                             ignore_index=True))
                    st.session_state.result = None
                    st.session_state.upload_token += 1
                    st.success(f"อ่านไฟล์ {upload.name} สำเร็จ — {len(incoming)} แถว")
                    st.rerun()
                except Exception as exc:  # noqa: BLE001 - surface any parse failure to the planner
                    st.error(f"อ่านไฟล์ไม่สำเร็จ: {exc}")

        edited = st.data_editor(
            st.session_state.do_df, num_rows="dynamic", width="stretch", height=330, key="editor",
            column_config={
                "DO No.": st.column_config.TextColumn("DO No.", required=False, width="small"),
                "Product": st.column_config.SelectboxColumn("สินค้า", options=PRODUCTS, width="small"),
                "Volume (ton)": st.column_config.NumberColumn("ปริมาณ (ตัน)", min_value=0.0,
                                                              max_value=60.0, step=0.5, format="%.1f"),
                "Transport Co.": st.column_config.SelectboxColumn(
                    "บริษัทขนส่ง", options=TRANSPORT_COMPANIES, width="small",
                    help="Yusen = ISO Tank ใช้เวลามาตรฐานคนละตาราง"),
                "Requested Time": st.column_config.TextColumn(
                    "เวลาที่ล็อกไว้", width="small",
                    help="HH:MM = ต้องโหลดเวลานั้นเป๊ะ · เว้นว่าง = ให้ระบบเลือกเวลาให้"),
                "Margin (min)": st.column_config.NumberColumn(
                    "เผื่อเวลา (นาที)", min_value=0, max_value=120, step=5,
                    help="เวลาเผื่อเฉพาะ DO นี้ บวกต่อจาก Buffer ของแถวในหน้า ⚙️ เวลามาตรฐาน"),
                "Plan truck": st.column_config.TextColumn(
                    "รถ (Plan truck)", width="small",
                    help="ไม่บังคับ — กรอกทะเบียน/รหัสรถเพื่อบังคับเว้นระยะเวลาระหว่างรอบของรถคันเดียวกัน "
                         "(ตั้งค่าใน sidebar > Timing)"),
            },
        )
        st.session_state.do_df = edited
        if is_empty:
            st.info("ยังไม่มีรายการ DO — อัปโหลดไฟล์ของวันนั้นด้านบน "
                    "(กดปุ่มดาวน์โหลด template ถ้ายังไม่มีฟอร์ม) หรือกด ➕ เพิ่มแถว เพื่อพิมพ์เอง",
                    icon="📤")

        jobs, errors, warnings = rows_to_jobs(edited, cfg)
        # a confirmed click-to-reschedule pick (plan mode only) is folded in here as
        # a pin_channel + requested override, so a re-solve is what actually moves
        # the truck - picking a slot never touches the schedule directly
        for j in jobs:
            override = st.session_state.plan_overrides.get(j["id"])
            if override:
                j["pin_channel"], j["requested"] = override
        n_fixed = sum(1 for j in jobs if j["requested"] is not None)
        n_yusen = sum(1 for j in jobs if is_yusen(j["company"]))
        ui_theme.sums([
            (len(jobs), "รถทั้งหมด", "DO ที่กรอกไว้", "accent"),
            (n_fixed, "ล็อกเวลาไว้", "ต้องโหลดตรงเวลาที่ขอ", "warn" if n_fixed else "quiet"),
            (len(jobs) - n_fixed, "ยืดหยุ่น", "ระบบเลือกเวลาให้ได้", ""),
            (n_yusen, "Yusen (ISO Tank)", "ใช้เวลามาตรฐานคนละตาราง", "" if n_yusen else "quiet"),
            (f"{sum(j['volume'] for j in jobs):,.0f}", "ตันรวม", "ปริมาณที่ต้องโหลดวันนี้", "quiet"),
        ])

    for e in errors:
        st.error(e, icon="🚫")
    for w in warnings:
        st.warning(w, icon="⚠️")
    for p in cfg_problems:
        st.error(f"ตั้งค่าไม่ถูกต้อง: {p}", icon="🚫")

    # ---- 2. run ------------------------------------------------------------
    with st.container(border=True):
        ui_theme.head("ขั้น 2", "ให้ระบบจัดคิวให้",
                      "หาแผนที่จัดได้มากที่สุด แล้วเลือกแผนที่จบงานเร็วที่สุดในบรรดานั้น")
        run = st.button("▶️  จัดคิวให้ที (Run Scheduler)", type="primary", width="stretch",
                        disabled=bool(errors or cfg_problems or not jobs))
        if errors or cfg_problems:
            st.caption("แก้ข้อผิดพลาดด้านบนก่อนจึงจะรันได้")
        elif not jobs:
            st.caption("ยังไม่มี DO ในตาราง")
        else:
            st.caption(f"{len(jobs)} DO · LORRY ล้วนราว 15 วินาที · ถ้ามี Yusen เยอะอาจถึง 2–5 นาที")

    if run:
        with st.spinner("กำลังหาคำตอบที่ดีที่สุดด้วย HiGHS solver …"):
            t0 = dt.datetime.now()
            try:
                result = build_and_solve(jobs, config=cfg, time_limit=float(time_limit))
            except Exception as exc:  # noqa: BLE001 - never leave the planner with a blank page
                result = {"error": "engine_exception", "message": str(exc), "schedule": [],
                          "dropped": [], "validation": [], "channel_summary": [], "config": cfg}
            elapsed = (dt.datetime.now() - t0).total_seconds()
        st.session_state.result = result
        st.session_state.run_meta = {"elapsed": elapsed, "n_jobs": len(jobs),
                                     "at": dt.datetime.now().strftime("%H:%M:%S"),
                                     "cfg_label": cfg_label(cfg)}
        # this plan becomes the baseline the mid-day re-planner measures churn against
        if not result.get("error"):
            st.session_state.baseline = baseline_from_schedule(result["schedule"])
            st.session_state.baseline_schedule = result["schedule"]
            st.session_state.baseline_meta = {"at": st.session_state.run_meta["at"],
                                              "cfg_label": cfg_label(cfg)}
            st.session_state.status_sig = None      # status table must be rebuilt
            st.session_state.replan_result = None
        st.rerun()

    # ---- 3. results --------------------------------------------------------
    result = st.session_state.result
    if result is None:
        st.info("กดปุ่มด้านบนเพื่อจัดคิว — ผลลัพธ์จะค้างอยู่จนกว่าจะสั่งรันใหม่", icon="👆")
        return

    meta = st.session_state.run_meta or {}
    res_cfg = result.get("config") or cfg
    with st.container(border=True):
        ui_theme.head("ขั้น 3", "แผนของวันนี้ออกมาเป็นแบบไหน",
                      f"จัดเมื่อ {meta.get('at', '-')} · ใช้เวลา {meta.get('elapsed', 0):.1f} วินาที "
                      f"· {meta.get('cfg_label', '-')}")

        if meta.get("cfg_label") and meta["cfg_label"] != cfg_label(cfg):
            # covers the standard-time tables too now, not just the sidebar
            st.warning("Settings ใน sidebar หรือเวลามาตรฐาน เปลี่ยนไปหลังจัดคิวครั้งล่าสุด — "
                       "แผนด้านล่างยังเป็นของค่าเดิม กดจัดคิวอีกครั้งเพื่ออัปเดต", icon="⚠️")

        if result.get("error") == "hard_deadline_conflict":
            render_conflict(result, cfg, time_limit)
            return
        if result.get("error"):
            st.error(f"Solver ไม่สามารถหาคำตอบได้: {result.get('message', result['error'])}")
            if result.get("dropped"):
                st.dataframe(dropped_dataframe(result["dropped"]), width="stretch", hide_index=True)
            return

        schedule, dropped = result["schedule"], result["dropped"]
        last = max((r["end"] for r in schedule), default=None)
        ui_theme.sums([
            (len(schedule), "จัดคิวได้", f"จาก {len(schedule) + len(dropped)} คัน", "ok"),
            (sum(1 for r in schedule if r["is_fixed"]), "ล็อกเวลาไว้ และได้ตามนั้น",
             "ไม่มีคันไหนถูกเลื่อนเงียบ ๆ", "accent"),
            (len(dropped), "ต้องเลื่อนไปวันถัดไป", "โหลดไม่ทันในวันนี้",
             "crit dashed" if dropped else "quiet"),
            (min_to_hhmm(last) if last else "-", "คันสุดท้ายเสร็จ",
             f"ปิดงาน {min_to_hhmm(res_cfg.window_end_min)}", ""),
            (f"{sum(r['volume'] for r in schedule):,.0f}", "ตันที่ส่งออกได้", "รวมทุกช่อง", "quiet"),
        ])
        render_selfcheck(result)
        render_results(result, res_cfg, key="plan")
        st.info("แผนนี้ถูกเก็บเป็น **แผนหลัก** แล้ว — ระหว่างวันถ้ารถมาไม่ตรงเวลา "
                "ให้สลับไปโหมด 🔄 ปรับแผนระหว่างวัน ที่ sidebar", icon="📌")

    reschedule_result = None
    movable = _movable_rows(schedule)
    if movable:
        movable_ids = {r["id"] for r in movable}
        if st.session_state.get("reschedule_pick") not in movable_ids:
            st.session_state["reschedule_pick"] = movable[0]["id"]
        target_job = next(r for r in movable if r["id"] == st.session_state["reschedule_pick"])
        with st.container(border=True):
            ui_theme.head("ขั้น 4", "อยากย้ายคิวไหนไหม",
                          "คลิกการ์ดคิวในปฏิทิน (ขั้น 3) เพื่อเลือก แล้วคลิกช่วงเวลาที่ว่างในตารางด้านล่าง")
            st.caption(f"กำลังเลือก: **{target_job['id']} · {target_job['product']} · "
                       f"ช่อง {target_job['channel']} {target_job['start_hhmm']}**")
            reschedule_result = ui_reschedule.render(target_job, schedule, res_cfg)

    if reschedule_result:
        do_id = reschedule_result.get("do_id")
        new_channel, new_start = reschedule_result.get("channel"), reschedule_result.get("start_min")
        st.session_state.plan_overrides[do_id] = (new_channel, new_start)
        for j in jobs:
            if j["id"] == do_id:
                j["pin_channel"], j["requested"] = new_channel, new_start
        with st.spinner(f"กำลังย้าย {do_id} ไปช่อง {new_channel} {min_to_hhmm(new_start)} แล้วจัดคิวใหม่ …"):
            t0 = dt.datetime.now()
            try:
                new_result = build_and_solve(jobs, config=cfg, time_limit=float(time_limit))
            except Exception as exc:  # noqa: BLE001 - never leave the planner with a blank page
                new_result = {"error": "engine_exception", "message": str(exc), "schedule": [],
                              "dropped": [], "validation": [], "channel_summary": [], "config": cfg}
            elapsed = (dt.datetime.now() - t0).total_seconds()
        st.session_state.result = new_result
        st.session_state.run_meta = {"elapsed": elapsed, "n_jobs": len(jobs),
                                     "at": dt.datetime.now().strftime("%H:%M:%S"),
                                     "cfg_label": cfg_label(cfg)}
        if not new_result.get("error"):
            st.session_state.baseline = baseline_from_schedule(new_result["schedule"])
            st.session_state.baseline_schedule = new_result["schedule"]
            st.session_state.baseline_meta = {"at": st.session_state.run_meta["at"],
                                              "cfg_label": cfg_label(cfg)}
            st.session_state.status_sig = None
            st.session_state.replan_result = None
        st.rerun()


# ---------------------------------------------------------------------------
# page: re-plan mid-day
# ---------------------------------------------------------------------------
def default_status_df(baseline_schedule, now_min) -> pd.DataFrame:
    rows = []
    for r in sorted(baseline_schedule, key=lambda x: x["start"]):
        if r["end"] <= now_min:
            status = STATUS_DONE
        elif r["start"] <= now_min < r["end"]:
            status = STATUS_LOADING
        else:
            status = STATUS_WAITING
        rows.append({
            "DO No.": r["id"], "Product": r["product"],
            "แผนเดิม": f"{r['channel']} {r['start_hhmm']}-{r['end_hhmm']}",
            "สถานะ": status, "เริ่มจริง": "", "ETA override": "",
        })
    return pd.DataFrame(rows)


def _replan_status(res, n_late, n_ready):
    if res is None:
        if n_late:
            return "warn", f"มี {n_late} คันที่จะมาไม่ทันคิวเดิม", "กดจัดคิวใหม่เพื่อปรับแผน"
        return "info", "ยังไม่ได้จัดคิวใหม่", "ใส่สถานะจริงและ GPS แล้วกดจัดคิวใหม่"
    if res.get("error"):
        return "crit", "จัดคิวใหม่ไม่สำเร็จ", "ดูรายละเอียดด้านล่าง"
    if res.get("validation"):
        return "crit", "ผลลัพธ์ไม่ผ่านการตรวจซ้ำ", "ห้ามนำแผนนี้ไปใช้"
    n_drop, n_change = len(res["dropped"]), len(res.get("baseline_diff") or [])
    if n_drop:
        return "warn", f"ปรับแผนแล้ว · หลุดไปวันถัดไป {n_drop} คัน", f"ขยับจากแผนเดิม {n_change} คัน"
    if n_change:
        return "ok", f"ปรับแผนแล้ว · ยังจัดได้ครบ", f"ขยับจากแผนเดิม {n_change} คัน · รถที่รออยู่ถูกดึงมาอุดช่องว่าง"
    return "ok", "ไม่ต้องเปลี่ยนอะไร", "แผนเดิมยังใช้ได้ทั้งหมด"


def page_replan():
    baseline_schedule = st.session_state.baseline_schedule
    if not baseline_schedule:
        ui_theme.hero("ปรับแผนระหว่างวัน", "แช่แข็งคิวที่เกิดไปแล้ว แล้วจัดคิวที่เหลือใหม่จากตำแหน่งรถจริง",
                      "--:--", "ยังไม่มีแผนหลัก", "info", "ต้องมีแผนตั้งต้นก่อน",
                      "ไปโหมดวางแผนต้นวันแล้วกดจัดคิว")
        st.info("ยังไม่มีแผนหลัก — ไปที่โหมด **📋 วางแผนต้นวัน** แล้วกด Run Scheduler ก่อน "
                "แผนที่ได้จะถูกใช้เป็น baseline ของหน้านี้อัตโนมัติ", icon="📌")
        return

    # the hero's verdict depends on values computed further down, so its slot is
    # reserved here and filled at the end - it still renders at the top of the page
    hero_slot = st.container()
    plan_by_id = {r["id"]: r for r in baseline_schedule}
    bmeta = st.session_state.baseline_meta or {}

    # ---- 1. now ------------------------------------------------------------
    now_default = dt.datetime.now().time().replace(second=0, microsecond=0)
    now_default = now_default.replace(minute=now_default.minute - now_default.minute % 5)

    sig = tuple(sorted(r["id"] for r in baseline_schedule))
    with st.container(border=True):
        ui_theme.head("ขั้น 1", "ตอนนี้กี่โมง และคิวไหนเกิดไปแล้วบ้าง",
                      f"แผนหลัก {len(baseline_schedule)} คิว · วางไว้เมื่อ {bmeta.get('at', '-')}")
        c = st.columns([1, 1.2, 3])
        now_t = c[0].time_input("ตอนนี้", now_default, step=300, key="replan_now")
        now_min = hhmm_to_min(now_t)
        if c[1].button("↻ ตั้งสถานะตามเวลา", width="stretch",
                       help="กำหนด เสร็จแล้ว / กำลังโหลด / รอเข้า ให้ทุกคิวตามแผนเดิมเทียบกับเวลานี้"):
            st.session_state.status_df = default_status_df(baseline_schedule, now_min)
            st.session_state.status_sig = sig
            st.rerun()

        if st.session_state.status_sig != sig or st.session_state.status_df is None:
            st.session_state.status_df = default_status_df(baseline_schedule, now_min)
            st.session_state.status_sig = sig

        status_df = st.data_editor(
            st.session_state.status_df, width="stretch", height=290, key="status_editor",
            column_config={
                "DO No.": st.column_config.TextColumn("DO No.", disabled=True, width="small"),
                "Product": st.column_config.TextColumn("สินค้า", disabled=True, width="small"),
                "แผนเดิม": st.column_config.TextColumn("แผนเดิม", disabled=True),
                "สถานะ": st.column_config.SelectboxColumn("สถานะจริง", options=STATUSES, width="small"),
                "เริ่มจริง": st.column_config.TextColumn(
                    "เริ่มจริง", width="small",
                    help="HH:MM — ใส่เมื่อรถเข้าจริงไม่ตรงแผน (ใช้กับ กำลังโหลด/เสร็จแล้ว) "
                         "เว้นว่าง = ใช้เวลาตามแผน"),
                "ETA override": st.column_config.TextColumn(
                    "ETA ที่รู้แน่", width="small",
                    help="HH:MM — ถ้าโทรถามคนขับแล้วรู้เวลาแน่นอน ใส่ตรงนี้เพื่อทับค่าที่คำนวณจาก GPS"),
            },
        )
        st.session_state.status_df = status_df

        counts = status_df["สถานะ"].value_counts().to_dict()
        ui_theme.sums([
            (counts.get(STATUS_DONE, 0), "เสร็จแล้ว", "ออกจากช่องไปแล้ว", "quiet"),
            (counts.get(STATUS_LOADING, 0), "กำลังโหลด", "อยู่ในช่อง ห้ามขยับ", "accent"),
            (counts.get(STATUS_WAITING, 0), "ยังรอเข้า", "คิวที่จัดใหม่ได้", "ok"),
            (counts.get(STATUS_CANCELLED, 0), "ยกเลิก", "ตัดออกจากแผน",
             "warn" if counts.get(STATUS_CANCELLED, 0) else "quiet"),
        ])

    pending_ids = [r["DO No."] for _, r in status_df.iterrows() if r["สถานะ"] == STATUS_WAITING]

    # ---- 2. GPS ------------------------------------------------------------
    with st.container(border=True):
        ui_theme.head("ขั้น 2", "รถที่ยังไม่เข้า ตอนนี้อยู่ไหน",
                      "ระยะทางจาก GPS → เวลาที่เร็วที่สุดที่รถคันนั้นเริ่มโหลดได้")
        gps_empty = (st.session_state.gps_df is None
                     or st.session_state.gps_df.dropna(how="all").empty)
        g = st.columns([1, 4])
        if g[0].button("🗑️ ล้าง GPS", width="stretch", disabled=gps_empty):
            st.session_state.gps_df = pd.DataFrame(columns=GPS_COLS)
            st.rerun()

        with st.expander("📤 อัปโหลดพิกัด GPS จาก Excel / CSV", expanded=gps_empty):
            gps_upload = st.file_uploader("ไฟล์ที่มีคอลัมน์ DO No. / Lat / Lon / Last seen",
                                          type=["xlsx", "xls", "csv"],
                                          key=f"gps_uploader_{st.session_state.gps_token}")
            if gps_upload is not None:
                try:
                    raw = (pd.read_csv(gps_upload) if gps_upload.name.lower().endswith(".csv")
                           else pd.read_excel(gps_upload))
                    st.session_state.gps_df = normalise_gps_upload(raw)
                    st.session_state.gps_token += 1
                    st.success(f"อ่าน {gps_upload.name} สำเร็จ — {len(st.session_state.gps_df)} แถว")
                    st.rerun()
                except Exception as exc:  # noqa: BLE001
                    st.error(f"อ่านไฟล์ไม่สำเร็จ: {exc}")

        if st.session_state.gps_df is None:
            st.session_state.gps_df = pd.DataFrame(columns=GPS_COLS)
        gps_df = st.data_editor(
            st.session_state.gps_df, num_rows="dynamic", width="stretch", height=210,
            key="gps_editor",
            column_config={
                "DO No.": st.column_config.TextColumn("DO No.", width="small"),
                "Lat": st.column_config.NumberColumn("Lat", format="%.5f", step=0.001),
                "Lon": st.column_config.NumberColumn("Lon", format="%.5f", step=0.001),
                "Last seen": st.column_config.TextColumn("สัญญาณล่าสุด", width="small",
                                                         help="HH:MM ของ fix ล่าสุด"),
            },
        )
        st.session_state.gps_df = gps_df

        gps_rows = []
        for _, r in gps_df.iterrows():
            do_id = str(r.get("DO No.") or "").strip()
            if not do_id or pd.isna(r.get("Lat")) or pd.isna(r.get("Lon")):
                continue
            gps_rows.append({"id": do_id, "lat": float(r["Lat"]), "lon": float(r["Lon"]),
                             "last_seen_min": hhmm_to_min(r.get("Last seen"))})
        etas = fleet_etas(gps_rows, now_min, travel)

        if gps_rows and is_placeholder_plant(travel):
            st.error(
                "📍 **พิกัดโรงงานยังเป็นค่าตั้งต้น ยังไม่ได้ตั้งเป็นตาชั่งจริง** — "
                f"ตอนนี้วัดระยะจาก {travel.plant_lat:.4f}, {travel.plant_lon:.4f} "
                "ซึ่งเป็นเพียงจุดคร่าว ๆ **ETA ทุกคันจึงยังเชื่อถือไม่ได้** "
                "ตั้งค่าจริงที่ deploy-time secrets หรือพิมพ์ทับที่ sidebar → 🛰️ GPS / ETA",
                icon="🚫")

        overrides = {}
        for _, r in status_df.iterrows():
            ov = hhmm_to_min(r.get("ETA override"))
            if ov is not None:
                overrides[str(r["DO No."]).strip()] = ov

        pending_rows = [{"id": i, "product": plan_by_id.get(i, {}).get("product", ""),
                         "planned_start": plan_by_id.get(i, {}).get("start")} for i in pending_ids]
        gps_table = eta_table(pending_rows, etas, now_min)
        if not gps_table.empty:
            for idx, row in gps_table.iterrows():
                if row["DO No."] in overrides:
                    gps_table.at[idx, "ETA"] = min_to_hhmm(overrides[row["DO No."]])
                    gps_table.at[idx, "Status"] = "✍️ ระบุเอง"

        ready_now = ([r["DO No."] for _, r in gps_table.iterrows()
                      if r["Status"] in (STATUS_AT_PLANT, STATUS_NEAR)]
                     if not gps_table.empty else [])
        late = ([r["DO No."] for _, r in gps_table.iterrows()
                 if str(r["ช้ากว่าแผน (นาที)"]).startswith("+")] if not gps_table.empty else [])

        ui_theme.sums([
            (len(pending_ids), "ยังรอเข้า", "คิวที่จัดใหม่ได้", ""),
            (len(ready_now), "พร้อมเสียบคิวได้เลย", "ถึงแล้วหรือใกล้ถึง", "ok" if ready_now else "quiet"),
            (len(late), "จะมาไม่ทันคิวเดิม", "ETA เลยเวลาที่วางไว้", "crit" if late else "quiet"),
            (len(gps_rows), "มีสัญญาณ GPS", f"จาก {len(pending_ids)} คันที่รออยู่",
             "quiet" if len(gps_rows) >= len(pending_ids) else "warn dashed"),
        ])
        if ready_now:
            st.success("พร้อมเสียบคิวได้ทันที: " + ", ".join(ready_now[:12]) +
                       (" …" if len(ready_now) > 12 else ""), icon="🟢")
        if late:
            st.warning("มาไม่ทันคิวเดิม: " + ", ".join(late[:12]) +
                       (" …" if len(late) > 12 else ""), icon="🔴")
        if len(gps_rows) < len(pending_ids):
            st.caption(f"⚪ อีก {len(pending_ids) - len(gps_rows)} คันไม่มีสัญญาณ GPS — "
                       "ระบบจะถือว่าพร้อมตั้งแต่ตอนนี้ ถ้าไม่จริงให้ใส่ ETA ที่รู้แน่ในตารางขั้น 1")

    # ---- 3. re-optimise ----------------------------------------------------
    with st.container(border=True):
        ui_theme.head("ขั้น 3", "รถที่มาไม่ทัน จะทำยังไง",
                      "คิวที่เริ่มไปแล้วถูกแช่แข็ง · ไม่มีคิวไหนเริ่มก่อนรถถึง · ขยับแผนเดิมเท่าที่จำเป็น")

        swap_mode = st.radio(
            "วิธีจัดการ", [MODE_AUTO, MODE_MANUAL], horizontal=True, key="swap_mode",
            help="Auto = ให้ solver จัดคิวใหม่ทั้งกระดานเอง · "
                 "Manual = ระบบเสนอคันแทนให้เลือก แล้วค่อยจัดคิวรอบเดียวตามที่เลือก")

        policy = swap_planner.SwapPolicy()
        if swap_mode == MODE_AUTO:
            st.caption("ระบบจะดันคันที่มาสายออกไปตาม ETA และดึงคันที่พร้อมแล้วเข้ามาอุดช่องว่างให้เอง "
                       "โดยพยายามไม่ขยับแผนเดิมเกินจำเป็น")
        else:
            with st.expander("⚙️ เกณฑ์การหาคันแทน", expanded=False):
                pc = st.columns(3)
                policy = swap_planner.SwapPolicy(
                    late_threshold_min=pc[0].number_input(
                        "เตือนเมื่อจะสายเกิน (นาที)", 0, 120, 15, 5,
                        help="สายไม่เกินนี้ถือว่ายังรับได้ ไม่รบกวนกระดาน"),
                    lead_min=pc[1].number_input(
                        "คันแทนต้องพร้อมก่อนคิว (นาที)", 0, 120, 30, 5,
                        help="กันเวลาเผื่อให้รถเข้าประจำที่ทัน"),
                    notice_min=pc[2].number_input(
                        "แจ้งคนขับล่วงหน้า (นาที)", 0, 180, 60, 15,
                        help="ใช้เฉพาะคันที่ยังอยู่บนถนน · คันที่จอดรออยู่แล้วเรียกเข้าได้ทันที"),
                    bumped=(swap_planner.BUMPED_ETA if st.session_state.get("bumped_ui", True)
                            else swap_planner.BUMPED_TAIL))
                bumped_eta = st.radio(
                    "คันที่ถูกแทน ให้ไปอยู่ตรงไหน",
                    ["เสียบกลับตามเวลาที่รถถึงจริง (แนะนำ)", "ต่อท้ายคิวของช่องนั้น (กติกาเดิม)"],
                    horizontal=True, key="bumped_choice")
                policy.bumped = (swap_planner.BUMPED_ETA
                                 if bumped_eta.startswith("เสียบกลับ") else swap_planner.BUMPED_TAIL)
                st.caption(policy.describe())

            pending_full = []
            for do_id in pending_ids:
                plan = plan_by_id.get(do_id)
                if plan is None:
                    continue
                eta = overrides.get(do_id)
                if eta is None:
                    eta = (etas.get(do_id) or {}).get("eta_min")
                pending_full.append({
                    "id": do_id, "product": plan["product"], "company": plan.get("company", ""),
                    "planned_channel": plan["channel"], "planned_start": plan["start"],
                    "planned_end": plan["end"], "eta": eta})
            board = swap_planner.inbound_board(pending_full, now_min, policy)
            render_swap_board(board, policy, now_min)
            if st.session_state.swap_decisions or st.session_state.swap_dismissed:
                if st.button("↺ ล้างการตัดสินใจทั้งหมด", key="clear_swaps"):
                    st.session_state.swap_decisions = []
                    st.session_state.swap_dismissed = []
                    st.rerun()
            st.divider()

        auto_release = st.checkbox(
            "ปลดล็อกเวลาของคิวที่เลยเวลาที่ขอไปแล้ว (ถือเป็นคิวยืดหยุ่น)", value=True,
            help="ถ้าไม่ติ๊ก คิวที่เวลาที่ขออยู่ในอดีตจะกลายเป็น conflict ที่ต้องแก้ด้วยมือ")

        all_jobs, errors, warnings = rows_to_jobs(st.session_state.do_df, cfg)
        by_id = {j["id"]: j for j in all_jobs}
        status_by_id = {str(r["DO No."]).strip(): r for _, r in status_df.iterrows()}

        jobs, released, missing = [], [], []
        for do_id, job in by_id.items():
            row = status_by_id.get(do_id)
            status = (row["สถานะ"] if row is not None else STATUS_WAITING)
            if status == STATUS_CANCELLED:
                continue
            job = dict(job, status=status)
            if status in (STATUS_LOADING, STATUS_DONE):
                plan = plan_by_id.get(do_id)
                if plan is None:
                    missing.append(do_id)
                    continue
                actual = hhmm_to_min(row["เริ่มจริง"]) if row is not None else None
                job["locked_channel"] = plan["channel"]
                job["locked_start"] = actual if actual is not None else plan["start"]
            else:
                eta = overrides.get(do_id)
                if eta is None:
                    eta = (etas.get(do_id) or {}).get("eta_min")
                job["earliest"] = eta
                if job["requested"] is not None and job["requested"] < now_min and auto_release:
                    released.append(do_id)
                    job["requested"] = None
            jobs.append(job)

        if swap_mode == MODE_MANUAL and st.session_state.swap_decisions:
            jobs, applied = swap_planner.apply_decisions(
                jobs, st.session_state.swap_decisions, baseline_schedule, policy, now_min)
            for a in applied:
                if a["result"] == "applied":
                    st.info(a["why"], icon="📌")
                else:
                    st.warning(f"{a['replacement_id']}: {a['why']}", icon="⚠️")

        if missing:
            st.warning("คิวที่ทำเครื่องหมายว่าเริ่มแล้วแต่ไม่อยู่ในแผนหลัก (ไม่รู้ว่าอยู่ช่องไหน): "
                       + ", ".join(missing), icon="⚠️")
        if released:
            st.info("คิวที่เลยเวลาที่ขอไปแล้ว ถูกถือเป็นคิวยืดหยุ่นในรอบนี้: " + ", ".join(released),
                    icon="🔓")
        for e in errors:
            st.error(e, icon="🚫")
        for p in cfg_problems:
            st.error(f"ตั้งค่าไม่ถูกต้อง: {p}", icon="🚫")

        go = st.button("🔄  จัดคิวใหม่จากเวลานี้", type="primary", width="stretch",
                       disabled=bool(errors or cfg_problems or not jobs))

    if go:
        with st.spinner("กำลังจัดคิวใหม่ …"):
            t0 = dt.datetime.now()
            try:
                res = build_and_solve(jobs, config=cfg, time_limit=float(time_limit),
                                      now_min=now_min, baseline=st.session_state.baseline,
                                      stability=stability)
            except Exception as exc:  # noqa: BLE001
                res = {"error": "engine_exception", "message": str(exc), "schedule": [],
                       "dropped": [], "validation": [], "channel_summary": [], "config": cfg}
            elapsed = (dt.datetime.now() - t0).total_seconds()
        st.session_state.replan_result = res
        st.session_state.replan_meta = {"elapsed": elapsed, "at": min_to_hhmm(now_min),
                                        "n_jobs": len(jobs), "gps": len(gps_rows)}
        st.rerun()

    res = st.session_state.replan_result
    with hero_slot:
        ui_theme.hero(
            "ปรับแผนระหว่างวัน",
            "แช่แข็งคิวที่เกิดไปแล้ว → เอา ETA จาก GPS มาเป็นเวลาที่เร็วที่สุดที่เริ่มได้ → จัดคิวที่เหลือใหม่",
            min_to_hhmm(now_min), "เวลาที่ใช้อ้างอิง",
            *_replan_status(res, len(late), len(ready_now)))

    if res is None:
        st.info("กด **จัดคิวใหม่จากเวลานี้** เพื่อปรับแผนจากสถานะปัจจุบัน", icon="👆")
        return

    # ---- 4. the new plan ---------------------------------------------------
    rmeta = st.session_state.replan_meta or {}
    with st.container(border=True):
        ui_theme.head("ขั้น 4", "แผนใหม่ต่างจากเดิมตรงไหน",
                      f"จัดใหม่ ณ {rmeta.get('at', '-')} · {rmeta.get('n_jobs', 0)} คิว "
                      f"· ใช้ GPS {rmeta.get('gps', 0)} คัน · {rmeta.get('elapsed', 0):.1f} วินาที")

        if res.get("error") == "hard_deadline_conflict":
            render_conflict(res)
            return
        if res.get("error"):
            st.error(f"Solver ไม่สามารถหาคำตอบได้: {res.get('message', res['error'])}")
            if res.get("dropped"):
                st.dataframe(dropped_dataframe(res["dropped"], with_eta=True),
                             width="stretch", hide_index=True)
            return

        sched, drop = res["schedule"], res["dropped"]
        diff = res.get("baseline_diff") or []
        last = max((r["end"] for r in sched), default=None)
        ui_theme.sums([
            (len(sched), "จัดคิวได้", f"จาก {len(sched) + len(drop)} คัน", "ok"),
            (res.get("n_locked", 0), "แช่แข็งไว้", "กำลังโหลด / เสร็จแล้ว", "quiet"),
            (len(diff), "ขยับจากแผนเดิม", "ดูรายการในแท็บถัดไป", "warn" if diff else "quiet"),
            (len(drop), "หลุดไปวันถัดไป", "โหลดไม่ทันในวันนี้", "crit dashed" if drop else "quiet"),
            (min_to_hhmm(last) if last else "-", "คันสุดท้ายเสร็จ",
             f"ปิดงาน {min_to_hhmm((res.get('config') or cfg).window_end_min)}", ""),
        ])
        render_selfcheck(res)
        render_results(res, res.get("config") or cfg, now_min=now_min, gps_table=gps_table,
                       key="replan")

        if st.button("📌 ใช้แผนใหม่นี้เป็นแผนหลัก",
                     help="รอบถัดไปจะวัดการเปลี่ยนแปลงเทียบกับแผนนี้แทน"):
            st.session_state.baseline = baseline_from_schedule(sched)
            st.session_state.baseline_schedule = sched
            st.session_state.baseline_meta = {"at": min_to_hhmm(now_min), "cfg_label": cfg_label(cfg)}
            st.session_state.status_sig = None
            st.success("บันทึกเป็นแผนหลักแล้ว")
            st.rerun()


# ---------------------------------------------------------------------------
if mode == MODE_PLAN:
    page_plan()
elif mode == MODE_REPLAN:
    page_replan()
else:
    page_standard_times()
