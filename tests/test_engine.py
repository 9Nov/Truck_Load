"""Solver-level tests: does scheduler_engine produce correct, conflict-free schedules?

Run:  py tests\\test_engine.py
"""
import random
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scheduler_engine import (  # noqa: E402
    LORRY_COMPANIES,
    LORRY_STD_FROM_ORIGINAL,
    YUSEN_STD_FROM_ORIGINAL,
    SchedulerConfig,
    build_and_solve,
    hhmm_to_min,
)

# These cases hand-compute how many trucks fit, so they are pinned to the ISO standard
# tables. What the app now ships as defaults (measured P75) is covered by
# tests/test_std_times.py - the solver behaviour under test here is the same either way.
ORIGINAL = dict(lorry_std=LORRY_STD_FROM_ORIGINAL, yusen_std=YUSEN_STD_FROM_ORIGINAL)


def std_config(**kw):
    return SchedulerConfig(**ORIGINAL, **kw)

VOL = {
    "MAA1": [14, 20, 22, 24], "MAA2": [14, 20, 22, 24], "MAA3": [14, 21, 23],
    "MMA1": [14, 21, 23, 25, 28], "MMA2": [14, 20, 22, 25, 29],
    "i-BMA": [12, 14], "n-BMA1": [13, 14], "n-BMA2": [12, 14],
}
WEIGHTS = [("MMA2", 5), ("MMA1", 4), ("MAA1", 4), ("MAA2", 3), ("MAA3", 3),
           ("i-BMA", 2), ("n-BMA1", 2), ("n-BMA2", 2)]
POOL = [p for p, w in WEIGHTS for _ in range(w)]
FIXED = ["08:00", "10:30", "14:00", "16:30"]


def sample_jobs(n=26, seed=7):
    rnd = random.Random(seed)
    jobs = []
    for i in range(n):
        p = rnd.choice(POOL)
        req = FIXED[i] if i < len(FIXED) else ""
        jobs.append({
            "id": f"DO{1001 + i}", "product": p, "volume": float(rnd.choice(VOL[p])),
            "company": rnd.choice(LORRY_COMPANIES),
            "requested": hhmm_to_min(req) if req else None,
            "margin": rnd.choice([0, 0, 0, 5, 10]),
        })
    return jobs


def run(label, jobs, cfg=None):
    t0 = time.time()
    r = build_and_solve(jobs, config=cfg or std_config())
    took = time.time() - t0
    print(f"\n=== {label} ({len(jobs)} DO) - {took:.1f}s ===")
    if r.get("error"):
        print("  error:", r["error"], "| candidates_to_relax:", r.get("candidates_to_relax"))
        return r
    print(f"  scheduled={len(r['schedule'])} dropped={len(r['dropped'])} "
          f"fixed={r['n_fixed']} flex={r['max_scheduled_flex']}/{r['n_flex_total']} "
          f"vars={r['n_vars']} cons={r['n_constraints']}")
    print("  self-check:", r["validation"] or "NONE - no channel/weigh-station overlap")
    assert not r["validation"], f"SELF-CHECK FAILED: {r['validation']}"
    return r


# 1. a normal day fits
r = run("normal day", sample_jobs(26))
assert len(r["schedule"]) == 26 and not r["dropped"]

# 2. an overloaded day drops the surplus instead of overrunning
r = run("overloaded day", sample_jobs(45, seed=3))
assert r["dropped"], "45 DO should not all fit"
assert all(d["reason"] for d in r["dropped"])
assert all(j["is_fixed"] is False for j in r["dropped"]), "hard deadlines must never be dropped silently"

# 3. two hard deadlines on the same channel-only product at the same minute
r = run("hard-deadline conflict (both MAA3 -> channel B, 09:00)", [
    {"id": "X1", "product": "MAA3", "volume": 14, "company": "SV", "requested": 9 * 60, "margin": 0},
    {"id": "X2", "product": "MAA3", "volume": 14, "company": "VIV", "requested": 9 * 60, "margin": 0},
    {"id": "X3", "product": "MMA1", "volume": 20, "company": "SV", "requested": None, "margin": 0},
])
assert r.get("error") == "hard_deadline_conflict"
assert set(r["candidates_to_relax"]) == {"X1", "X2"}, r["candidates_to_relax"]

# 4. different channels, same minute -> must still clash on the shared camera/weighbridge
r = run("weigh-station conflict (channel B vs C, both 09:00)", [
    {"id": "W1", "product": "MAA3", "volume": 14, "company": "SV", "requested": 9 * 60, "margin": 0},
    {"id": "W2", "product": "MMA1", "volume": 14, "company": "SV", "requested": 9 * 60, "margin": 0},
])
assert r.get("error") == "hard_deadline_conflict", "the shared weigh station must be a hard constraint"

# 5. requested times outside the window / across a break
r = run("requested time outside window or across a break", [
    {"id": "E1", "product": "MMA1", "volume": 14, "company": "SV", "requested": 7 * 60, "margin": 0},
    {"id": "E2", "product": "MMA1", "volume": 14, "company": "SV", "requested": 11 * 60 + 50, "margin": 0},
    {"id": "E3", "product": "MMA1", "volume": 14, "company": "SV", "requested": None, "margin": 0},
])
assert {d["id"] for d in r["dropped"]} == {"E1", "E2"}, r["dropped"]

# 6. configuration really comes from the caller, not from constants
cfg = std_config(window_start_min=hhmm_to_min("06:00"), window_end_min=hhmm_to_min("18:00"),
                 breaks_min=[(hhmm_to_min("11:30"), hhmm_to_min("12:15"))],
                 realistic_buffer_min=15, weigh_resource_min=15)
r = run("custom config 06:00-18:00, buffer 15, weigh 15", sample_jobs(30, seed=11), cfg)
assert all(6 * 60 <= x["start"] and x["end"] <= 18 * 60 for x in r["schedule"])

# 7. products with no standard time for that volume bracket
r = run("no standard-time data", [
    {"id": "N1", "product": "MAA1", "volume": 28, "company": "SV", "requested": None, "margin": 0},
    {"id": "N2", "product": "i-BMA", "volume": 20, "company": "SV", "requested": None, "margin": 0},
])
assert len(r["dropped"]) == 2

# 8. empty input must not explode
r = build_and_solve([], config=std_config())
print("\n=== empty input ===\n  error:", r.get("error"), "| schedule:", len(r.get("schedule", [])))

print("\nALL ENGINE TESTS PASSED")
