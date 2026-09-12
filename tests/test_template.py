"""Template tests: does DO_Template.xlsx survive the app's own upload path?

Run:  py tests\\test_template.py
"""
import sys
import tempfile
from pathlib import Path

import pandas as pd
from openpyxl import load_workbook

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from do_template import build_template_bytes  # noqa: E402
from scheduler_engine import (  # noqa: E402
    PRODUCTS,
    TRANSPORT_COMPANIES,
    SchedulerConfig,
    build_and_solve,
    hhmm_to_min,
)

COLS = ["DO No.", "Product", "Volume (ton)", "Transport Co.", "Requested Time", "Margin (min)"]

# build a fresh copy so the test covers the generator, not a stale file
path = Path(tempfile.gettempdir()) / "DO_Template_test.xlsx"
path.write_bytes(build_template_bytes())
print("built:", path)

# 1. the sheet the app actually reads (pandas takes the first sheet by default)
first = pd.read_excel(path)
print("\nfirst sheet columns:", list(first.columns))
print("first sheet rows   :", len(first))
assert list(first.columns) == COLS, "headers must match what app.py expects exactly"
assert len(first) == 0, "the input sheet must start empty"

# 2. the worked example
ex = pd.read_excel(path, sheet_name="Example")
assert list(ex.columns)[:6] == COLS, list(ex.columns)
ex = ex[COLS].dropna(how="all")  # a free-text note sits outside the data columns
print("\nexample rows:")
print(ex.to_string(index=False))
assert set(ex["Product"]) <= set(PRODUCTS), set(ex["Product"])
assert set(ex["Transport Co."]) <= set(TRANSPORT_COMPANIES), set(ex["Transport Co."])

# 3. requested times must come back as parseable HH:MM, not an Excel date
jobs = []
for _, row in ex.iterrows():
    raw = row["Requested Time"]
    requested = None
    if raw is not None and str(raw).strip() not in ("", "nan", "NaT", "None"):
        requested = hhmm_to_min(raw)
        assert requested is not None, f"unparseable requested time: {raw!r} ({type(raw)})"
        assert requested % 5 == 0, f"{raw!r} is not on the 5-minute grid"
    jobs.append({"id": row["DO No."], "product": row["Product"],
                 "volume": float(row["Volume (ton)"]), "company": row["Transport Co."],
                 "requested": requested, "margin": int(row["Margin (min)"] or 0)})

# 4. and they really schedule
res = build_and_solve(jobs, config=SchedulerConfig())
assert not res.get("error"), res.get("error")
assert not res["validation"], res["validation"]
print(f"\nscheduled={len(res['schedule'])} dropped={len(res['dropped'])} "
      f"self-check={res['validation'] or 'NONE'}")
for r in res["schedule"]:
    print(f"  {r['channel']} {r['start_hhmm']}-{r['end_hhmm']} {r['id']} {r['product']} "
          f"{'FIXED ' + r['requested_hhmm'] if r['is_fixed'] else ''}")

# 5. workbook structure
wb = load_workbook(path)
print("\nsheets:", [(s.title, s.sheet_state) for s in wb.worksheets])
assert wb.worksheets[0].title == "DO List", "the app only reads the first sheet"
assert wb["Lists"].sheet_state == "hidden"
dv = {str(d.sqref): d.type for d in wb["DO List"].data_validations.dataValidation}
print("validations:", dv)
assert len(dv) == 4, dv

# 6. the copy committed in the project is in sync with the generator
shipped = ROOT / "DO_Template.xlsx"
if shipped.exists():
    shipped_cols = list(pd.read_excel(shipped).columns)
    assert shipped_cols == COLS, f"{shipped.name} is stale - rerun: py do_template.py"
    print(f"\n{shipped.name}: in sync")
else:
    print(f"\n{shipped.name}: not generated yet - run: py do_template.py")

# ---------------------------------------------------------------------------
# 7. the upload path end to end. Since the app ships no sample data, this is the
#    ONLY way a DO reaches the solver, so it gets its own walk-through.
print("\n### 7. upload -> normalise -> jobs -> schedule")
import datetime as dt  # noqa: E402
import io as _io  # noqa: E402

from scheduler_engine import SchedulerConfig, build_and_solve  # noqa: E402
from ui_common import normalise_upload, rows_to_jobs  # noqa: E402

# a file as a planner's own export would look: loose header spellings, a real
# datetime in the time column, a blank row, numbers stored as text
messy = pd.DataFrame([
    {"DO": "DO900", "Grade": "MMA2", "Volume": "22", "Carrier": " SV ",
     "Requested": dt.time(9, 30), "Margin": 5},
    {"DO": "DO901", "Grade": "i-BMA", "Volume": 14.0, "Carrier": "Yusen",
     "Requested": "", "Margin": None},
    {"DO": None, "Grade": None, "Volume": None, "Carrier": None,
     "Requested": None, "Margin": None},          # blank row must be ignored
    {"DO": "DO902", "Grade": "MAA1", "Volume": 20, "Carrier": "ศรีไทย",
     "Requested": "1400", "Margin": 0},           # HHMM with no colon
])
buf = _io.BytesIO()
messy.to_excel(buf, index=False)
buf.seek(0)

normalised = normalise_upload(pd.read_excel(buf))
print(normalised.to_string(index=False))
assert list(normalised.columns) == COLS
assert normalised["Requested Time"].tolist() == ["09:30", "", "", "14:00"]
assert normalised["Transport Co."].tolist() == ["SV", "Yusen", "", "ศรีไทย"]

jobs, errors, warnings = rows_to_jobs(normalised)
for w in warnings:
    print("  warning:", w)
assert not errors, errors
assert [j["id"] for j in jobs] == ["DO900", "DO901", "DO902"], "the blank row must be dropped"
assert jobs[0]["requested"] == 9 * 60 + 30 and jobs[0]["margin"] == 5
assert jobs[2]["requested"] == 14 * 60

res = build_and_solve(jobs, config=SchedulerConfig())
assert not res.get("error"), res.get("error")
assert not res["validation"], res["validation"]
for r in res["schedule"]:
    print(f"  {r['channel']} {r['start_hhmm']}-{r['end_hhmm']} {r['id']} {r['product']} "
          f"{r['company']} · {r['std_source']}")
assert len(res["schedule"]) == 3
yusen_row = next(r for r in res["schedule"] if r["id"] == "DO901")
assert yusen_row["std_source"] == "Yusen ISO Tank", "carrier must still pick the ISO Tank table"
print("  ไฟล์ที่หัวคอลัมน์ไม่ตรงเป๊ะก็อ่านได้ และเข้าถึง solver ได้จริง ✓")

# CSV goes down the same road
csv_buf = _io.StringIO()
messy.to_csv(csv_buf, index=False)
csv_jobs, csv_errors, _ = rows_to_jobs(normalise_upload(pd.read_csv(_io.StringIO(csv_buf.getvalue()))))
assert not csv_errors, csv_errors
assert [j["id"] for j in csv_jobs] == ["DO900", "DO901", "DO902"]
print("  CSV ให้ผลเหมือน Excel ✓")

print("\nTEMPLATE TESTS PASSED")
