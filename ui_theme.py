"""
Look and feel, kept in one place.

The visual language follows the F7/F8 Truck Slot Board so the two tools read as
one system: a grey canvas with white cards, one blue accent, semantic ok / warn /
crit colours carried by a 2px border plus a soft fill (never colour alone), every
number in a tabular mono face, and section headings written as the question the
section answers rather than as a feature name.

Everything here is presentation only - no scheduling logic lives in this module.
"""
import streamlit as st

# --- design tokens (kept in sync with .streamlit/config.toml) ----------------
INK = "#0E1720"
INK_2 = "#33454F"
MUTED = "#5B6F7D"
FAINT = "#8497A4"
LINE = "#C9D4DC"
LINE_2 = "#DEE5EB"
GROUND = "#E7ECF0"
SURFACE = "#FFFFFF"
SURFACE_2 = "#F2F5F8"
SURFACE_3 = "#E3E9EE"
ACCENT = "#0B5FA5"
OK = "#137248"
WARN = "#A96A0B"
CRIT = "#A8331F"

FONT_SANS = '"IBM Plex Sans Thai","IBM Plex Sans","Noto Sans Thai",system-ui,-apple-system,sans-serif'
FONT_MONO = '"IBM Plex Mono",ui-monospace,"Cascadia Mono",Menlo,monospace'

# Product colours stay grouped by family exactly as the business rules require
# (MAA red/orange, MMA purple, i-BMA blue, n-BMA green); only the hues were
# re-tuned to the board's muted, print-like tone.
PRODUCT_COLORS = {
    "MAA1": "#A24F1D", "MAA2": "#C2703A", "MAA3": "#7E3B15",
    "MMA1": "#6B4E9E", "MMA2": "#8F73BE",
    "i-BMA": "#14719B",
    "n-BMA1": "#237A48", "n-BMA2": "#4E9E72",
}
FAMILY_COLORS = {"MAA": "#A24F1D", "MMA": "#6B4E9E", "i-BMA": "#14719B", "n-BMA": "#237A48"}

_CSS = """
<style>
@import url('https://fonts.googleapis.com/css2?family=IBM+Plex+Sans+Thai:wght@400;500;600;700&family=IBM+Plex+Mono:wght@500;600;700&display=swap');

:root{
  --ground:#E7ECF0; --surface:#FFFFFF; --surface-2:#F2F5F8; --surface-3:#E3E9EE;
  --ink:#0E1720; --ink-2:#33454F; --muted:#5B6F7D; --faint:#8497A4;
  --line:#C9D4DC; --line-2:#DEE5EB;
  --accent:#0B5FA5; --accent-soft:#DCEAF6; --accent-line:#8FBEDF; --on-accent:#FFFFFF;
  --ok:#137248; --ok-soft:#D9EDE3; --ok-line:#8CC7AC;
  --warn:#A96A0B; --warn-soft:#F8EBD3; --warn-line:#E0BE83;
  --crit:#A8331F; --crit-soft:#F9E1DC; --crit-line:#E0A398;
  --shadow:0 1px 2px rgba(14,23,32,.07), 0 6px 18px rgba(14,23,32,.06);
  --radius:14px;
}

html, body, [data-testid="stAppViewContainer"], [data-testid="stSidebar"]{
  font-family:__SANS__;
}
[data-testid="stAppViewContainer"]{ background:var(--ground); }
[data-testid="stHeader"]{ background:transparent; }
.block-container{ padding-top:1.6rem; padding-bottom:3rem; max-width:1720px; }
h1,h2,h3{ font-weight:700; letter-spacing:-.01em; }
.mono{ font-family:__MONO__; font-variant-numeric:tabular-nums; }

/* ---- sidebar reads as a control panel, not a second page ---- */
[data-testid="stSidebar"]{ border-right:1px solid var(--line); }
[data-testid="stSidebar"] .stRadio label{ font-weight:600; }
.tm-modelabel{ font-size:11px; font-weight:700; letter-spacing:.08em; text-transform:uppercase;
  color:var(--faint); margin-bottom:4px; }
[data-testid="stSidebar"] [data-testid="stExpander"] summary{ font-weight:600; font-size:14px; }

/* ---- every st.container(border=True) becomes a card ---- */
[data-testid="stVerticalBlockBorderWrapper"]:has(> div > [data-testid="stVerticalBlock"]){
  background:var(--surface); border:1px solid var(--line) !important;
  border-radius:var(--radius); box-shadow:var(--shadow);
}
[data-testid="stSidebar"] [data-testid="stVerticalBlockBorderWrapper"]{ box-shadow:none; }

/* ---- hero bar ---- */
.tm-hero{ background:var(--surface); border:1px solid var(--line); border-radius:var(--radius);
  box-shadow:var(--shadow); padding:14px 20px; margin-bottom:16px;
  display:flex; align-items:center; gap:22px; flex-wrap:wrap; }
.tm-hero .ttl{ display:flex; flex-direction:column; line-height:1.2; }
.tm-hero .ttl .b{ font-size:21px; font-weight:700; color:var(--ink); }
.tm-hero .ttl .s{ font-size:13px; color:var(--muted); }
.tm-hero .clock{ font-family:__MONO__; font-weight:700; font-size:34px; line-height:1;
  letter-spacing:-.02em; color:var(--ink); }
.tm-hero .clock small{ display:block; font-family:__SANS__; font-size:11px; font-weight:500;
  color:var(--muted); letter-spacing:0; margin-top:3px; }
.tm-status{ margin-left:auto; display:flex; align-items:center; gap:11px; padding:9px 18px;
  border-radius:10px; border:2px solid var(--ok-line); background:var(--ok-soft); max-width:680px; }
.tm-status.warn{ border-color:var(--warn-line); background:var(--warn-soft); }
.tm-status.crit{ border-color:var(--crit-line); background:var(--crit-soft); }
.tm-status.info{ border-color:var(--accent-line); background:var(--accent-soft); }
.tm-status .dot{ width:13px; height:13px; border-radius:50%; background:var(--ok); flex:none; }
.tm-status.warn .dot{ background:var(--warn); }
.tm-status.crit .dot{ background:var(--crit); }
.tm-status.info .dot{ background:var(--accent); }
.tm-status .txt{ font-size:15px; font-weight:600; line-height:1.3; color:var(--ink); }
.tm-status .txt small{ display:block; font-size:12px; font-weight:500; color:var(--muted); }

/* ---- section header inside a card ---- */
.tm-head{ display:flex; align-items:center; gap:12px; flex-wrap:wrap;
  padding:2px 0 12px; margin-bottom:12px; border-bottom:1px solid var(--line-2); }
.tm-head h2{ font-size:17px; margin:0; color:var(--ink); }
.tm-head .hint{ margin-left:auto; font-size:13px; color:var(--muted); }
.tm-pill{ font-size:11.5px; font-weight:700; letter-spacing:.08em; text-transform:uppercase;
  padding:4px 10px; border-radius:20px; background:var(--accent); color:var(--on-accent);
  white-space:nowrap; }
.tm-pill.quiet{ background:var(--surface-3); color:var(--muted); }
.tm-pill.ok{ background:var(--ok); color:#fff; }
.tm-pill.warn{ background:var(--warn); color:#fff; }
.tm-pill.crit{ background:var(--crit); color:#fff; }

/* ---- summary tiles ---- */
.tm-sums{ display:flex; gap:10px; flex-wrap:wrap; margin:2px 0 6px; }
.tm-sum{ display:flex; align-items:center; gap:10px; border:2px solid var(--line);
  border-radius:11px; padding:9px 14px; background:var(--surface-2); min-width:172px; flex:1 1 172px; }
.tm-sum .n{ font-family:__MONO__; font-weight:700; font-size:26px; line-height:1;
  font-variant-numeric:tabular-nums; color:var(--ink); }
.tm-sum .l{ font-size:12.5px; line-height:1.3; font-weight:600; color:var(--ink-2); }
.tm-sum .l small{ display:block; font-size:11px; color:var(--muted); font-weight:500; }
.tm-sum.accent{ border-color:var(--accent-line); background:var(--accent-soft); }
.tm-sum.accent .n{ color:var(--accent); }
.tm-sum.ok{ border-color:var(--ok-line); background:var(--ok-soft); } .tm-sum.ok .n{ color:var(--ok); }
.tm-sum.warn{ border-color:var(--warn-line); background:var(--warn-soft); } .tm-sum.warn .n{ color:var(--warn); }
.tm-sum.crit{ border-color:var(--crit-line); background:var(--crit-soft); } .tm-sum.crit .n{ color:var(--crit); }
.tm-sum.quiet .n{ color:var(--muted); }
.tm-sum.dashed{ border-style:dashed; }

/* ---- inbound rows: one waiting truck per line ---- */
.tm-row{ display:flex; align-items:center; gap:12px; flex-wrap:wrap; }
.tm-chb{ width:30px; height:30px; border-radius:8px; display:grid; place-items:center; flex:none;
  font-family:__MONO__; font-weight:700; font-size:15px; background:var(--accent); color:#fff; }
.tm-row .main{ flex:1; min-width:230px; display:grid; gap:2px; }
.tm-row .l1{ display:flex; gap:8px; align-items:baseline; flex-wrap:wrap; font-size:14px; }
.tm-row .l1 .do{ font-family:__MONO__; font-weight:700; font-size:15px; }
.tm-row .l1 .who{ color:var(--muted); }
.tm-row .l2{ font-family:__MONO__; font-size:12px; color:var(--muted); }
.tm-row .l2 b{ color:var(--ink-2); }
.tm-stat{ font-size:12px; font-weight:700; padding:4px 13px; border-radius:20px; white-space:nowrap; }
.tm-stat.ok{ background:var(--ok); color:#fff; }
.tm-stat.late{ background:var(--crit); color:#fff; }
.tm-stat.slight{ background:var(--warn-soft); color:var(--warn); border:1px solid var(--warn-line); }
.tm-stat.blind{ background:var(--surface-3); color:var(--muted); border:1px solid var(--line); }
.tm-badge{ font-size:11px; font-weight:600; padding:2px 8px; border-radius:6px;
  background:var(--ok-soft); color:var(--ok); border:1px solid var(--ok-line); white-space:nowrap; }
.tm-badge.q{ background:var(--surface-3); color:var(--muted); border-color:var(--line); }
.tm-note{ font-size:12.5px; color:var(--muted); line-height:1.5; padding:2px 0 0; }
.tm-note b{ color:var(--ink-2); }

/* ---- maintenance list in the sidebar ---- */
.tm-maint{ border-left:3px solid var(--crit); background:var(--crit-soft); border-radius:7px;
  padding:6px 9px; font-size:12.5px; line-height:1.45; color:var(--ink-2); }
.tm-maint b{ font-family:__MONO__; color:var(--ink); }
.tm-maint .why{ color:var(--muted); font-size:11.5px; }
.tm-maint.off{ border-left-color:var(--line); background:var(--surface-2); opacity:.75; }

/* ---- legend ---- */
.tm-legend{ display:flex; gap:16px; flex-wrap:wrap; font-size:12px; color:var(--muted);
  align-items:center; padding-top:8px; }
.tm-legend b{ display:inline-block; width:10px; height:10px; border-radius:3px;
  margin-right:5px; vertical-align:-1px; }
.tm-legend .sw{ display:inline-flex; align-items:center; }

/* ---- native widgets, retuned ---- */
[data-testid="stAlert"]{ border-radius:11px; border-width:2px; }
[data-testid="stAlert"] p{ font-size:14px; }
.stButton>button{ border-radius:9px; font-weight:600; border-width:2px; }
.stButton>button[kind="primary"]{ box-shadow:var(--shadow); }
[data-testid="stTabs"] button[role="tab"]{ font-weight:600; font-size:14px; }
[data-testid="stDataFrame"], [data-testid="stDataEditor"]{ border-radius:10px; }
[data-testid="stDataFrame"] *, [data-testid="stDataEditor"] *{ font-variant-numeric:tabular-nums; }
[data-testid="stExpander"] details{ border-radius:11px; border-color:var(--line); }
[data-testid="stMetricValue"]{ font-family:__MONO__; font-variant-numeric:tabular-nums; }
hr{ border-color:var(--line-2); }
</style>
"""


def inject():
    """Load the stylesheet once per rerun. Safe to call from every page."""
    st.markdown(_CSS.replace("__SANS__", FONT_SANS).replace("__MONO__", FONT_MONO),
                unsafe_allow_html=True)


def esc(text) -> str:
    """HTML-escape any value that came from the DO table or an upload."""
    return (str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


_esc = esc  # internal alias used throughout this module


def hero(title: str, subtitle: str, clock: str, clock_label: str,
         status_kind: str = "ok", status_text: str = "", status_sub: str = ""):
    """Title, the one number that anchors the screen (a time), and a single
    plain-language verdict on the current state."""
    status = ""
    if status_text:
        status = (f'<div class="tm-status {_esc(status_kind)}"><span class="dot"></span>'
                  f'<span class="txt">{_esc(status_text)}'
                  f'<small>{_esc(status_sub)}</small></span></div>')
    st.markdown(
        f'<div class="tm-hero">'
        f'<div class="ttl"><span class="b">{_esc(title)}</span>'
        f'<span class="s">{_esc(subtitle)}</span></div>'
        f'<div class="clock">{_esc(clock)}<small>{_esc(clock_label)}</small></div>'
        f'{status}</div>', unsafe_allow_html=True)


def head(pill: str, title: str, hint: str = "", tone: str = ""):
    """Card header: step badge, the question this section answers, and a quiet hint."""
    hint_html = f'<span class="hint">{_esc(hint)}</span>' if hint else ""
    st.markdown(
        f'<div class="tm-head"><span class="tm-pill {_esc(tone)}">{_esc(pill)}</span>'
        f'<h2>{_esc(title)}</h2>{hint_html}</div>', unsafe_allow_html=True)


def sums(items):
    """Big-number tiles. items: (value, label, sub, tone) with tone in
    '', quiet, accent, ok, warn, crit (+ ' dashed')."""
    cells = "".join(
        f'<div class="tm-sum {_esc(tone)}"><span class="n">{_esc(value)}</span>'
        f'<span class="l">{_esc(label)}<small>{_esc(sub)}</small></span></div>'
        for value, label, sub, tone in items)
    st.markdown(f'<div class="tm-sums">{cells}</div>', unsafe_allow_html=True)


def legend(entries, note: str = ""):
    """entries: (colour, text) — colour may be '' for a text-only marker."""
    parts = []
    for colour, text in entries:
        swatch = f'<b style="background:{_esc(colour)}"></b>' if colour else ""
        parts.append(f'<span class="sw">{swatch}{_esc(text)}</span>')
    if note:
        parts.append(f'<span class="sw">{_esc(note)}</span>')
    st.markdown(f'<div class="tm-legend">{"".join(parts)}</div>', unsafe_allow_html=True)
