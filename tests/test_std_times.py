"""Standard-time tables: the shipped defaults, the editing rules, and the fact that
an edit actually changes how long a truck is costed.

Run:  py tests\\test_std_times.py
"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import std_times as st_  # noqa: E402
from scheduler_engine import (  # noqa: E402
    LOAD_LORRY_ORIGINAL_STD,
    LORRY_STD_DEFAULT,
    LORRY_STD_FROM_ORIGINAL,
    SRC_ACTUAL,
    SRC_STANDARD,
    WEIGH_OVERHEAD,
    YUSEN_STD_DEFAULT,
    YUSEN_STD_FROM_ORIGINAL,
    YUSEN_STD_TOTAL_ORIGINAL_STD,
    SchedulerConfig,
    build_and_solve,
    job_total_slots,
)

# ---------------------------------------------------------------------------
print("### 1. the shipped defaults are the measured P75 figures, not the ISO standard")
EXPECTED_LORRY = {
    ("MMA1", "14MT"): 85, ("MMA1", "20-22MT"): 100, ("MMA1", "22-24MT"): 100,
    ("MMA1", "24-25MT"): 95, ("MMA1", "29MT"): 105,
    ("MMA2 CH.A-B", "14MT"): 75, ("MMA2 CH.A-B", "20-22MT"): 80,
    ("MMA2 CH.A-B", "22-24MT"): 85, ("MMA2 CH.A-B", "24-25MT"): 90,
    ("MMA2 CH.A-B", "29MT"): 85,
    ("MAA1-2", "14MT"): 85, ("MAA1-2", "20-22MT"): 95, ("MAA1-2", "22-24MT"): 65,
    ("MAA3", "14MT"): 85, ("MAA3", "20-22MT"): 95, ("MAA3", "22-24MT"): 65,
    ("IBMA", "14MT"): 90, ("NBMA", "14MT"): 80,
}
EXPECTED_YUSEN = {"i-BMA": 105, "MAA1": 110, "MAA2": 110, "MAA3": 110,
                  "MMA1": 125, "MMA2": 100, "n-BMA1": 105, "n-BMA2": 105}

flat = {(g, b): e for g, br in LORRY_STD_DEFAULT.items() for b, e in br.items()}
assert set(flat) == set(EXPECTED_LORRY), set(flat) ^ set(EXPECTED_LORRY)
for key, total in EXPECTED_LORRY.items():
    assert flat[key].total_min == total, (key, flat[key].total_min, total)
for product, total in EXPECTED_YUSEN.items():
    assert YUSEN_STD_DEFAULT[product].total_min == total, product
print(f"  LORRY {len(EXPECTED_LORRY)} แถว · ISO Tank {len(EXPECTED_YUSEN)} แถว ตรงทุกค่า")

# every figure must sit on the 5-minute grid the model is built on
for key, e in list(flat.items()) + [((p, ""), v) for p, v in YUSEN_STD_DEFAULT.items()]:
    assert e.total_min % 5 == 0, key
print("  ทุกค่าหารด้วย 5 ลงตัว ✓")

# ---------------------------------------------------------------------------
print("\n### 2. only the two cells with no real trips fall back to the ISO standard")
fallback = [(g, b) for (g, b), e in flat.items() if e.source == SRC_STANDARD]
fallback += [(p, "") for p, e in YUSEN_STD_DEFAULT.items() if e.source == SRC_STANDARD]
print("  fallback rows:", fallback)
assert set(fallback) == {("MAA1-2", "22-24MT"), ("MAA3", "22-24MT")}, fallback
for g, b in fallback:
    assert flat[(g, b)].total_min == LOAD_LORRY_ORIGINAL_STD[g][b] + WEIGH_OVERHEAD
    assert flat[(g, b)].n == 0

# ---------------------------------------------------------------------------
print("\n### 3. buffer defaults: 0 where the figure was measured, 10 where it was not")
for key, e in flat.items():
    expect = 0 if e.source == SRC_ACTUAL else 10
    assert e.buffer_min == expect, (key, e.buffer_min, expect)
for product, e in YUSEN_STD_DEFAULT.items():
    assert e.buffer_min == 0, product
print("  Actual → buffer 0 · Standard → buffer 10 ✓ "
      f"(มี {len(fallback)} แถวที่ได้ buffer 10)")

# ---------------------------------------------------------------------------
print("\n### 4. the rollback table really is the untouched ISO standard")
for group, brackets in LOAD_LORRY_ORIGINAL_STD.items():
    for bracket, load in brackets.items():
        e = LORRY_STD_FROM_ORIGINAL[group][bracket]
        assert e.total_min == load + WEIGH_OVERHEAD and e.load_min == load
for product, total in YUSEN_STD_TOTAL_ORIGINAL_STD.items():
    assert YUSEN_STD_FROM_ORIGINAL[product].total_min == total
print("  Standard เดิม: MMA2 14MT total 50 (load 20) · Yusen MMA1 110 ✓")

# ---------------------------------------------------------------------------
print("\n### 5. an edit changes how long that truck is costed")
CFG = SchedulerConfig()
before = job_total_slots("MMA1", 14, 10, 0, "SV",
                         lorry_std=LORRY_STD_DEFAULT)[0] * 5
rows = st_.lorry_rows()
for row in rows:
    if row[st_.GROUP].startswith("MMA1") and row[st_.BRACKET] == "14MT":
        row[st_.TOTAL] = 90
edited, notes = st_.rows_to_lorry(rows)
after = job_total_slots("MMA1", 14, 10, 0, "SV", lorry_std=edited)[0] * 5
print(f"  MMA1 14MT: {before} → {after} นาที (Load {before - 30} → {after - 30})")
assert (before, after) == (85, 90) and not notes
assert edited["MMA1"]["14MT"].load_min == 60

# and it reaches the solver through the config
res = build_and_solve(
    [{"id": "E1", "product": "MMA1", "volume": 14, "company": "SV",
      "requested": None, "margin": 0}],
    config=SchedulerConfig(lorry_std=edited))
assert res["schedule"][0]["duration_min"] == 90, res["schedule"][0]["duration_min"]
print("  ค่าที่แก้ถูกใช้จริงตอนจัดคิว ✓")

# ---------------------------------------------------------------------------
print("\n### 6. off-grid and impossible figures are corrected, loudly")
rows = st_.lorry_rows()
for row in rows:
    if row[st_.GROUP].startswith("MMA1") and row[st_.BRACKET] == "14MT":
        row[st_.TOTAL] = 77                      # not a multiple of 5
    if row[st_.GROUP].startswith("IBMA"):
        row[st_.TOTAL] = 20                      # less than weigh-in + weigh-out
    if row[st_.GROUP].startswith("NBMA"):
        row[st_.BUFFER] = 7                      # off-grid buffer
fixed, notes = st_.rows_to_lorry(rows)
for n in notes:
    print("  ", n)
assert fixed["MMA1"]["14MT"].total_min == 75, "77 ต้องถูกปัดเป็น 75"
assert fixed["IBMA"]["14MT"].total_min == st_.MIN_TOTAL == 35
assert fixed["NBMA"]["14MT"].buffer_min == 5
assert len(notes) == 3, notes   # 1 rounding + 1 floor + 1 buffer rounding

bad = st_.rows_to_lorry([{st_.GROUP: "MMA1", st_.BRACKET: "14MT",
                          st_.TOTAL: "ไม่ใช่ตัวเลข", st_.BUFFER: 0}])[1]
print("  ", bad[0])
assert "ไม่ใช่ตัวเลข" in bad[0]

# ---------------------------------------------------------------------------
print("\n### 7. reset goes back to the measured defaults, not to the ISO standard")
assert st_.rows_to_lorry(st_.lorry_rows())[0]["MMA2 CH.A-B"]["14MT"].total_min == 75
assert st_.rows_to_lorry(st_.lorry_rows(LORRY_STD_FROM_ORIGINAL))[0][
    "MMA2 CH.A-B"]["14MT"].total_min == 50
back, _ = st_.rows_to_yusen(st_.yusen_rows())
assert back["MMA1"].total_min == 125 and back["MMA1"].buffer_min == 0
print("  reset → 75 (P75) · rollback → 50 (standard เดิม) ✓")

changed = st_.changed_rows(edited, LORRY_STD_DEFAULT)
print("  changed_rows เห็นการแก้:", [(g, b) for g, b, _, _ in changed])
assert [(g, b) for g, b, _, _ in changed] == [("MMA1", "14MT")]
assert st_.changed_rows(st_.rows_to_lorry(st_.lorry_rows())[0], LORRY_STD_DEFAULT) == []

# ---------------------------------------------------------------------------
print("\n### 8. export carries the numbers and where they came from")
payload = json.loads(st_.to_json(LORRY_STD_DEFAULT, YUSEN_STD_DEFAULT))
assert payload["lorry"]["MMA2 CH.A-B"]["14MT"]["total_min"] == 75
assert payload["lorry"]["MMA2 CH.A-B"]["14MT"]["load_min"] == 45
assert payload["yusen"]["MMA1"]["n"] == 44
assert payload["lorry"]["MAA1-2"]["22-24MT"]["source"] == SRC_STANDARD
csv = st_.to_csv(LORRY_STD_DEFAULT, YUSEN_STD_DEFAULT)
assert csv.count("\n") == len(EXPECTED_LORRY) + len(EXPECTED_YUSEN) + 1
print(f"  JSON + CSV ครบ {len(EXPECTED_LORRY) + len(EXPECTED_YUSEN)} แถว ✓")

# ---------------------------------------------------------------------------
print("\n### 9. the defaults are slower than the standard — the day fits fewer trucks")
jobs = [{"id": f"D{i}", "product": "MMA2", "volume": 14, "company": "SV",
         "requested": None, "margin": 0} for i in range(30)]
std_plan = build_and_solve(jobs, config=SchedulerConfig(
    lorry_std=LORRY_STD_FROM_ORIGINAL, yusen_std=YUSEN_STD_FROM_ORIGINAL))
p75_plan = build_and_solve(jobs, config=SchedulerConfig())
assert not std_plan["validation"] and not p75_plan["validation"]
print(f"  standard เดิม (60 น./เที่ยว): จัดได้ {len(std_plan['schedule'])} คัน")
print(f"  ค่าเริ่มต้น P75 (75 น./เที่ยว): จัดได้ {len(p75_plan['schedule'])} คัน")
assert len(p75_plan["schedule"]) <= len(std_plan["schedule"]), \
    "a slower, more realistic cycle cannot fit more trucks"

print("\nALL STANDARD-TIME TESTS PASSED")
