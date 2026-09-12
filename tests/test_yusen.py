"""Yusen (ISO Tank) standard-time tests, including a strict no-regression check
that every non-Yusen DO still lands exactly where it did before the change.

Run:  py tests\\test_yusen.py
"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scheduler_engine import (  # noqa: E402
    LORRY_STD_FROM_ORIGINAL,
    SLOT,
    STD_SOURCE_LORRY,
    STD_SOURCE_YUSEN,
    WEIGH_IN_LORRY,
    WEIGH_OUT_LORRY,
    YUSEN_STD_FROM_ORIGINAL,
    YUSEN_STD_TOTAL,
    SchedulerConfig,
    build_and_solve,
    is_yusen,
    job_total_slots,
    min_to_hhmm,
)

# This suite is about WHICH table a carrier is costed from, not about which numbers
# ship as defaults. It therefore pins both tables to the untouched ISO standard - the
# same figures the regression fixture was captured with. The measured P75 defaults that
# the app now starts from are covered by tests/test_std_times.py.
ORIGINAL = dict(lorry_std=LORRY_STD_FROM_ORIGINAL, yusen_std=YUSEN_STD_FROM_ORIGINAL)
CFG = SchedulerConfig(**ORIGINAL)
FIXTURE = ROOT / "tests" / "fixtures" / "lorry_baseline.json"


def cycle(product, volume, company, margin=0, cfg=CFG):
    """Total minutes a DO occupies a bay, straight out of the engine."""
    total_slots, load_slots, bracket, source = job_total_slots(
        product, volume, cfg.realistic_buffer_min, margin,
        transport_co=company, yusen_buffer_min=cfg.yusen_buffer_min,
        lorry_std=cfg.lorry_std, yusen_std=cfg.yusen_std)
    return (None if total_slots is None else total_slots * SLOT,
            None if load_slots is None else load_slots * SLOT, bracket, source)


# ---------------------------------------------------------------------------
print("### 1. Yusen i-BMA -> exactly 100 min (15 weigh-in + 70 load + 15 weigh-out)")
total, load, bracket, source = cycle("i-BMA", 14, "Yusen")
print(f"  total={total} load={load} bracket={bracket} source={source}")
assert total == 100, total
assert load == 100 - WEIGH_IN_LORRY - WEIGH_OUT_LORRY == 70, load
assert source == STD_SOURCE_YUSEN

# ---------------------------------------------------------------------------
print("\n### 2. Yusen is tonnage-independent: MAA2 at 14 t and at 24 t are both 110")
small = cycle("MAA2", 14, "Yusen")
big = cycle("MAA2", 24, "Yusen")
print(f"  14 t -> {small[0]} min (load {small[1]}) | 24 t -> {big[0]} min (load {big[1]})")
assert small[0] == big[0] == 110, (small, big)
assert small[1] == big[1] == 80, (small, big)

print("\n  every product, any tonnage:")
for product, std in YUSEN_STD_TOTAL.items():
    got = {cycle(product, v, "Yusen")[0] for v in (12, 14, 20, 22, 24, 25, 29)}
    print(f"    {product:<7} standard {std} -> {sorted(got)}")
    assert got == {std}, (product, got)

# ---------------------------------------------------------------------------
print("\n### 3. LORRY carriers are untouched: ศรีไทย MAA2 14 t is still 60 min")
total, load, bracket, source = cycle("MAA2", 14, "ศรีไทย")
print(f"  total={total} load={load} (50 std + {CFG.realistic_buffer_min} buffer) "
      f"bracket={bracket} source={source}")
assert total == 60, total          # 15 + (20 std + 10 buffer) + 15
assert load == 30 and bracket == "14MT" and source == STD_SOURCE_LORRY
for company in ("SV", "VIV"):
    assert cycle("MAA2", 14, company)[0] == 60, company
print("  SV / VIV identical ✓")

# ---------------------------------------------------------------------------
print("\n### 4. Margin is added on both paths; the Realistic Buffer only on the LORRY one")
print(f"  Yusen i-BMA + margin 10  -> {cycle('i-BMA', 14, 'Yusen', margin=10)[0]} min")
assert cycle("i-BMA", 14, "Yusen", margin=10)[0] == 110
assert cycle("MAA1", 20, "Yusen", margin=15)[0] == 125
print(f"  ศรีไทย MAA2 14 t + margin 10 -> {cycle('MAA2', 14, 'ศรีไทย', margin=10)[0]} min")
assert cycle("MAA2", 14, "ศรีไทย", margin=10)[0] == 70

# A table entry may carry its own buffer (the measured P75 rows do, at 0) or leave it
# to the config (the ISO-standard rows do). Both routes have to work.
import dataclasses  # noqa: E402

no_buffer = SchedulerConfig(**ORIGINAL, realistic_buffer_min=0)
assert cycle("MAA2", 14, "ศรีไทย", cfg=no_buffer)[0] == 50, \
    "an entry without its own buffer must follow the config"
yusen_buffered = SchedulerConfig(**{**ORIGINAL, "yusen_std": {
    p: dataclasses.replace(e, buffer_min=10) for p, e in YUSEN_STD_FROM_ORIGINAL.items()}})
assert cycle("i-BMA", 14, "Yusen", cfg=yusen_buffered)[0] == 110, \
    "an entry with its own buffer must win"
print("  buffer: entry-level ชนะ config-level · standard เดิมปล่อยให้ config คุม ✓")
assert LORRY_STD_FROM_ORIGINAL["MAA1-2"]["14MT"].buffer_min is None
assert CFG.realistic_buffer_min == 10 and CFG.yusen_buffer_min == 0

# a margin off the 5-minute grid is rounded onto it, both paths
for company, expected in (("Yusen", 105), ("ศรีไทย", 65)):
    got = cycle("i-BMA", 14, company, margin=7)[0]
    print(f"  {company} + margin 7 -> {got} min (rounded onto the {SLOT}-min grid)")
    assert got == expected and got % SLOT == 0, (company, got)

# carrier matching tolerates spreadsheet sloppiness
assert is_yusen("Yusen") and is_yusen(" yusen ") and is_yusen("YUSEN")
assert not is_yusen("SV") and not is_yusen("") and not is_yusen(None)
print("  carrier match is case/space tolerant ✓")

# ---------------------------------------------------------------------------
print("\n### 5. REGRESSION: the old LORRY-only sample must solve identically")
baseline = json.loads(FIXTURE.read_text(encoding="utf-8"))
for name, case in baseline.items():
    cfg = SchedulerConfig(**case["cfg"], **ORIGINAL)
    cfg.breaks_min = [tuple(b) for b in case["cfg"]["breaks_min"]]
    res = build_and_solve(case["jobs"], config=cfg)
    assert not res.get("error"), (name, res["error"])
    assert not res["validation"], (name, res["validation"])
    now = sorted([{k: r[k] for k in ("id", "channel", "start", "end", "duration_min", "bracket")}
                  for r in res["schedule"]], key=lambda x: x["id"])
    diff = [(a, b) for a, b in zip(case["schedule"], now) if a != b]
    print(f"  {name}: {len(now)} scheduled / {len(res['dropped'])} dropped · "
          f"differences vs before the Yusen change: {len(diff)}")
    for a, b in diff[:5]:
        print("    was", a, "\n    now", b)
    assert not diff, f"{name}: non-Yusen DOs changed!"
    assert sorted(d["id"] for d in res["dropped"]) == case["dropped"], name
    assert all(r["std_source"] == STD_SOURCE_LORRY for r in res["schedule"])
print("  ✓ byte-identical channel, start, end and duration for every LORRY DO")

# ---------------------------------------------------------------------------
print("\n### 6. A mixed day still passes the shared-resource self-check")
jobs = []
for i in range(9):
    product = ["MAA1", "MMA2", "i-BMA", "MAA3", "MMA1", "n-BMA1", "MAA2", "MMA2", "n-BMA2"][i]
    jobs.append({"id": f"Y{i+1}", "product": product, "volume": [14, 22, 14, 21, 25, 14, 24, 29, 12][i],
                 "company": "Yusen" if i % 2 == 0 else "SV",
                 "requested": None, "margin": 0})
res = build_and_solve(jobs, config=CFG)
assert not res.get("error"), res.get("error")
assert not res["validation"], res["validation"]
print("  self-check:", res["validation"] or "NONE - no bay or weigh-station clash")
for r in sorted(res["schedule"], key=lambda x: (x["channel"], x["start"])):
    print(f"    {r['channel']} {r['start_hhmm']}-{r['end_hhmm']} {r['id']:<3} {r['product']:<7} "
          f"{r['volume']:>4}t {r['company']:<7} {r['duration_min']:>3} min  {r['std_source']}")
for r in res["schedule"]:
    expect = (YUSEN_STD_TOTAL[r["product"]] if is_yusen(r["company"])
              else None)
    if expect is not None:
        assert r["duration_min"] == expect, (r["id"], r["duration_min"], expect)
        assert r["std_source"] == STD_SOURCE_YUSEN and r["bracket"] == "ISO Tank"
    else:
        assert r["std_source"] == STD_SOURCE_LORRY
print("  every Yusen row carries its own standard and is labelled as such ✓")

# the mid-day re-planner must cost Yusen the same way
res2 = build_and_solve(jobs, config=CFG, now_min=8 * 60,
                       baseline={r["id"]: (r["channel"], r["start"]) for r in res["schedule"]})
assert not res2["validation"], res2["validation"]
y = next(r for r in res2["schedule"] if is_yusen(r["company"]))
assert y["duration_min"] == YUSEN_STD_TOTAL[y["product"]]
print(f"  re-plan path agrees: {y['id']} {y['product']} = {y['duration_min']} min ✓")

print("\nALL YUSEN TESTS PASSED")
