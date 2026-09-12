"""
Exact optimizer for the TMMA loading-channel scheduling problem, built as a
time-indexed MILP (Mixed-Integer Linear Program) and solved with scipy's
built-in HiGHS solver (scipy.optimize.milp) — no external solver needed.

Why time-indexed instead of the earlier greedy heuristic:
  - The physical bottleneck (1 camera + 1 weighbridge, used briefly at both
    weigh-in and weigh-out of EVERY truck, regardless of channel) is modeled
    as an explicit shared-resource capacity constraint, not a heuristic
    "+10 min if 3 channels start close together" fudge factor.
  - The solver searches ALL valid combinations at once and proves optimality
    (or proves infeasibility), instead of committing to one job at a time
    and never revisiting that choice (which is the greedy scheduler's known
    limitation).

Objective (2-phase, lexicographic):
  Phase 1: maximize the number of *flexible* DOs scheduled today (fixed
           hard-deadline DOs are mandatory and always included).
  Phase 2: among all schedules achieving that same max count, pick the one
           that finishes earliest (tie-break only, does not affect count).

Time granularity: 5-minute slots (every duration in the standard-time table
is a multiple of 5, so this is exact — no rounding error).
"""
import json
import numpy as np
from scipy.optimize import milp, LinearConstraint, Bounds
from scipy.sparse import lil_matrix

SLOT = 5  # minutes per slot
WINDOW_START_MIN = 8 * 60          # 08:00
WINDOW_END_MIN = 23 * 60 + 30      # 23:30
BREAKS_MIN = [(12 * 60, 12 * 60 + 30), (19 * 60, 19 * 60 + 30)]
CHANNELS = ["A", "B", "C"]

T = (WINDOW_END_MIN - WINDOW_START_MIN) // SLOT  # total slots in the day window

CHANNEL_ELIGIBILITY = {
    "MAA1": ["A", "C"], "MAA2": ["A", "C"], "MAA3": ["B"], "MMA1": ["C"],
    "MMA2": ["A", "B"], "i-BMA": ["B", "C"], "n-BMA1": ["C"], "n-BMA2": ["C"],
}
STD_MAP = {
    "MAA1": "MAA1-2", "MAA2": "MAA1-2", "MAA3": "MAA3", "MMA1": "MMA1",
    "MMA2": "MMA2 CH.A-B", "i-BMA": "IBMA", "n-BMA1": "NBMA", "n-BMA2": "NBMA",
}
# Weight-In / Load(by bracket) / Weight-Out breakdown for LORRY, minutes
# (source: Standard_Time sheet, build.py data_rows)
WEIGH_IN_LORRY = 15
WEIGH_OUT_LORRY = 15
LOAD_LORRY = {
    "MMA2 CH.A-B": {"14MT": 20, "20-22MT": 25, "22-24MT": 30, "24-25MT": 35, "29MT": 40},
    "MMA1":        {"14MT": 25, "20-22MT": 35, "22-24MT": 40, "24-25MT": 45, "29MT": 50},
    "MAA1-2":      {"14MT": 20, "20-22MT": 30, "22-24MT": 35},
    "MAA3":        {"14MT": 20, "20-22MT": 30, "22-24MT": 35},
    "IBMA":        {"14MT": 20},
    "NBMA":        {"14MT": 20},
}
WEIGHIN_SLOTS = WEIGH_IN_LORRY // SLOT    # 3  (channel/bay occupancy - full 15 min)
WEIGHOUT_SLOTS = WEIGH_OUT_LORRY // SLOT  # 3  (channel/bay occupancy - full 15 min)

# Of the 15-min Weight-In / Weight-Out phase, only part of it actually needs the shared
# camera + weighbridge (the rest is paperwork/driving in-out that doesn't need the resource).
# Per user: treat that shared-resource portion as 10 min, occurring at the START of each
# 15-min phase (truck weighs & is photographed first, then moves off for the remaining 5 min).
WEIGH_RESOURCE_MIN = 10
WEIGH_RESOURCE_SLOTS = WEIGH_RESOURCE_MIN // SLOT  # 2

BREAK_SLOTS = set()
for bs, be in BREAKS_MIN:
    s = (bs - WINDOW_START_MIN) // SLOT
    e = (be - WINDOW_START_MIN) // SLOT
    BREAK_SLOTS.update(range(s, e))


def bracket_for_volume(ton):
    if ton <= 14: return "14MT"
    if ton <= 22: return "20-22MT"
    if ton <= 24: return "22-24MT"
    if ton <= 25: return "24-25MT"
    return "29MT"


def job_total_slots(product, volume, realistic_buffer_min, margin_min):
    """Returns (total_slots, load_slots, bracket) or (None, None, bracket) if no std data."""
    std_name = STD_MAP[product]
    bracket = bracket_for_volume(volume)
    base_load = LOAD_LORRY.get(std_name, {}).get(bracket)
    if base_load is None:
        return None, None, bracket
    load_min = base_load + realistic_buffer_min + margin_min
    # round margin/buffer effect to nearest slot (5 min) -- exact if caller uses multiples of 5
    load_slots = round(load_min / SLOT)
    total_slots = WEIGHIN_SLOTS + load_slots + WEIGHOUT_SLOTS
    return total_slots, load_slots, bracket


def candidate_starts(total_slots, forced_t=None):
    """All slot indices t where a job of this total length could start without straddling
    a break or running past the window, or just [forced_t] if a fixed time is required."""
    if forced_t is not None:
        ts = [forced_t]
    else:
        ts = range(0, T - total_slots + 1)
    out = []
    for t in ts:
        span = range(t, t + total_slots)
        if any(s in BREAK_SLOTS for s in span):
            continue
        if t < 0 or t + total_slots > T:
            continue
        out.append(t)
    return out


def min_to_slot(m):
    return (m - WINDOW_START_MIN) // SLOT


def slot_to_hhmm(t):
    if t is None:
        return None
    m = WINDOW_START_MIN + t * SLOT
    return f"{m // 60:02d}:{m % 60:02d}"


def build_and_solve(jobs, realistic_buffer_min=10, verbose=True):
    """
    jobs: list of dicts: {id, product, volume, company, requested (minutes-from-midnight or None), margin}
    Returns dict with schedule, dropped list, and solver stats.
    """
    # --- prepare each job's geometry ---
    prepared = []
    for j in jobs:
        total_slots, load_slots, bracket = job_total_slots(
            j["product"], j["volume"], realistic_buffer_min, j.get("margin", 0))
        eligible = CHANNEL_ELIGIBILITY[j["product"]]
        forced_t = None
        if j.get("requested") is not None:
            forced_t = min_to_slot(j["requested"])
        prepared.append({**j, "total_slots": total_slots, "load_slots": load_slots,
                          "bracket": bracket, "eligible": eligible, "forced_t": forced_t,
                          "is_fixed": j.get("requested") is not None})

    no_std = [j for j in prepared if j["total_slots"] is None]
    prepared = [j for j in prepared if j["total_slots"] is not None]

    # --- enumerate variables: one binary per (job, channel, start-slot) ---
    var_index = {}   # (job_idx, channel, t) -> column index
    var_list = []     # parallel list of (job_idx, channel, t)
    for ji, j in enumerate(prepared):
        if j["forced_t"] is not None and (j["forced_t"] < 0 or j["forced_t"] + j["total_slots"] > T
                                           or any(s in BREAK_SLOTS for s in range(j["forced_t"], j["forced_t"] + j["total_slots"]))):
            # fixed time literally outside window/break -> infeasible by construction, flag later
            j["forced_infeasible"] = True
            continue
        j["forced_infeasible"] = False
        for c in j["eligible"]:
            starts = candidate_starts(j["total_slots"], forced_t=j["forced_t"])
            for t in starts:
                var_index[(ji, c, t)] = len(var_list)
                var_list.append((ji, c, t))

    n_vars = len(var_list)
    fixed_infeasible = [j for j in prepared if j.get("forced_infeasible")]
    prepared_ok = [j for j in prepared if not j.get("forced_infeasible")]

    if n_vars == 0:
        return {"error": "no feasible variables constructed", "no_std": no_std,
                "fixed_infeasible": fixed_infeasible}

    # --- constraint rows ---
    rows = []  # list of (dict{col:coef}, sense, rhs)  sense in {'<=','=='}

    # (a) each job scheduled at most once (== 1 if fixed & feasible)
    job_to_cols = {}
    for ji, c, t in var_list:
        job_to_cols.setdefault(ji, []).append(var_index[(ji, c, t)])
    for ji, j in enumerate(prepared):
        if j.get("forced_infeasible"):
            continue
        cols = job_to_cols.get(ji, [])
        if not cols:
            continue
        rows.append((cols, "==" if j["is_fixed"] else "<=", 1))

    # (b) channel capacity: at most 1 job occupying channel c at slot tau (whole span incl weigh-in/out)
    for c in CHANNELS:
        occ = {tau: [] for tau in range(T)}
        for ji, cc, t in var_list:
            if cc != c:
                continue
            j = prepared[ji]
            for tau in range(t, t + j["total_slots"]):
                occ[tau].append(var_index[(ji, cc, t)])
        for tau in range(T):
            if tau in BREAK_SLOTS or len(occ[tau]) <= 1:
                continue
            rows.append((occ[tau], "<=", 1))

    # (c) shared weigh-station (camera + weighbridge) capacity: at most 1 job weighing at slot tau,
    #     across ALL channels (weigh-in = first WEIGHIN_SLOTS, weigh-out = last WEIGHOUT_SLOTS of span)
    weigh_occ = {tau: [] for tau in range(T)}
    for ji, c, t in var_list:
        j = prepared[ji]
        in_span = range(t, t + WEIGH_RESOURCE_SLOTS)  # first 10 min of the weigh-in phase
        out_phase_start = t + j["total_slots"] - WEIGHOUT_SLOTS
        out_span = range(out_phase_start, out_phase_start + WEIGH_RESOURCE_SLOTS)  # first 10 min of weigh-out phase
        for tau in in_span:
            weigh_occ[tau].append(var_index[(ji, c, t)])
        for tau in out_span:
            weigh_occ[tau].append(var_index[(ji, c, t)])
    for tau in range(T):
        if tau in BREAK_SLOTS or len(weigh_occ[tau]) <= 1:
            continue
        rows.append((weigh_occ[tau], "<=", 1))

    # --- build sparse A matrix ---
    A = lil_matrix((len(rows), n_vars))
    lb, ub = [], []
    for ri, (cols, sense, rhs) in enumerate(rows):
        for col in cols:
            A[ri, col] = 1
        if sense == "==":
            lb.append(rhs); ub.append(rhs)
        else:
            lb.append(-np.inf); ub.append(rhs)
    constraints = LinearConstraint(A.tocsr(), lb, ub)
    integrality = np.ones(n_vars)
    bounds = Bounds(0, 1)

    flex_cols = [job_to_cols[ji] for ji, j in enumerate(prepared) if not j["is_fixed"] and ji in job_to_cols]

    # ---- Phase 1: maximize number of flexible jobs scheduled ----
    c1 = np.zeros(n_vars)
    for cols in flex_cols:
        for col in cols:
            c1[col] = -1  # minimize negative => maximize count
    res1 = milp(c1, constraints=constraints, integrality=integrality, bounds=bounds)
    if not res1.success:
        # Diagnose: this can ONLY happen if two or more *hard-deadline* (fixed) DOs
        # cannot simultaneously be satisfied (fixed jobs are mandatory "==1" rows,
        # so dropping a flexible job can never fix this). Find which fixed DOs are
        # the culprits by checking, one at a time, whether relaxing it to optional
        # restores feasibility (a practical stand-in for a true minimal-conflict-set
        # search, good enough for surfacing an actionable message to the user).
        culprits = []
        fixed_ids = [j["id"] for j in prepared_ok if j["is_fixed"]]
        for fid in fixed_ids:
            trial_rows = []
            for ji, j in enumerate(prepared):
                if j.get("forced_infeasible"):
                    continue
                cols = job_to_cols.get(ji, [])
                if not cols:
                    continue
                sense = "==" if (j["is_fixed"] and j["id"] != fid) else "<="
                trial_rows.append((cols, sense, 1))
            trial_rows += rows[len([j for j in prepared if not j.get("forced_infeasible")]):]
            # rebuild quickly using same channel/weigh rows (rows[b:] onward are resource rows,
            # job rows are rows[:n_job_rows]) -- simpler: just re-solve feasibility with fid relaxed
            A_t = lil_matrix((len(rows), n_vars))
            lb_t, ub_t = [], []
            job_row_i = 0
            resource_rows = rows[sum(1 for j in prepared if not j.get('forced_infeasible')):]
            all_trial_rows = trial_rows[:sum(1 for j in prepared if not j.get('forced_infeasible'))] + list(resource_rows)
            for ri, (cols, sense, rhs) in enumerate(all_trial_rows):
                for col in cols:
                    A_t[ri, col] = 1
                if sense == "==":
                    lb_t.append(rhs); ub_t.append(rhs)
                else:
                    lb_t.append(-np.inf); ub_t.append(rhs)
            trial_constraints = LinearConstraint(A_t.tocsr(), lb_t, ub_t)
            trial_res = milp(np.zeros(n_vars), constraints=trial_constraints, integrality=integrality, bounds=bounds)
            if trial_res.success:
                culprits.append(fid)
        return {"error": "hard_deadline_conflict",
                "message": "Two or more hard-deadline DOs cannot all be satisfied at once "
                            "(channel or shared weigh-station clash). Relaxing any ONE of these "
                            "would make the rest feasible -- needs a human decision on which DO's "
                            "requested time to change.",
                "candidates_to_relax": culprits,
                "no_std": no_std, "fixed_infeasible": fixed_infeasible}
    max_scheduled = round(-res1.fun)

    # ---- Phase 2: same max count, minimize total start slot (prefer compact/early finish) ----
    flex_count_cols = [col for cols in flex_cols for col in cols]
    row_fix = np.zeros(n_vars)
    for col in flex_count_cols:
        row_fix[col] = 1
    A2 = lil_matrix((len(rows) + 1, n_vars))
    A2[:len(rows), :] = A
    A2[len(rows), :] = row_fix
    lb2 = lb + [max_scheduled]
    ub2 = ub + [max_scheduled]
    constraints2 = LinearConstraint(A2.tocsr(), lb2, ub2)

    c2 = np.zeros(n_vars)
    for ji, c, t in var_list:
        c2[var_index[(ji, c, t)]] = t  # prefer earlier starts overall

    res2 = milp(c2, constraints=constraints2, integrality=integrality, bounds=bounds)
    res = res2 if res2.success else res1

    x = res.x
    chosen = {}
    for ji, c, t in var_list:
        if x[var_index[(ji, c, t)]] > 0.5:
            chosen[ji] = (c, t)

    schedule = []
    dropped = []
    for ji, j in enumerate(prepared):
        if j.get("forced_infeasible"):
            dropped.append({**j, "reason": "requested time outside operating window/break"})
            continue
        if ji in chosen:
            c, t = chosen[ji]
            start_min = WINDOW_START_MIN + t * SLOT
            end_min = start_min + j["total_slots"] * SLOT
            schedule.append({
                "id": j["id"], "product": j["product"], "volume": j["volume"], "company": j["company"],
                "channel": c, "start": start_min, "end": end_min,
                "start_hhmm": slot_to_hhmm(t), "end_hhmm": slot_to_hhmm(t + j["total_slots"]),
                "is_fixed": j["is_fixed"], "bracket": j["bracket"],
            })
        else:
            dropped.append({**j, "reason": "no feasible slot within capacity (optimizer chose to drop)"})

    for j in no_std:
        dropped.append({**j, "reason": "no standard-time data for this product/bracket"})

    schedule.sort(key=lambda r: (r["channel"], r["start"]))
    return {
        "status": res.message, "max_scheduled_flex": max_scheduled,
        "n_flex_total": len(flex_cols), "n_fixed": sum(1 for j in prepared if j["is_fixed"]),
        "schedule": schedule, "dropped": dropped, "n_vars": n_vars, "n_constraints": len(rows),
    }
