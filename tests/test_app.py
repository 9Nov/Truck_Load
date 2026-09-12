"""UI-level tests: drive app.py headlessly with Streamlit's AppTest harness.

Run:  py tests\\test_app.py
"""
import sys
from pathlib import Path

from streamlit.testing.v1 import AppTest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
APP = str(ROOT / "app.py")

# The app itself ships no sample data any more - a production planner uploads the
# day's DO file. The suite therefore seeds session state from its own fixtures.
from sample_data import sample_df, sample_gps_df  # noqa: E402


def show(at, label):
    print(f"\n=== {label} ===")
    print("  exception:", [e.value for e in at.exception])
    print("  error    :", [e.value[:90] for e in at.error])
    print("  warning  :", [w.value[:90] for w in at.warning])
    print("  success  :", [s.value[:90] for s in at.success])
    assert not at.exception, at.exception


# 1. first render: an empty board that asks for an upload
at = AppTest.from_file(APP, default_timeout=180)
at.run()
show(at, "initial render (no data)")
assert at.session_state["do_df"].dropna(how="all").empty, "a fresh session must start empty"
assert any("อัปโหลดไฟล์ของวันนั้น" in i.value for i in at.info), \
    "the empty state must point the planner at the upload"
run_btn = [b for b in at.button if "Run Scheduler" in b.label][0]
assert run_btn.disabled, "there is nothing to schedule yet"
assert not [b for b in at.button if "ตัวอย่าง" in b.label], \
    "no sample-data button may ship in production"

# 2. load a day's DOs the way an upload would, then run the scheduler
at.session_state["do_df"] = sample_df()
at.run()
[b for b in at.button if "Run Scheduler" in b.label][0].click().run()
show(at, "after Run Scheduler")
res = at.session_state["result"]
assert res is not None and not res.get("error")
assert not res["validation"], res["validation"]
print(f"  scheduled={len(res['schedule'])} dropped={len(res['dropped'])} "
      f"self-check={res['validation'] or 'NONE'}")

# 3. an unrelated widget change must not wipe the results (Streamlit reruns the whole script)
#    The sidebar no longer carries a Realistic Buffer: every standard-time row owns its own
#    buffer and that always wins, so those two numbers could never reach the solver. They
#    were removed rather than left as controls that do nothing - guard that they stay gone,
#    then drift a setting that genuinely does change the plan.
labels = [n.label for n in at.sidebar.number_input]
print("\n  sidebar number inputs:", labels)
assert not any("Realistic Buffer" in label for label in labels), \
    "the dead Realistic Buffer inputs are back - the buffer belongs on the Standard Time page"
weigh = [n for n in at.sidebar.number_input if "ตาชั่ง" in n.label][0]
weigh.set_value(5).run()
show(at, "after changing the shared camera/weighbridge time")
assert at.session_state["result"] is not None, "results were lost on an unrelated widget change"
assert any("Settings" in w.value for w in at.warning), "should warn that settings drifted since the run"

# 4. clearing the table resets the result
[b for b in at.button if "เคลียร์" in b.label][0].click().run()
print("\n=== after clearing the table ===")
print("  info:", [i.value[:80] for i in at.info])
assert at.session_state["result"] is None
assert not at.exception

# 5. reload the DO list and run again with the changed settings
at.session_state["do_df"] = sample_df()
at.run()
[b for b in at.button if "Run Scheduler" in b.label][0].click().run()
show(at, "second run with the weigh resource at 5 min")
res2 = at.session_state["result"]
assert not res2["validation"], res2["validation"]
assert res2["config"].weigh_resource_min == 5, res2["config"]
print(f"  scheduled={len(res2['schedule'])} dropped={len(res2['dropped'])}")
print("  config used:", res2["config"])

# ---------------------------------------------------------------------------
# 6. switch to the mid-day re-planner; the plan just made is its baseline
import datetime as dt  # noqa: E402

at.sidebar.radio[0].set_value("🔄 ปรับแผนระหว่างวัน").run()
show(at, "re-plan page")
assert at.session_state["baseline"], "the plan run should have been stored as the baseline"
assert not any("ยังไม่มีแผนหลัก" in i.value for i in at.info), "baseline was not picked up"

# 7. pin 'now' to 09:00 so the test is deterministic, then set statuses from it
[w for w in at.time_input if w.label == "ตอนนี้"][0].set_value(dt.time(9, 0)).run()
[b for b in at.button if "ตั้งสถานะตามเวลา" in b.label][0].click().run()
statuses = at.session_state["status_df"]["สถานะ"].value_counts().to_dict()
print("  statuses at 09:00:", statuses)
assert statuses.get("เสร็จแล้ว", 0) + statuses.get("กำลังโหลด", 0) > 0, \
    "at 09:00 some trucks must already be done or loading"

# 8. feed in GPS fixes the way a real feed would, then re-optimise
assert not [b for b in at.button if "สุ่มตัวอย่าง GPS" in b.label], \
    "no GPS mock button may ship in production"
from gps_eta import TravelConfig  # noqa: E402

plan_by_id = {r["id"]: r for r in at.session_state["baseline_schedule"]}
waiting = [r["DO No."] for _, r in at.session_state["status_df"].iterrows()
           if r["สถานะ"] == "รอเข้า"]
at.session_state["gps_df"] = sample_gps_df(
    [{"id": i, "planned_start": plan_by_id[i]["start"]} for i in waiting if i in plan_by_id],
    9 * 60, TravelConfig())
at.run()
gps = at.session_state["gps_df"]
print("  GPS rows:", len(gps))
assert len(gps) > 0

[b for b in at.button if "จัดคิวใหม่" in b.label][0].click().run()
show(at, "after Re-optimize")
rp = at.session_state["replan_result"]
assert rp is not None and not rp.get("error"), rp.get("error")
assert not rp["validation"], rp["validation"]
print(f"  scheduled={len(rp['schedule'])} frozen={rp['n_locked']} "
      f"dropped={len(rp['dropped'])} changes={len(rp['baseline_diff'])}")
assert rp["n_locked"] > 0, "already-running trucks must be frozen"

# the day-view calendar must render, and must carry the per-truck state classes
cal = [m.value for m in at.markdown if "tmcal-scroll" in m.value]
assert cal, "the calendar view did not render"
html = cal[0]
assert html.count('class="colh"') == 3, "one column header per channel A/B/C"
assert html.count('class="ev ') >= len(rp["schedule"]), "every scheduled truck needs a card"
states = {c for c in ("s-load", "s-done", "s-here", "s-late", "s-eta", "s-blind") if c in html}
print("  calendar states drawn:", sorted(states))
assert "s-load" in states or "s-done" in states, "frozen trucks must be styled as such"
assert "nowl" in html, "the now line must be drawn when re-planning"
assert all(r["start"] >= 9 * 60 for r in rp["schedule"] if not r["is_locked"]), \
    "nothing may be re-scheduled into the past"

# 9. Manual mode: pick a replacement for a late truck, then re-solve with it pinned
swap_radio = [r for r in at.radio if r.key == "swap_mode"][0]
swap_radio.set_value("🙋 Manual — เลือกคันแทนเอง").run()
show(at, "manual swap lane")
picks = [b for b in at.button if b.label == "เลือกคันนี้แทน"]
print("  candidate buttons offered:", len(picks))
assert picks, "no replacement candidates were offered for any late truck"

picks[0].click().run()
decisions = at.session_state["swap_decisions"]
print("  decision:", decisions)
assert len(decisions) == 1
d = decisions[0]
assert d["replacement_id"] != d["target_id"]

[b for b in at.button if "จัดคิวใหม่" in b.label][0].click().run()
show(at, "after re-optimising with the manual pick")
rp2 = at.session_state["replan_result"]
assert rp2 is not None and not rp2.get("error"), rp2.get("error")
assert not rp2["validation"], rp2["validation"]
placed = {r["id"]: r for r in rp2["schedule"]}
rep = placed.get(d["replacement_id"])
print(f"  {d['replacement_id']} -> {rep['channel']} {rep['start_hhmm']} "
      f"(ขอไว้ {d['channel']} {d['start'] // 60:02d}:{d['start'] % 60:02d}) pinned={rep['is_pinned']}")
assert rep is not None, "the chosen replacement must be in the new plan"
assert rep["channel"] == d["channel"] and rep["start"] == d["start"],     "a manual pick must land exactly where the planner put it"
assert rep["is_pinned"], "it must be marked as a planner decision"

# 10. switching back to the planning page must not lose either result
[r for r in at.sidebar.radio][0].set_value("📋 วางแผนต้นวัน").run()
show(at, "back on the planning page")
assert at.session_state["result"] is not None
assert at.session_state["replan_result"] is not None

# 11. a maintenance window closes one bay and only that bay.
# It is entered as a dated period; the app projects it onto the day being planned.
today = dt.date.today()
at.session_state["maintenance"] = [{
    "channel": "B", "start_date": today - dt.timedelta(days=1), "start_time": dt.time(22, 0),
    "end_date": today, "end_time": dt.time(14, 0), "reason": "ซ่อมแขนโหลด (ข้ามคืน)"}]
at.run()
[b for b in at.button if "Run Scheduler" in b.label][0].click().run()
show(at, "after closing channel B 10:00-14:00")
res3 = at.session_state["result"]
assert res3 is not None and not res3.get("error"), res3.get("error")
assert not res3["validation"], res3["validation"]
assert res3["config"].blackouts, "the maintenance window never reached the solver"
closed = [r for r in res3["schedule"]
          if r["channel"] == "B" and r["start"] < 14 * 60 and r["end"] > 10 * 60]
print(f"  scheduled={len(res3['schedule'])} dropped={len(res3['dropped'])} "
      f"· jobs inside B's closed window: {len(closed)}")
assert not closed, f"channel B took trucks while closed: {[r['id'] for r in closed]}"
others = [r for r in res3["schedule"]
          if r["channel"] != "B" and r["start"] < 14 * 60 and r["end"] > 10 * 60]
assert others, "channels A and C must keep working through B's maintenance"
print(f"  A/C ยังเดินงานในช่วงนั้น {len(others)} คิว")

cal = [m.value for m in at.markdown if "tmcal-scroll" in m.value]
assert cal and 'class="down"' in cal[0], "the closed band must be drawn on the calendar"
summary = {s["Channel"]: s for s in res3["channel_summary"]}
print(f"  B: ปิด {summary['B']['Closed (min)']} นาที · "
      f"เปิดจริง {summary['B']['Available (min)']} · ใช้ {summary['B']['Utilisation %']}%")
assert summary["B"]["Closed (min)"] > 0 and summary["A"]["Closed (min)"] == 0

# 12. the standard-time page: defaults reach the solver, and an edit changes the plan
import pandas as pd  # noqa: E402
import std_times as st_  # noqa: E402

at.sidebar.radio[0].set_value("⚙️ เวลามาตรฐาน").run()
show(at, "standard-time page")
rows = at.session_state["std_lorry_rows"]
by_key = {(r[st_.GROUP].split("(")[0].strip(), r[st_.BRACKET]): r for r in rows}
print("  MMA2 14MT:", by_key[("MMA2 CH.A-B", "14MT")][st_.ORIGINAL], "->",
      by_key[("MMA2 CH.A-B", "14MT")][st_.TOTAL],
      "|", by_key[("MMA2 CH.A-B", "14MT")][st_.SOURCE])
assert by_key[("MMA2 CH.A-B", "14MT")][st_.TOTAL] == 75, "default must be the measured P75"
assert by_key[("MMA2 CH.A-B", "14MT")][st_.ORIGINAL] == 50, "the ISO standard must still show"
fallback = [k for k, r in by_key.items() if r[st_.SOURCE] != "Actual (P75 จริง)"]
print("  rows still on the old standard:", fallback)
assert set(fallback) == {("MAA1-2", "22-24MT"), ("MAA3", "22-24MT")}
assert all(r[st_.BUFFER] == (10 if r[st_.SOURCE] != "Actual (P75 จริง)" else 0) for r in rows)

# edit MMA1 14MT 85 -> 90 and confirm the plan gets 5 minutes longer for that DO
at.sidebar.radio[0].set_value("📋 วางแผนต้นวัน").run()
at.session_state["do_df"] = pd.DataFrame(
    [{"DO No.": "S1", "Product": "MMA1", "Volume (ton)": 14.0, "Transport Co.": "SV",
      "Requested Time": "", "Margin (min)": 0}], columns=list(at.session_state["do_df"].columns))
at.session_state["maintenance"] = []
at.run()
[b for b in at.button if "Run Scheduler" in b.label][0].click().run()
base_minutes = at.session_state["result"]["schedule"][0]["duration_min"]

edited = [dict(r) for r in at.session_state["std_lorry_rows"]]
for r in edited:
    if r[st_.GROUP].startswith("MMA1") and r[st_.BRACKET] == "14MT":
        r[st_.TOTAL] = 90
at.session_state["std_lorry_rows"] = edited
at.run()
# Editing a standard time is the single biggest thing that can change a plan, so the
# stale-plan warning has to fire for it as well. It used to track only the sidebar, which
# meant a table edit left the old plan on screen with nothing to say it was out of date.
assert any("Settings" in w.value for w in at.warning), \
    "editing a standard time must mark the plan already on screen as stale"
[b for b in at.button if "Run Scheduler" in b.label][0].click().run()
show(at, "after editing MMA1 14MT to 90")
new_minutes = at.session_state["result"]["schedule"][0]["duration_min"]
print(f"  MMA1 14MT DO: {base_minutes} -> {new_minutes} นาที")
assert (base_minutes, new_minutes) == (85, 90), (base_minutes, new_minutes)
assert not at.session_state["result"]["validation"]

print("\nALL APP TESTS PASSED")
