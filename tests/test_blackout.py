"""Per-channel maintenance blackouts: a bay that is down takes no trucks, while
the other bays keep working straight through.

Run:  py tests\\test_blackout.py
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scheduler_engine import (  # noqa: E402
    SchedulerConfig,
    build_and_solve,
    hhmm_to_min,
    min_to_hhmm,
)


H = hhmm_to_min


def jobs(product, n, start=1, volume=14, **kw):
    return [{"id": f"{product[:3]}{start + i}", "product": product, "volume": volume,
             "company": "SV", "requested": None, "margin": 0, **kw} for i in range(n)]


def show(label, res, cfg=None):
    print(f"\n=== {label} ===")
    if res.get("error"):
        print("  error:", res["error"])
        for d in res["dropped"]:
            print(f"    DROPPED {d['id']:<8} {d['reason']}")
        return res
    for r in sorted(res["schedule"], key=lambda x: (x["channel"], x["start"])):
        print(f"  {r['channel']} {r['start_hhmm']}-{r['end_hhmm']} {r['id']}")
    for d in res["dropped"]:
        print(f"  DROPPED {d['id']:<8} {d['reason']}")
    print("  self-check:", res["validation"] or "NONE")
    assert not res["validation"], res["validation"]
    return res


# ---------------------------------------------------------------------------
print("### 1. config maths: clipping, outward rounding, no double-count with breaks")
cfg = SchedulerConfig(blackouts=[
    ("C", H("06:00"), H("09:00"), "ซ่อมปั๊ม"),      # starts before the window opens
    ("C", H("12:10"), H("12:50"), "calibrate"),     # straddles the 12:00-12:30 break
    ("B", H("22:00"), H("23:59"), ""),              # runs past the window close
])
print("  C:", [(min_to_hhmm(a), min_to_hhmm(b), w) for a, b, w in cfg.blackouts_for("C")])
print("  B:", [(min_to_hhmm(a), min_to_hhmm(b), w) for a, b, w in cfg.blackouts_for("B")])
assert cfg.blackouts_for("C")[0][0] == cfg.window_start_min, "clipped to the window start"
assert cfg.blackouts_for("B")[0][1] == cfg.window_end_min, "clipped to the window close"
assert cfg.blackouts_for("A") == [], "channel A is untouched"

# 08:00-09:00 is 60 min; 12:10-12:50 is 40 min of which 20 overlaps the break
print(f"  C ปิดจริง {cfg.blackout_minutes('C')} นาที (60 + 40 − 20 ที่ทับช่วงพัก)")
assert cfg.blackout_minutes("C") == 60 + 40 - 20
assert cfg.blackout_minutes("A") == 0

slots = cfg.blackout_slots("C")
assert min(slots) == 0, "08:00 is closed"
# 12:10 rounds DOWN to the 12:10 slot and 12:50 rounds UP - never optimistic
assert (H("12:10") - cfg.window_start_min) // 5 in slots
assert (H("12:45") - cfg.window_start_min) // 5 in slots
print("  ปัดออกด้านนอกทั้งสองฝั่ง ✓")

# ---------------------------------------------------------------------------
print("\n### 2. a closed bay takes no trucks in that window — and the others do not care")
# n-BMA1 is channel C only, MAA3 is channel B only: two products that cannot escape
close_c = SchedulerConfig(blackouts=[("C", H("08:00"), H("13:00"), "ซ่อมแขนโหลด")])
res = show("ปิดช่อง C 08:00-13:00", build_and_solve(
    jobs("n-BMA1", 4) + jobs("MAA3", 3, start=50), config=close_c), close_c)
for r in res["schedule"]:
    if r["channel"] == "C":
        assert r["start"] >= H("13:00"), f"{r['id']} started while C was closed"
maa3 = [r for r in res["schedule"] if r["product"] == "MAA3"]
assert len(maa3) == 3 and min(r["start"] for r in maa3) < H("13:00"), \
    "channel B must keep running through channel C's maintenance"
print("  ช่อง B เดินงานปกติตั้งแต่", min(r["start_hhmm"] for r in maa3), "✓")

# ---------------------------------------------------------------------------
print("\n### 3. utilisation is measured against the time the bay was actually open")
summary = {s["Channel"]: s for s in res["channel_summary"]}
for c in ("B", "C"):
    s = summary[c]
    print(f"  {c}: busy {s['Busy (min)']} · ปิด {s['Closed (min)']} · "
          f"เปิดจริง {s['Available (min)']} · ใช้ {s['Utilisation %']}%")
assert summary["C"]["Closed (min)"] == 300 - 30, "08:00-13:00 minus the 12:00-12:30 break"
assert summary["B"]["Closed (min)"] == 0
assert summary["C"]["Available (min)"] < summary["B"]["Available (min)"]

# ---------------------------------------------------------------------------
print("\n### 4. a hard deadline inside a blackout is refused with a reason")
res2 = show("MMA1 ล็อก 09:00 แต่ช่อง C ปิด", build_and_solve(
    [{"id": "F1", "product": "MMA1", "volume": 14, "company": "SV",
      "requested": H("09:00"), "margin": 0}], config=close_c), close_c)
assert res2["dropped"], "a fixed DO inside a closed window must not be silently moved"
print("  reason:", res2["dropped"][0]["reason"])

# ---------------------------------------------------------------------------
print("\n### 5. closing every bay a product can use drops it with a clear reason")
shut = SchedulerConfig(blackouts=[("C", H("08:00"), H("23:30"), "ซ่อมทั้งวัน")])
res3 = build_and_solve(jobs("n-BMA1", 2), config=shut)
show("ปิดช่อง C ทั้งวัน (n-BMA1 ลงได้ช่องเดียว)", res3, shut)
assert len(res3["dropped"]) == 2 and not res3["schedule"]
assert "ปิดซ่อม" in res3["dropped"][0]["reason"], res3["dropped"][0]["reason"]

# ---------------------------------------------------------------------------
print("\n### 6. a truck already loading when the bay closes is reality, not a violation")
running = SchedulerConfig(blackouts=[("C", H("09:00"), H("12:00"), "ซ่อมฉุกเฉิน")])
res4 = build_and_solve(
    [{"id": "L1", "product": "MMA1", "volume": 14, "company": "SV", "requested": None,
      "margin": 0, "locked_channel": "C", "locked_start": H("08:40")}]
    + jobs("n-BMA1", 2), config=running, now_min=H("09:00"))
show("รถกำลังโหลดอยู่ตอนช่องถูกปิด", res4, running)
locked = next(r for r in res4["schedule"] if r["id"] == "L1")
assert locked["start"] == H("08:40") and locked["is_locked"]
for r in res4["schedule"]:
    if r["channel"] == "C" and not r["is_locked"]:
        assert r["start"] >= H("12:00"), f"{r['id']} was placed inside the blackout"
print("  คิวที่แช่แข็งไว้ไม่ถูกนับเป็นความผิด ส่วนคิวใหม่ถูกดันไปหลัง 12:00 ✓")

# ---------------------------------------------------------------------------
print("\n### 7. a shutdown across several days is projected onto the day being planned")
import datetime as dt  # noqa: E402

import maintenance as mt  # noqa: E402

W0, W1 = H("08:00"), H("23:30")
job = mt.Maintenance("B", dt.date(2026, 9, 12), dt.time(22, 0),
                     dt.date(2026, 9, 14), dt.time(16, 0), "overhaul ปั๊ม")
print("  ", job.describe(), f"· กิน {job.days} วัน")
assert job.days == 3

for day, expect in [
    (dt.date(2026, 9, 11), None),                    # before it starts
    (dt.date(2026, 9, 12), (H("22:00"), W1)),        # first day: from 22:00 to close
    (dt.date(2026, 9, 13), (W0, W1)),                # middle day: closed all day
    (dt.date(2026, 9, 14), (W0, H("16:00"))),        # last day: open again at 16:00
    (dt.date(2026, 9, 15), None),                    # after it ends
]:
    got = mt.window_on(job, day, W0, W1)
    label = "-" if got is None else f"{min_to_hhmm(got[0])}–{min_to_hhmm(got[1])}"
    print(f"    {day:%d/%m} -> {label}")
    assert got == expect, (day, got, expect)

# a middle day really does shut the bay out of the whole plan
mid = SchedulerConfig(blackouts=mt.to_blackouts([job], dt.date(2026, 9, 13), W0, W1))
res5 = build_and_solve(jobs("n-BMA1", 2) + jobs("MAA3", 2, start=50), config=mid)
show("วันกลางของช่วงซ่อม 3 วัน (ช่อง B ปิดทั้งวัน)", res5, mid)
assert not [r for r in res5["schedule"] if r["channel"] == "B"]
assert len([r for r in res5["dropped"] if r["product"] == "MAA3"]) == 2, \
    "MAA3 ลงได้แต่ช่อง B — ต้องหลุดไปวันถัดไป"

# a shutdown that only covers hours the plant is shut anyway changes nothing
night = mt.Maintenance("A", dt.date(2026, 9, 12), dt.time(0, 0),
                       dt.date(2026, 9, 12), dt.time(6, 0), "ล้างไลน์กลางคืน")
assert mt.window_on(night, dt.date(2026, 9, 12), W0, W1) is None
print("  ซ่อมตอนกลางคืนนอกเวลาทำงาน → ไม่กระทบแผน ✓")

# validation catches the two ways a planner can get it wrong
assert mt.Maintenance("D", dt.date(2026, 9, 12), dt.time(9), dt.date(2026, 9, 12),
                      dt.time(10)).errors(), "ช่อง D ไม่มีในระบบนี้"
backwards = mt.Maintenance("A", dt.date(2026, 9, 12), dt.time(10), dt.date(2026, 9, 12),
                           dt.time(9))
print("  error:", backwards.errors()[0])
assert backwards.errors()

# session state stores plain dicts; they must round-trip
rows = mt.to_rows([job, night])
assert [m.describe() for m in mt.from_rows(rows)] == [job.describe(), night.describe()]
assert mt.summarise([job], dt.date(2026, 9, 13), W0, W1) == "B 930 น."
print("  round-trip ผ่าน ·", mt.summarise([job], dt.date(2026, 9, 13), W0, W1))

# ---------------------------------------------------------------------------
print("\n### 8. no blackouts configured = byte-identical behaviour to before")
plain = SchedulerConfig()
a = build_and_solve(jobs("n-BMA1", 4), config=plain)
b = build_and_solve(jobs("n-BMA1", 4), config=SchedulerConfig(blackouts=[]))
assert [(r["id"], r["channel"], r["start"]) for r in a["schedule"]] == \
       [(r["id"], r["channel"], r["start"]) for r in b["schedule"]]
assert all(s["Closed (min)"] == 0 for s in a["channel_summary"])
print("  ไม่มีช่วงปิด → ผลลัพธ์เหมือนเดิมทุกอย่าง ✓")

print("\nALL BLACKOUT TESTS PASSED")
