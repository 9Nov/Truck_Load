"""Mid-day re-planning tests: freezing, GPS release times, plan stability.

Run:  py tests\\test_reschedule.py
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from gps_eta import (  # noqa: E402
    PLACEHOLDER_PLANT_LATLON,
    STATUS_AT_PLANT,
    STATUS_FAR,
    TravelConfig,
    eta_for,
    fleet_etas,
    haversine_km,
    is_placeholder_plant,
    offset_position,
    resolve_plant_latlon,
)
from scheduler_engine import (  # noqa: E402
    LORRY_STD_FROM_ORIGINAL,
    YUSEN_STD_FROM_ORIGINAL,
    SchedulerConfig,
    StabilityWeights,
    baseline_from_schedule,
    build_and_solve,
    hhmm_to_min,
    min_to_hhmm,
)

# The scenarios below are built around 60-minute cycles, so they are pinned to the ISO
# standard tables; the measured P75 defaults are covered by tests/test_std_times.py.
CFG = SchedulerConfig(lorry_std=LORRY_STD_FROM_ORIGINAL, yusen_std=YUSEN_STD_FROM_ORIGINAL)
T = TravelConfig(plant_lat=12.68, plant_lon=101.15, avg_speed_kmh=45,
                 detour_factor=1.35, gate_buffer_min=10)


def show(label, res):
    print(f"\n=== {label} ===")
    if res.get("error"):
        print("  error:", res["error"], "|", res.get("candidates_to_relax"))
        return res
    for r in sorted(res["schedule"], key=lambda x: (x["channel"], x["start"])):
        tag = "FROZEN" if r["is_locked"] else ("FIXED" if r["is_fixed"] else "")
        eta = f" eta={r['earliest_hhmm']}" if r["earliest_hhmm"] else ""
        print(f"  {r['channel']} {r['start_hhmm']}-{r['end_hhmm']} {r['id']:<6} {tag}{eta}")
    for d in res["dropped"]:
        print(f"  DROPPED {d['id']:<6} {d['reason']}")
    print("  self-check:", res["validation"] or "NONE")
    assert not res["validation"], res["validation"]
    return res


def nbma(do_id, requested=None, **kw):
    """n-BMA1 only fits channel C, so a whole test scenario stays on one bay."""
    return {"id": do_id, "product": "n-BMA1", "volume": 14, "company": "SV",
            "requested": requested, "margin": 0, **kw}


# ---------------------------------------------------------------------------
print("### 1. GPS -> ETA arithmetic")
# a point ~22.5 km north of the plant: 22.5 * 1.35 / 45 km/h = 40.5 min + 10 gate
lat, lon = offset_position(22.5, 0, T)
d = haversine_km(lat, lon, T.plant_lat, T.plant_lon)
e = eta_for(lat, lon, hhmm_to_min("09:00"), T, last_seen_min=hhmm_to_min("08:58"))
print(f"  distance={d:.1f} km travel={e['travel_min']} min eta={min_to_hhmm(e['eta_min'])} "
      f"status={e['status']} stale={e['stale']}")
assert 22.0 < d < 23.0, d
assert 39 <= e["travel_min"] <= 42, e
assert e["eta_min"] % 5 == 0 and e["eta_min"] >= 9 * 60 + 50, e   # rounded UP, never optimistic
assert e["status"] == STATUS_FAR if e["travel_min"] > T.enroute_eta_min else True
assert e["stale"] is False

at_gate = eta_for(*offset_position(0.3, 90, T), hhmm_to_min("09:00"), T)
print(f"  at the gate: status={at_gate['status']} eta={min_to_hhmm(at_gate['eta_min'])}")
assert at_gate["status"] == STATUS_AT_PLANT
assert at_gate["eta_min"] == 9 * 60, "a truck already in the yard can be called forward now"

stale = eta_for(lat, lon, hhmm_to_min("09:00"), T, last_seen_min=hhmm_to_min("08:20"))
assert stale["stale"] is True and stale["age_min"] == 40, stale
print("  stale fix flagged:", stale["stale"], f"({stale['age_min']} min old)")

# ---------------------------------------------------------------------------
print("\n### 2. baseline plan for one bay")
base_jobs = [nbma(f"D{i}") for i in range(1, 7)]
plan = show("baseline (channel C only, 6 trucks)", build_and_solve(base_jobs, config=CFG))
baseline = baseline_from_schedule(plan["schedule"])
assert len(plan["schedule"]) == 6
starts = {r["id"]: r["start"] for r in plan["schedule"]}

# ---------------------------------------------------------------------------
print("\n### 3. frozen jobs stay exactly where they are and still block the bay")
now = hhmm_to_min("09:00")
jobs = []
for i, j in enumerate(base_jobs, start=1):
    do_id = j["id"]
    if starts[do_id] < now:          # already started: freeze on its actual bay/time
        jobs.append(dict(j, locked_channel="C", locked_start=starts[do_id]))
    else:
        jobs.append(dict(j))
res = show("re-plan at 09:00, nothing late", build_and_solve(
    jobs, config=CFG, now_min=now, baseline=baseline))
frozen = [r for r in res["schedule"] if r["is_locked"]]
assert frozen, "some trucks should already be running at 09:00"
for r in frozen:
    assert r["start"] == baseline[r["id"]][1] and r["channel"] == baseline[r["id"]][0]
assert all(r["start"] >= now for r in res["schedule"] if not r["is_locked"])
assert not res["baseline_diff"], f"a quiet re-plan must not churn: {res['baseline_diff']}"
print("  frozen:", [r["id"] for r in frozen], "| changes:", len(res["baseline_diff"]))

# ---------------------------------------------------------------------------
print("\n### 4. a truck that cannot arrive is never scheduled before its ETA")
late_id = next(r["id"] for r in plan["schedule"] if r["start"] >= now)
late_eta = hhmm_to_min("14:00")
jobs2 = [dict(j, earliest=late_eta) if j["id"] == late_id else dict(j) for j in jobs]
res2 = show(f"{late_id} stuck in traffic until 14:00", build_and_solve(
    jobs2, config=CFG, now_min=now, baseline=baseline))
placed = {r["id"]: r for r in res2["schedule"]}
assert placed[late_id]["start"] >= late_eta, placed[late_id]
print(f"  {late_id}: แผนเดิม {min_to_hhmm(baseline[late_id][1])} -> ใหม่ {placed[late_id]['start_hhmm']}")

# ---------------------------------------------------------------------------
print("\n### 5. a truck already in the yard fills the gap the late truck left")
gap_start = baseline[late_id][1]
candidates = [r["id"] for r in plan["schedule"] if r["start"] > gap_start]
filler = candidates[0]
jobs3 = []
for j in jobs2:
    if j["id"] == filler:
        jobs3.append(dict(j, earliest=now))   # GPS says it is at the gate right now
    else:
        jobs3.append(dict(j))
res3 = show(f"{filler} is at the gate and can move up", build_and_solve(
    jobs3, config=CFG, now_min=now, baseline=baseline))
placed3 = {r["id"]: r for r in res3["schedule"]}
print(f"  gap was {min_to_hhmm(gap_start)} ({late_id} pushed to "
      f"{placed3[late_id]['start_hhmm']})")
print(f"  {filler}: แผนเดิม {min_to_hhmm(baseline[filler][1])} -> ใหม่ "
      f"{placed3[filler]['start_hhmm']}")
assert placed3[filler]["start"] < baseline[filler][1], \
    "a truck that is provably at the plant should be pulled forward into the gap"
assert placed3[filler]["start"] <= gap_start, "it should actually take the vacated slot"
assert len(res3["schedule"]) == 6, "nobody should be lost by re-planning"

# ---------------------------------------------------------------------------
print("\n### 6. a hard deadline the truck cannot physically make is reported, not moved")
res4 = build_and_solve([
    nbma("H1", requested=hhmm_to_min("10:00"), earliest=hhmm_to_min("12:00")),
    nbma("H2"),
], config=CFG, now_min=hhmm_to_min("09:00"), baseline=None)
show("H1 fixed 10:00 but ETA 12:00", res4)
h1 = next(d for d in res4["dropped"] if d["id"] == "H1")
assert "มาไม่ทัน" in h1["reason"], h1["reason"]
print("  reason:", h1["reason"])

# ---------------------------------------------------------------------------
print("\n### 7. nothing may be scheduled in the past")
res5 = show("re-plan at 15:00 with everything still waiting", build_and_solve(
    [nbma(f"P{i}") for i in range(1, 5)], config=CFG, now_min=hhmm_to_min("15:00")))
assert all(r["start"] >= hhmm_to_min("15:00") for r in res5["schedule"])

# ---------------------------------------------------------------------------
print("\n### 8. delaying a DO is penalised, so the board does not churn for nothing")
loose = StabilityWeights(channel_change=0, delay=0, advance=0, early=1)
res6 = build_and_solve(jobs, config=CFG, now_min=now, baseline=baseline, stability=loose)
tight = build_and_solve(jobs, config=CFG, now_min=now, baseline=baseline,
                        stability=StabilityWeights())
print(f"  changes with no stability term: {len(res6['baseline_diff'])}"
      f" | with the default weights: {len(tight['baseline_diff'])}")
assert len(tight["baseline_diff"]) <= len(res6["baseline_diff"])

# ---------------------------------------------------------------------------
print("\n### 9. a frozen truck that clashes with a hard deadline is surfaced, not hidden")
res7 = build_and_solve([
    nbma("F1", locked_channel="C", locked_start=hhmm_to_min("09:00")),
    nbma("F2", requested=hhmm_to_min("09:00")),
], config=CFG, now_min=hhmm_to_min("09:00"))
print("  error:", res7.get("error"), "| candidates:", res7.get("candidates_to_relax"))
assert res7.get("error") == "hard_deadline_conflict"
assert set(res7["candidates_to_relax"]) == {"F1", "F2"}

# ---------------------------------------------------------------------------
print("\n### 10. fleet_etas over a whole list")
rows = [{"id": "A1", "lat": lat, "lon": lon, "last_seen_min": now - 2},
        {"id": "A2", "lat": None, "lon": None, "last_seen_min": None}]
out = fleet_etas(rows, now, T)
print("  A1:", out["A1"]["status"], min_to_hhmm(out["A1"]["eta_min"]))
print("  A2:", out["A2"]["status"], out["A2"]["eta_min"])
assert out["A2"]["eta_min"] is None, "no GPS = no release time, the solver just uses 'now'"

# ---------------------------------------------------------------------------
print("\n### 11. the shipped default is a placeholder; real coordinates come from secrets")
# The real weighbridge coordinates must never live in source control - this is a
# public repo. The shipped default is a deliberately rough placeholder, and the real
# site is meant to arrive via resolve_plant_latlon() reading deploy-time secrets.
cfg = TravelConfig()
assert (cfg.plant_lat, cfg.plant_lon) == PLACEHOLDER_PLANT_LATLON
assert not cfg.validate(), cfg.validate()
assert is_placeholder_plant(cfg), "the shipped default must be recognised as a placeholder"
print(f"  ค่าเริ่มต้นที่ ship: {cfg.plant_lat}, {cfg.plant_lon} (placeholder) -> ต้องเตือน")

configured = TravelConfig(*resolve_plant_latlon({"plant": {"lat": 10.0, "lon": 100.0}}))
assert not is_placeholder_plant(configured), "a real secret must not be flagged as unset"
print(f"  มี secrets จริง: {configured.plant_lat}, {configured.plant_lon} -> ไม่เตือน")

# malformed / missing secrets must fall back quietly, never crash
for bad in (None, {}, {"plant": {}}, {"plant": {"lat": "not a number", "lon": 101.15}}):
    assert resolve_plant_latlon(bad) == PLACEHOLDER_PLANT_LATLON, bad
print("  secrets ที่ไม่มี/พัง -> fallback เป็น placeholder เงียบ ๆ ไม่ crash ✓")

# a truck sitting exactly on the configured plant is at distance zero
at_gate = eta_for(configured.plant_lat, configured.plant_lon, hhmm_to_min("09:00"), configured)
print(f"  รถที่จุดชั่งพอดี: {at_gate['distance_km']} km · {at_gate['status']}")
assert at_gate["distance_km"] == 0.0 and at_gate["status"] == STATUS_AT_PLANT
assert at_gate["eta_min"] == hhmm_to_min("09:00")

print("\nALL RESCHEDULE TESTS PASSED")
