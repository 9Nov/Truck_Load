"""
TMMA loading-channel scheduler engine.

Time-indexed MILP (5-minute slots) solved with scipy's built-in HiGHS solver
(scipy.optimize.milp) -- no external solver needed.

This is milp_optimizer.py with every tunable constant lifted out of module
scope and into a SchedulerConfig object supplied by the caller (the Streamlit
sidebar).  The core algorithm is unchanged:

  Phase 1: maximize the number of *flexible* DOs scheduled today (fixed
           hard-deadline DOs are mandatory and always included).
  Phase 2: among all schedules achieving that same max count, pick the best
           one by a cost that is pure tie-break when planning from scratch
           (earliest overall start) and adds plan-stability terms when
           re-planning mid-day against a committed baseline.

Why time-indexed instead of a greedy heuristic: the physical bottleneck
(1 camera + 1 weighbridge shared by all three channels, used briefly at both
weigh-in and weigh-out of EVERY truck) is an explicit capacity constraint the
solver must honour, not a fudge factor.  The greedy prototype was measured
letting 27 trucks collide on that shared resource over a 40-DO day.

Mid-day re-planning (see rolling-horizon notes in README) reuses the exact same
model with three extra per-job inputs:

  earliest        - release time: the DO cannot start before this (from GPS ETA)
  locked_channel  - the truck is already loading / has finished: freeze it here
  locked_start      so it still consumes channel and weigh-station capacity

plus an optional `baseline` (the committed plan) that the objective is pulled
towards, so a re-optimisation does not churn the whole board for no reason.
"""
from dataclasses import dataclass, field
from typing import Optional

import numpy as np
from scipy.optimize import Bounds, LinearConstraint, milp
from scipy.sparse import lil_matrix

SLOT = 5  # minutes per slot -- every standard time is a multiple of 5, so this is exact
CHANNELS = ["A", "B", "C"]

PRODUCTS = ["MAA1", "MAA2", "MAA3", "MMA1", "MMA2", "i-BMA", "n-BMA1", "n-BMA2"]
# Transport Co. is NOT cosmetic: it selects which standard-time table a DO is costed with.
YUSEN = "Yusen"                                  # ISO Tank fleet, its own standard times
LORRY_COMPANIES = ["ศรีไทย", "SV", "VIV"]        # everything else runs LORRY
TRANSPORT_COMPANIES = LORRY_COMPANIES + [YUSEN]

# Channel eligibility per product (loading station layout).
# Channel D is deliberately absent: it serves Truck Drum / Dry Container only,
# never the LORRY traffic this scheduler plans.
CHANNEL_ELIGIBILITY = {
    "MAA1": ["A", "C"], "MAA2": ["A", "C"], "MAA3": ["B"], "MMA1": ["C"],
    "MMA2": ["A", "B"], "i-BMA": ["B", "C"], "n-BMA1": ["C"], "n-BMA2": ["C"],
}
PRODUCT_FAMILY = {
    "MAA1": "MAA", "MAA2": "MAA", "MAA3": "MAA",
    "MMA1": "MMA", "MMA2": "MMA",
    "i-BMA": "i-BMA", "n-BMA1": "n-BMA", "n-BMA2": "n-BMA",
}
STD_MAP = {
    "MAA1": "MAA1-2", "MAA2": "MAA1-2", "MAA3": "MAA3", "MMA1": "MMA1",
    "MMA2": "MMA2 CH.A-B", "i-BMA": "IBMA", "n-BMA1": "NBMA", "n-BMA2": "NBMA",
}

# Weight-In / Load(by bracket) / Weight-Out breakdown for LORRY, minutes.
WEIGH_IN_LORRY = 15
WEIGH_OUT_LORRY = 15
WEIGH_OVERHEAD = WEIGH_IN_LORRY + WEIGH_OUT_LORRY   # the 30 min outside the Load phase

# The original ISO standard table, load-only minutes. Kept verbatim so the app can
# always show "what the standard says" next to "what we actually plan with", and so
# a planner can roll the whole thing back to it.
LOAD_LORRY_ORIGINAL_STD = {
    "MMA2 CH.A-B": {"14MT": 20, "20-22MT": 25, "22-24MT": 30, "24-25MT": 35, "29MT": 40},
    "MMA1":        {"14MT": 25, "20-22MT": 35, "22-24MT": 40, "24-25MT": 45, "29MT": 50},
    "MAA1-2":      {"14MT": 20, "20-22MT": 30, "22-24MT": 35},
    "MAA3":        {"14MT": 20, "20-22MT": 30, "22-24MT": 35},
    "IBMA":        {"14MT": 20},
    "NBMA":        {"14MT": 20},
}
LOAD_LORRY = LOAD_LORRY_ORIGINAL_STD   # backwards-compatible alias
WEIGHIN_SLOTS = WEIGH_IN_LORRY // SLOT    # channel/bay occupancy - the full 15 min
WEIGHOUT_SLOTS = WEIGH_OUT_LORRY // SLOT  # channel/bay occupancy - the full 15 min

# --- Yusen (ISO Tank) --------------------------------------------------------
# Whole-cycle standard time, gate to gate, in minutes. Unlike the LORRY table this
# does NOT vary with volume - one number per product, any tonnage.
# Source categories were I-BMA 100 / MAA 110 / MMA1 110 / MMA2 100 / nBMA 100;
# "MAA" covers every MAA grade and "nBMA" every n-BMA grade, expanded here.
#
# Validated against 1,213 real Yusen trips on channels A/B/C (Sep 2025 - Sep 2026):
#   Product      Standard   Median actual   % of trips finishing within standard
#   I-BMA          100          91.5                 67.9%
#   MAA1           110          98.0                 73.6%
#   MAA2           110          92.0                 76.6%
#   MAA3           110          99.0                 70.2%
#   MMA1           110         106.5                 63.6%
#   MMA2           100          90.0                 72.1%
#   n-BMA1         100          83.0                 78.7%
#   n-BMA2         100          92.0                 67.9%
#   overall                                          71.4%
# i.e. each standard already sits around the 64-79th percentile of actual times
# (roughly a P75 planning level), so it ALREADY contains its own allowance ->
# the Realistic Buffer must not be added on top (SchedulerConfig.yusen_buffer_min = 0).
#
# Known but deliberately not modelled this round: i-BMA is ~11 min faster on channel B
# than on channel C (median 86 vs 97.5, p = 0.0016). The supplied standard is a single
# 100-minute figure, so that is what is used.
YUSEN_STD_TOTAL_ORIGINAL_STD = {
    "i-BMA": 100, "MAA1": 110, "MAA2": 110, "MAA3": 110,
    "MMA1": 110, "MMA2": 100, "n-BMA1": 100, "n-BMA2": 100,
}
YUSEN_STD_TOTAL = YUSEN_STD_TOTAL_ORIGINAL_STD   # backwards-compatible alias
# The whole-cycle number is split into the model's three phases. Weigh-in/weigh-out stay
# at 15 min because weighing, photographing and paperwork are the same for every carrier
# regardless of vehicle type - what actually differs is the time spent filling the tank.
YUSEN_WEIGH_OVERHEAD = WEIGH_IN_LORRY + WEIGH_OUT_LORRY  # 30 min taken out of the total
STD_SOURCE_LORRY = "LORRY"
STD_SOURCE_YUSEN = "Yusen ISO Tank"

# Minimum minutes a physical truck (tracked by its "Plan truck" label) must rest
# between the end of one job and the start of its next one -- driving away, turning
# around and queuing back in takes real time even when the bay itself is free.
TRUCK_GAP_MIN_DEFAULT = 300


# ---------------------------------------------------------------------------
# Planning tables: what the app plans with by default
# ---------------------------------------------------------------------------
# These are NOT the ISO standard. They are the 75th percentile of what trucks
# actually took over the last 12 months (Lorry 3,601 trips, Yusen 1,213 trips),
# rounded onto the model's 5-minute grid.
#
# Why P75 and not the median: the MILP is deterministic - one fixed duration per
# DO, no variance - and queues jobs back to back, so the number fed in IS the
# probability of not running into the next slot. A median would overrun about half
# the time and push the rest of the day late; P75 leaves roughly a 25% chance of
# overrun, which is the level the Yusen standard was already pitched at.
#
# Because P75 already carries that allowance, rows sourced from actuals default to
# a Realistic Buffer of 0. Only rows that fall back to the old standard - where no
# real trip exists to measure - keep the usual 10-minute buffer.
SRC_ACTUAL = "Actual (P75 จริง)"
SRC_STANDARD = "Standard เดิม (ไม่มีข้อมูลจริง)"

BUFFER_FOR_ACTUAL = 0
BUFFER_FOR_STANDARD = 10


@dataclass(frozen=True)
class StdEntry:
    """One cell of a standard-time table.

    total_min is the WHOLE cycle (weigh-in + load + weigh-out), the same basis the
    standard sheets use; the Load phase is total_min - WEIGH_OVERHEAD.
    """
    total_min: int
    source: str = SRC_ACTUAL
    buffer_min: Optional[int] = None   # None -> fall back to the config-level buffer
    p75_raw: Optional[float] = None     # unrounded reference figure, display only
    n: int = 0                          # trips behind it, display only
    note: str = ""

    @property
    def load_min(self) -> int:
        return self.total_min - WEIGH_OVERHEAD


def _actual(total, p75, n, note=""):
    return StdEntry(total, SRC_ACTUAL, BUFFER_FOR_ACTUAL, p75, n, note)


def _standard(total, note=""):
    return StdEntry(total, SRC_STANDARD, BUFFER_FOR_STANDARD, None, 0, note)


# MAA1-2 and MAA3 share one measurement: the trip data does not separate the grades.
_MAA_14 = _actual(85, 86.5, 351, "ข้อมูลจริงไม่แยกเกรด MAA1/2/3")
_MAA_22 = _actual(95, 97.0, 38, "ข้อมูลจริงไม่แยกเกรด MAA1/2/3")
_MAA_24 = _standard(65, "ไม่มีเที่ยวจริงในกลุ่มนี้เลย (n=0)")

LORRY_STD_DEFAULT = {
    "MMA1": {
        "14MT": _actual(85, 84.0, 56), "20-22MT": _actual(100, 99.8, 50),
        "22-24MT": _actual(100, 102.0, 245), "24-25MT": _actual(95, 94.0, 53),
        "29MT": _actual(105, 105.0, 37),
    },
    "MMA2 CH.A-B": {
        "14MT": _actual(75, 77.0, 1082), "20-22MT": _actual(80, 81.0, 367),
        "22-24MT": _actual(85, 85.0, 689), "24-25MT": _actual(90, 88.0, 178),
        "29MT": _actual(85, 87.0, 229),
    },
    "MAA1-2": {"14MT": _MAA_14, "20-22MT": _MAA_22, "22-24MT": _MAA_24},
    "MAA3": {"14MT": _MAA_14, "20-22MT": _MAA_22, "22-24MT": _MAA_24},
    "IBMA": {"14MT": _actual(90, 88.5, 60)},
    "NBMA": {"14MT": _actual(80, 80.0, 166, "วัดจาก n-BMA2 เท่านั้น")},
}

_MAA_ISO = _actual(110, 111.0, 544, "รวม MAA1+MAA2+MAA3 — ข้อมูลจริงไม่แยกเกรด")
_NBMA_ISO = _actual(105, 104.0, 402, "รวม n-BMA1+n-BMA2 — ข้อมูลจริงไม่แยกเกรด")

YUSEN_STD_DEFAULT = {
    "i-BMA": _actual(105, 106.2, 112),
    "MAA1": _MAA_ISO, "MAA2": _MAA_ISO, "MAA3": _MAA_ISO,
    "MMA1": _actual(125, 127.2, 44, "standard เดิมหลวมที่สุด: 110 → 125"),
    "MMA2": _actual(100, 101.0, 111),
    "n-BMA1": _NBMA_ISO, "n-BMA2": _NBMA_ISO,
}

# The same two tables expressed from the untouched ISO standard, for the rollback
# button and for tests that want the pre-existing behaviour. buffer_min is left as
# None so the config-level Realistic Buffer still governs, exactly as it did before
# these tables became data - the UI supplies the usual 10 / 0 when it shows them.
LORRY_STD_FROM_ORIGINAL = {
    group: {bracket: StdEntry(load + WEIGH_OVERHEAD, SRC_STANDARD)
            for bracket, load in brackets.items()}
    for group, brackets in LOAD_LORRY_ORIGINAL_STD.items()
}
YUSEN_STD_FROM_ORIGINAL = {
    product: StdEntry(total, SRC_STANDARD)
    for product, total in YUSEN_STD_TOTAL_ORIGINAL_STD.items()
}


def is_yusen(transport_co) -> bool:
    """Carrier match is case/space tolerant so 'yusen ' from a spreadsheet still counts."""
    return str(transport_co or "").strip().casefold() == YUSEN.casefold()


@dataclass
class SchedulerConfig:
    """Everything the planner can tune from the UI."""
    window_start_min: int = 8 * 60           # 08:00 first possible start
    window_end_min: int = 23 * 60 + 30       # 23:30 last truck must be finished
    breaks_min: list = field(default_factory=lambda: [(12 * 60, 12 * 60 + 30),
                                                      (19 * 60, 19 * 60 + 30)])
    # FALLBACK BUFFERS ONLY. Every entry in the tables below carries its own buffer,
    # and those always win (see job_total_slots). These two are consulted solely when a
    # caller invokes the engine with an entry whose buffer_min is None - tests, batch
    # scripts, anything not going through the Standard Time page. The app does not set
    # them: it edits the buffer per row, where it can differ per figure, which is what
    # the data actually requires (P75 rows need 0, fallback-to-standard rows need 10).
    realistic_buffer_min: int = 10           # LORRY rows with no buffer of their own
    yusen_buffer_min: int = 0                # Yusen standards already include their own allowance
    weigh_resource_min: int = 10             # camera+weighbridge time inside each 15-min weigh phase
    # Channel blackouts: (channel, start_min, end_min, reason). A bay cannot take any
    # truck while it is down for maintenance, so unlike a break this is per channel -
    # the other two bays keep working straight through it.
    blackouts: list = field(default_factory=list)
    # Standard-time tables. Empty -> the measured defaults above. The app fills these
    # from what the planner edited on the Standard Time page, so an edit there is what
    # the solver costs the day with.
    lorry_std: dict = field(default_factory=dict)
    yusen_std: dict = field(default_factory=dict)
    # Same-truck cooldown: minutes required between one job's end and the next job's
    # start when both rows carry the same non-blank "Plan truck" label. 0 disables the
    # constraint entirely (no rows added, no self-check) -- old uploads with no Plan
    # truck column never populate the label, so this never fires for them either.
    truck_gap_min: int = TRUCK_GAP_MIN_DEFAULT

    # ---- derived ----
    @property
    def total_slots(self) -> int:
        return (self.window_end_min - self.window_start_min) // SLOT

    @property
    def weigh_resource_slots(self) -> int:
        return max(1, round(self.weigh_resource_min / SLOT))

    @property
    def truck_gap_slots(self) -> int:
        return max(0, round(self.truck_gap_min / SLOT))

    def lorry_entry(self, group, bracket) -> Optional[StdEntry]:
        table = self.lorry_std or LORRY_STD_DEFAULT
        return table.get(group, {}).get(bracket)

    def yusen_entry(self, product) -> Optional[StdEntry]:
        table = self.yusen_std or YUSEN_STD_DEFAULT
        return table.get(product)

    def blackouts_for(self, channel) -> list:
        """[(start_min, end_min, reason)] for one bay, clipped to the operating window."""
        out = []
        for item in self.blackouts:
            ch, bs, be = item[0], item[1], item[2]
            reason = item[3] if len(item) > 3 else ""
            if ch != channel or be <= bs:
                continue
            s = max(bs, self.window_start_min)
            e = min(be, self.window_end_min)
            if e > s:
                out.append((s, e, reason))
        return sorted(out)

    def blackout_slots(self, channel) -> set:
        """Slots this bay is unavailable, rounded OUTWARDS so a partly-covered slot is
        still treated as unusable - never optimistic about a bay being back up."""
        out = set()
        for bs, be, _ in self.blackouts_for(channel):
            s = (bs - self.window_start_min) // SLOT
            e = -(-(be - self.window_start_min) // SLOT)
            out.update(range(max(0, s), min(self.total_slots, e)))
        return out

    def blackout_minutes(self, channel) -> int:
        """Downtime that actually costs this bay capacity - overlap with a break does
        not count twice, the bay was already closed then."""
        total = 0
        for bs, be, _ in self.blackouts_for(channel):
            overlap = sum(max(0, min(rest_e, be) - max(rest_s, bs))
                          for rest_s, rest_e in self.breaks_min)
            total += (be - bs) - overlap
        return total

    @property
    def break_slots(self) -> set:
        out = set()
        for bs, be in self.breaks_min:
            s = (bs - self.window_start_min) // SLOT
            e = -(-(be - self.window_start_min) // SLOT)  # ceil, so a partial slot is still blocked
            out.update(range(max(0, s), min(self.total_slots, e)))
        return out

    def validate(self) -> list:
        """Returns a list of human-readable problems with the configuration itself."""
        problems = []
        if self.window_end_min <= self.window_start_min:
            problems.append("Operating window end must be after its start.")
        if self.window_start_min % SLOT or self.window_end_min % SLOT:
            problems.append(f"Window start/end should be on a {SLOT}-minute boundary.")
        if self.realistic_buffer_min < 0:
            problems.append("Realistic buffer cannot be negative.")
        if self.yusen_buffer_min < 0:
            problems.append("Yusen realistic buffer cannot be negative.")
        if not (0 < self.weigh_resource_min <= WEIGH_IN_LORRY):
            problems.append(f"Shared weigh-resource time must be between 1 and {WEIGH_IN_LORRY} minutes.")
        if self.truck_gap_min < 0:
            problems.append("Truck cooldown gap cannot be negative.")
        for bs, be in self.breaks_min:
            if be <= bs:
                problems.append(f"Break {min_to_hhmm(bs)}-{min_to_hhmm(be)} ends before it starts.")
        for item in self.blackouts:
            ch, bs, be = item[0], item[1], item[2]
            if ch not in CHANNELS:
                problems.append("ช่วงปิดช่อง: '%s' ไม่ใช่ช่องโหลด (ต้องเป็น %s)"
                                % (ch, ", ".join(CHANNELS)))
            elif be <= bs:
                problems.append(f"ช่วงปิดช่อง {ch} {min_to_hhmm(bs)}-{min_to_hhmm(be)}: "
                                "เวลาจบต้องอยู่หลังเวลาเริ่ม")
        return problems


@dataclass
class StabilityWeights:
    """How hard a mid-day re-optimisation is pulled towards the committed plan.

    The penalty is deliberately ASYMMETRIC. Pushing a DO later, or onto another
    bay, breaks a commitment somebody already acted on (the driver was told a time
    and a channel) and is charged for. Pulling a DO earlier costs nothing by
    default: the release time already proves that truck can physically be there,
    so moving it up just fills the gap the late trucks left behind - which is the
    whole point of re-planning mid-day.

    Only used when `baseline` is passed to build_and_solve; planning from scratch
    uses `early` alone, which reproduces the original 'finish earliest' tie-break.
    """
    channel_change: int = 200  # a DO moving to a different loading bay
    delay: int = 8             # per 5-min slot pushed LATER than committed
    advance: int = 0           # per 5-min slot pulled EARLIER than committed
    early: int = 1             # mild pull towards starting the day's work early


# ----------------------------------------------------------------------------
# small helpers
# ----------------------------------------------------------------------------
def min_to_hhmm(m) -> str:
    if m is None:
        return ""
    m = int(round(m))
    return f"{(m // 60) % 24:02d}:{m % 60:02d}"


def hhmm_to_min(text) -> Optional[int]:
    """Accepts '8:00', '08:00', '0800', a datetime.time, or None/blank."""
    if text is None:
        return None
    if hasattr(text, "hour") and hasattr(text, "minute"):
        return text.hour * 60 + text.minute
    s = str(text).strip()
    if not s or s.lower() in {"nan", "nat", "none", "-"}:
        return None
    s = s.replace(".", ":")
    if ":" in s:
        parts = s.split(":")
        try:
            return int(parts[0]) * 60 + int(float(parts[1]))
        except (ValueError, IndexError):
            return None
    if s.isdigit() and len(s) in (3, 4):
        return int(s[:-2]) * 60 + int(s[-2:])
    return None


def bracket_for_volume(ton) -> str:
    if ton <= 14:
        return "14MT"
    if ton <= 22:
        return "20-22MT"
    if ton <= 24:
        return "22-24MT"
    if ton <= 25:
        return "24-25MT"
    return "29MT"


def job_total_slots(product, volume, realistic_buffer_min, margin_min,
                    transport_co=None, yusen_buffer_min=0, lorry_std=None, yusen_std=None):
    """How long one truck occupies a bay, in 5-minute slots.

    Two tables, chosen by carrier:
      Yusen (ISO Tank) - one whole-cycle figure per product, tonnage-independent
      everyone else    - LORRY: whole-cycle figure per standard group x weight bracket

    Each entry carries its own Realistic Buffer (0 where the figure came from measured
    P75 data, which already includes the allowance; 10 where it fell back to the ISO
    standard). An entry with no buffer of its own uses the config-level buffer, which
    is what keeps older callers behaving exactly as before.
    The per-DO Margin is added in both cases.

    Returns (total_slots, load_slots, bracket, std_source);
    total/load are None when no standard time exists for that product/size.
    """
    if is_yusen(transport_co):
        entry = (yusen_std or YUSEN_STD_DEFAULT).get(product)
        bracket, source, fallback = "ISO Tank", STD_SOURCE_YUSEN, yusen_buffer_min
    else:
        group = STD_MAP[product]
        bracket = bracket_for_volume(volume)
        entry = (lorry_std or LORRY_STD_DEFAULT).get(group, {}).get(bracket)
        source, fallback = STD_SOURCE_LORRY, realistic_buffer_min

    if entry is None:
        return None, None, bracket, source

    buffer_min = fallback if entry.buffer_min is None else entry.buffer_min
    total_min = entry.total_min + buffer_min + margin_min
    total_slots = max(WEIGHIN_SLOTS + WEIGHOUT_SLOTS + 1, round(total_min / SLOT))
    return total_slots, total_slots - WEIGHIN_SLOTS - WEIGHOUT_SLOTS, bracket, source


def candidate_starts(total_slots, cfg, forced_t=None, earliest_t=None, channel=None):
    """Slot indices where a job of this length can start without straddling a break,
    running past the window, or overlapping a maintenance blackout on `channel`;
    just [forced_t] when the DO has a hard deadline.
    `earliest_t` is the release time -- the truck cannot physically be there before it."""
    T = cfg.total_slots
    blocked = set(cfg.break_slots)
    if channel is not None:
        blocked |= cfg.blackout_slots(channel)
    ts = [forced_t] if forced_t is not None else range(0, T - total_slots + 1)
    out = []
    for t in ts:
        if t < 0 or t + total_slots > T:
            continue
        if earliest_t is not None and t < earliest_t:
            continue
        if any(s in blocked for s in range(t, t + total_slots)):
            continue
        out.append(t)
    return out


def _slot_to_min(t, cfg):
    return cfg.window_start_min + t * SLOT


def _min_to_slot_floor(m, cfg):
    return int((m - cfg.window_start_min) // SLOT)


def _min_to_slot_ceil(m, cfg):
    return int(-(-(m - cfg.window_start_min) // SLOT))


# ----------------------------------------------------------------------------
# solver
# ----------------------------------------------------------------------------
def _build_matrix(rows, n_vars):
    A = lil_matrix((len(rows), n_vars))
    lb, ub = [], []
    for ri, (cols, sense, rhs) in enumerate(rows):
        for col in cols:
            A[ri, col] = 1
        if sense == "==":
            lb.append(rhs)
            ub.append(rhs)
        else:
            lb.append(-np.inf)
            ub.append(rhs)
    return A.tocsr(), lb, ub


def build_and_solve(jobs, config=None, time_limit=120.0,
                    now_min=None, baseline=None, stability=None):
    """
    jobs: list of dicts
        required: id, product, volume
        optional: company, margin
                  requested       - hard deadline, minutes from midnight
                  earliest        - release time (GPS ETA), minutes from midnight
                  locked_channel  - freeze this DO on this channel ...
                  locked_start      ... starting exactly here (already loading / done)
    config:    SchedulerConfig (defaults to the standard TMMA day)
    now_min:   'time now' for a mid-day re-plan; no un-started DO may begin before it
    baseline:  {do_id: (channel, start_min)} committed plan the solver should stay close to
    stability: StabilityWeights

    Returns a dict with either:
      error == 'hard_deadline_conflict'  -> conflicting fixed DOs, needs a human decision
      error == 'no_feasible_variables'   -> nothing could even be placed
      otherwise: schedule / dropped / stats / validation / baseline_diff
    """
    cfg = config or SchedulerConfig()
    weights = stability or StabilityWeights()
    T = cfg.total_slots
    res_slots = cfg.weigh_resource_slots

    # --- prepare each job's geometry -----------------------------------------
    prepared = []
    for j in jobs:
        total_slots, load_slots, bracket, std_source = job_total_slots(
            j["product"], j["volume"], cfg.realistic_buffer_min, j.get("margin", 0) or 0,
            transport_co=j.get("company"), yusen_buffer_min=cfg.yusen_buffer_min,
            lorry_std=cfg.lorry_std, yusen_std=cfg.yusen_std)
        requested = j.get("requested")
        locked_channel, locked_start = j.get("locked_channel"), j.get("locked_start")
        is_locked = locked_channel is not None and locked_start is not None

        earliest = j.get("earliest")
        if now_min is not None and not is_locked:
            earliest = now_min if earliest is None else max(earliest, now_min)

        pin_channel = j.get("pin_channel")
        if is_locked:
            forced_t = int(round((locked_start - cfg.window_start_min) / SLOT))
            eligible = [locked_channel]
        else:
            forced_t = (int(round((requested - cfg.window_start_min) / SLOT))
                        if requested is not None else None)
            eligible = CHANNEL_ELIGIBILITY[j["product"]]
            if pin_channel:
                # the planner committed this truck to one bay (a manual swap);
                # it is still validated like any other job, it just cannot roam
                eligible = [pin_channel] if pin_channel in eligible else []

        prepared.append({
            **j, "total_slots": total_slots, "load_slots": load_slots, "bracket": bracket,
            "std_source": std_source,
            "eligible": eligible, "forced_t": forced_t, "pin_channel": pin_channel,
            "is_pinned": bool(pin_channel) and not is_locked,
            "is_fixed": requested is not None and not is_locked,
            "is_locked": is_locked, "locked_channel": locked_channel, "locked_start": locked_start,
            "earliest": earliest,
            "earliest_t": _min_to_slot_ceil(earliest, cfg) if earliest is not None else None,
            "requested_hhmm": min_to_hhmm(requested) if requested is not None else "",
            "earliest_hhmm": min_to_hhmm(earliest) if earliest is not None else "",
            "plan_truck": str(j.get("plan_truck") or "").strip(),
        })

    no_std = [j for j in prepared if j["total_slots"] is None]
    prepared = [j for j in prepared if j["total_slots"] is not None]

    # --- one binary variable per (job, channel, start-slot) -------------------
    var_index, var_list = {}, []
    for ji, j in enumerate(prepared):
        if j["is_locked"]:
            # already happening: keep it exactly where it is, even if the actual start
            # drifted into a break or past the nominal window -- it is reality, not a choice
            per_channel = {j["locked_channel"]: [j["forced_t"]]}
        else:
            # start times are worked out per bay now: a maintenance blackout closes one
            # channel without touching the other two
            per_channel = {c: candidate_starts(j["total_slots"], cfg, forced_t=j["forced_t"],
                                               earliest_t=j["earliest_t"], channel=c)
                           for c in j["eligible"]}
        if not j["eligible"] or not any(per_channel.values()):
            j["forced_infeasible"] = True
            j["infeasible_reason"] = _why_infeasible(j, cfg, T)
            continue
        j["forced_infeasible"] = False
        for c, starts in per_channel.items():
            for t in starts:
                var_index[(ji, c, t)] = len(var_list)
                var_list.append((ji, c, t))

    n_vars = len(var_list)
    fixed_infeasible = [j for j in prepared if j.get("forced_infeasible")]
    if n_vars == 0:
        dropped = [{**j, "reason": j["infeasible_reason"]} for j in fixed_infeasible]
        dropped += [{**j, "reason": "no standard-time data for this product/volume bracket"}
                    for j in no_std]
        return {"error": "no_feasible_variables", "config": cfg,
                "message": "No DO can be placed at all inside the current operating window.",
                "no_std": no_std, "fixed_infeasible": fixed_infeasible,
                "schedule": [], "dropped": dropped,
                "validation": [], "channel_summary": channel_summary([], cfg),
                "baseline_diff": diff_vs_baseline([], dropped, baseline) if baseline else []}

    job_to_cols = {}
    for ji, c, t in var_list:
        job_to_cols.setdefault(ji, []).append(var_index[(ji, c, t)])

    # --- (a) assignment rows: each job at most once; fixed and frozen DOs exactly once
    job_rows = []
    for ji, j in enumerate(prepared):
        cols = job_to_cols.get(ji)
        if not cols:
            continue
        job_rows.append((cols, "==" if (j["is_fixed"] or j["is_locked"]) else "<=", 1))

    resource_rows = []
    # --- (b) channel capacity: one truck per channel per slot (whole 15+load+15 span)
    for c in CHANNELS:
        occ = {tau: [] for tau in range(T)}
        for ji, cc, t in var_list:
            if cc != c:
                continue
            for tau in range(t, t + prepared[ji]["total_slots"]):
                if 0 <= tau < T:
                    occ[tau].append(var_index[(ji, cc, t)])
        for tau in range(T):
            if len(occ[tau]) > 1:
                resource_rows.append((occ[tau], "<=", 1))

    # --- (c) shared camera + weighbridge: one truck weighing at a time, across ALL channels
    weigh_occ = {tau: [] for tau in range(T)}
    for ji, c, t in var_list:
        col = var_index[(ji, c, t)]
        out_phase_start = t + prepared[ji]["total_slots"] - WEIGHOUT_SLOTS
        taus = list(range(t, t + res_slots)) + \
            list(range(out_phase_start, out_phase_start + res_slots))
        for tau in taus:
            if 0 <= tau < T:
                weigh_occ[tau].append(col)
    for tau in range(T):
        if len(weigh_occ[tau]) > 1:
            resource_rows.append((weigh_occ[tau], "<=", 1))

    # --- (d) same-truck cooldown: one physical truck can only be at one job at a
    # time, and needs `truck_gap_min` after dropping one load before it can start the
    # next -- driving off, turning around and queuing back in takes real time even
    # when the bay itself is free. Modelled by giving each job's occupancy an extra
    # `gap_slots` tail: two jobs sharing a truck conflict (need <=1 of them chosen at
    # any shared tau) exactly when their tail-extended windows overlap, which happens
    # exactly when the true gap between them (in either order) is under the minimum.
    gap_slots = cfg.truck_gap_slots
    if gap_slots > 0:
        truck_groups = {}
        for ji, j in enumerate(prepared):
            if j["plan_truck"]:
                truck_groups.setdefault(j["plan_truck"], []).append(ji)
        truck_job_set = {ji for jis in truck_groups.values() if len(jis) > 1 for ji in jis}
        if truck_job_set:
            truck_occ = {}
            for ji, c, t in var_list:
                if ji not in truck_job_set:
                    continue
                truck = prepared[ji]["plan_truck"]
                end_tau = min(T, t + prepared[ji]["total_slots"] + gap_slots)
                for tau in range(t, end_tau):
                    truck_occ.setdefault((truck, tau), []).append(var_index[(ji, c, t)])
            for cols in truck_occ.values():
                if len(cols) > 1:
                    resource_rows.append((cols, "<=", 1))

    rows = job_rows + resource_rows
    A, lb, ub = _build_matrix(rows, n_vars)
    constraints = LinearConstraint(A, lb, ub)
    integrality = np.ones(n_vars)
    bounds = Bounds(0, 1)
    opts = {"time_limit": time_limit} if time_limit else {}

    flex_cols = [job_to_cols[ji] for ji, j in enumerate(prepared)
                 if not (j["is_fixed"] or j["is_locked"]) and ji in job_to_cols]

    # ---- Phase 1: maximize the number of flexible DOs scheduled --------------
    c1 = np.zeros(n_vars)
    for cols in flex_cols:
        for col in cols:
            c1[col] = -1  # minimizing the negative == maximizing the count
    res1 = milp(c1, constraints=constraints, integrality=integrality, bounds=bounds, options=opts)
    if not res1.success:
        return _diagnose_conflict(prepared, job_to_cols, resource_rows, n_vars,
                                  integrality, bounds, opts, no_std, fixed_infeasible, cfg)
    max_scheduled = int(round(-res1.fun))

    # ---- Phase 2: same count, cheapest plan ---------------------------------
    # Planning from scratch  -> cost is just the start slot (finish the day early).
    # Re-planning mid-day    -> plus a penalty for moving a DO away from the committed
    #                           plan, so the board does not churn for a marginal gain.
    lock = np.zeros(n_vars)
    for cols in flex_cols:
        for col in cols:
            lock[col] = 1
    A2 = lil_matrix((len(rows) + 1, n_vars))
    A2[:len(rows), :] = A
    A2[len(rows), :] = lock
    constraints2 = LinearConstraint(A2.tocsr(), lb + [max_scheduled], ub + [max_scheduled])

    c2 = np.zeros(n_vars)
    for ji, c, t in var_list:
        cost = weights.early * t
        base = (baseline or {}).get(prepared[ji]["id"])
        if base:
            c0, t0_min = base
            t0 = int(round((t0_min - cfg.window_start_min) / SLOT))
            if c != c0:
                cost += weights.channel_change
            shift = t - t0
            cost += weights.delay * shift if shift > 0 else weights.advance * (-shift)
        c2[var_index[(ji, c, t)]] = cost

    res2 = milp(c2, constraints=constraints2, integrality=integrality, bounds=bounds, options=opts)
    res = res2 if res2.success else res1

    chosen = {ji: (c, t) for ji, c, t in var_list if res.x[var_index[(ji, c, t)]] > 0.5}

    schedule, dropped = [], []
    for ji, j in enumerate(prepared):
        if j.get("forced_infeasible"):
            dropped.append({**j, "reason": j["infeasible_reason"]})
            continue
        if ji not in chosen:
            dropped.append({**j, "reason": "no feasible slot within capacity today"})
            continue
        c, t = chosen[ji]
        start = _slot_to_min(t, cfg)
        end = start + j["total_slots"] * SLOT
        schedule.append({
            "id": j["id"], "product": j["product"], "family": PRODUCT_FAMILY[j["product"]],
            "volume": j["volume"], "company": j.get("company", ""), "channel": c,
            "start": start, "end": end,
            "start_hhmm": min_to_hhmm(start), "end_hhmm": min_to_hhmm(end),
            "load_start": start + WEIGH_IN_LORRY, "load_end": end - WEIGH_OUT_LORRY,
            "weigh_in_res": (start, start + cfg.weigh_resource_min),
            "weigh_out_res": (end - WEIGH_OUT_LORRY, end - WEIGH_OUT_LORRY + cfg.weigh_resource_min),
            "is_fixed": j["is_fixed"], "requested_hhmm": j["requested_hhmm"],
            "is_pinned": j["is_pinned"], "pin_channel": j["pin_channel"],
            "is_locked": j["is_locked"], "locked_channel": j["locked_channel"],
            "locked_start": j["locked_start"],
            "earliest": j["earliest"], "earliest_hhmm": j["earliest_hhmm"],
            "bracket": j["bracket"], "margin": j.get("margin", 0) or 0,
            "std_source": j["std_source"],
            "duration_min": j["total_slots"] * SLOT,
            "status": j.get("status", ""),
            "plan_truck": j["plan_truck"],
        })

    for j in no_std:
        dropped.append({**j, "reason": "no standard-time data for this product/volume bracket"})

    schedule.sort(key=lambda r: (r["channel"], r["start"]))
    # HiGHS status 0 = proven optimal; 1 = stopped on the time/iteration limit, which still
    # yields a valid plan (all hard constraints hold) that just is not proven best.
    # Yusen-heavy days are much tighter to pack, so this is worth surfacing, not hiding.
    return {
        "error": None, "config": cfg, "status": str(res.message),
        "proven_optimal": int(getattr(res, "status", 0)) == 0,
        "schedule": schedule, "dropped": dropped,
        "max_scheduled_flex": max_scheduled, "n_flex_total": len(flex_cols),
        "n_fixed": sum(1 for j in prepared if j["is_fixed"]),
        "n_locked": sum(1 for j in prepared if j["is_locked"]),
        "n_vars": n_vars, "n_constraints": len(rows),
        "now_min": now_min,
        "validation": validate_schedule(schedule, cfg, now_min=now_min),
        "channel_summary": channel_summary(schedule, cfg),
        "baseline_diff": diff_vs_baseline(schedule, dropped, baseline) if baseline else [],
    }


def _why_infeasible(j, cfg, T):
    """A reason a planner can act on, not just 'infeasible'."""
    if j["is_locked"]:
        return "frozen job sits outside the model window"
    if j.get("pin_channel") and not j["eligible"]:
        return (f"ถูกล็อกไว้ที่ช่อง {j['pin_channel']} แต่ {j['product']} ลงช่องนั้นไม่ได้")
    if j["forced_t"] is not None and j["earliest_t"] is not None and j["forced_t"] < j["earliest_t"]:
        return (f"รถถึงเร็วสุด {j['earliest_hhmm']} แต่ถูกล็อกเวลาไว้ที่ {j['requested_hhmm']} "
                f"— มาไม่ทันเวลาที่ขอ")
    if j["earliest_t"] is not None and j["earliest_t"] + j["total_slots"] > T:
        return f"รถถึงเร็วสุด {j['earliest_hhmm']} — ช้าเกินกว่าจะโหลดจบก่อนปิดวัน"
    closed = [c for c in j["eligible"] if cfg.blackout_slots(c)]
    if closed and candidate_starts(j["total_slots"], cfg, forced_t=j["forced_t"],
                                   earliest_t=j["earliest_t"]):
        # it would have fitted if the bay had been open -> the blackout is the cause
        return ("ช่อง " + "/".join(closed) + " ปิดซ่อมทับช่วงที่ลงคิวนี้ได้ — "
                "ไม่เหลือเวลาว่างในช่องที่สินค้านี้ลงได้")
    if j["forced_t"] is not None:
        return "requested time outside operating window / inside a break"
    return "no feasible start time inside the operating window"


def _diagnose_conflict(prepared, job_to_cols, resource_rows, n_vars,
                       integrality, bounds, opts, no_std, fixed_infeasible, cfg):
    """Phase 1 infeasible => two or more mandatory DOs clash (dropping a flexible
    DO can never help, its row is '<= 1').  Relax each mandatory DO in turn and see
    which single relaxation restores feasibility -- those are the DOs the planner
    must re-time.  The system deliberately does not pick one itself.

    Frozen (already-loading) DOs are relaxed too: if a truck that is physically on a
    bay clashes with a hard deadline, the deadline is the thing that has to move."""
    culprits = []
    mand_idx = [ji for ji, j in enumerate(prepared)
                if (j["is_fixed"] or j["is_locked"]) and not j.get("forced_infeasible")
                and ji in job_to_cols]
    for relax_ji in mand_idx:
        trial_job_rows = []
        for ji, j in enumerate(prepared):
            cols = job_to_cols.get(ji)
            if not cols:
                continue
            mandatory = (j["is_fixed"] or j["is_locked"]) and ji != relax_ji
            trial_job_rows.append((cols, "==" if mandatory else "<=", 1))
        A_t, lb_t, ub_t = _build_matrix(trial_job_rows + resource_rows, n_vars)
        trial = milp(np.zeros(n_vars), constraints=LinearConstraint(A_t, lb_t, ub_t),
                     integrality=integrality, bounds=bounds, options=opts)
        if trial.success:
            culprits.append(prepared[relax_ji]["id"])

    return {
        "error": "hard_deadline_conflict", "config": cfg,
        "message": ("Two or more hard-deadline DOs cannot all be satisfied at once "
                    "(channel clash or shared camera/weighbridge clash). Relaxing any ONE "
                    "of the DOs listed below would make the rest feasible - a planner has to "
                    "decide which requested time to change. The scheduler will not pick for you."),
        "candidates_to_relax": culprits,
        "fixed_ids": [prepared[ji]["id"] for ji in mand_idx],
        "no_std": no_std, "fixed_infeasible": fixed_infeasible,
        "schedule": [], "dropped": [],
        "validation": [], "channel_summary": channel_summary([], cfg), "baseline_diff": [],
    }


# ----------------------------------------------------------------------------
# self-check + reporting
# ----------------------------------------------------------------------------
def _overlap(a, b):
    return a[0] < b[1] and b[0] < a[1]


def validate_schedule(schedule, cfg, now_min=None):
    """Independent re-check of the solver's answer before it is shown to anyone:
    no two trucks in one channel at once, no two trucks on the shared camera/
    weighbridge at once, nothing outside the window or inside a break, every hard
    deadline landing exactly on its requested time, every frozen job still where it
    actually is, and nobody starting before their truck can physically arrive."""
    problems = []
    for c in CHANNELS:
        rows = sorted([r for r in schedule if r["channel"] == c], key=lambda r: r["start"])
        for a, b in zip(rows, rows[1:]):
            if _overlap((a["start"], a["end"]), (b["start"], b["end"])):
                problems.append(f"Channel {c}: {a['id']} ({a['start_hhmm']}-{a['end_hhmm']}) "
                                f"overlaps {b['id']} ({b['start_hhmm']}-{b['end_hhmm']})")

    weigh = []
    for r in schedule:
        weigh.append((r["weigh_in_res"], r["id"], "weigh-in"))
        weigh.append((r["weigh_out_res"], r["id"], "weigh-out"))
    weigh.sort(key=lambda w: w[0][0])
    for (sa, ia, pa), (sb, ib, pb) in zip(weigh, weigh[1:]):
        if _overlap(sa, sb):
            problems.append(f"Weigh station: {ia} {pa} ({min_to_hhmm(sa[0])}-{min_to_hhmm(sa[1])}) "
                            f"overlaps {ib} {pb} ({min_to_hhmm(sb[0])}-{min_to_hhmm(sb[1])})")

    for r in schedule:
        if r.get("is_locked"):
            # a truck already on the bay is reality: it may legitimately sit across a
            # break or run past the nominal window, so only its position is checked
            if r["channel"] != r["locked_channel"] or r["start"] != r["locked_start"]:
                problems.append(f"{r['id']} is frozen on {r['locked_channel']} at "
                                f"{min_to_hhmm(r['locked_start'])} but was moved to "
                                f"{r['channel']} {r['start_hhmm']}")
            continue

        if r["start"] < cfg.window_start_min or r["end"] > cfg.window_end_min:
            problems.append(f"{r['id']} runs {r['start_hhmm']}-{r['end_hhmm']}, outside the "
                            f"{min_to_hhmm(cfg.window_start_min)}-{min_to_hhmm(cfg.window_end_min)} window")
        for bs, be in cfg.breaks_min:
            if _overlap((r["start"], r["end"]), (bs, be)):
                problems.append(f"{r['id']} ({r['start_hhmm']}-{r['end_hhmm']}) runs through the "
                                f"{min_to_hhmm(bs)}-{min_to_hhmm(be)} break")
        for bs, be, why in cfg.blackouts_for(r["channel"]):
            if _overlap((r["start"], r["end"]), (bs, be)):
                problems.append(f"{r['id']} ({r['start_hhmm']}-{r['end_hhmm']}) sits on channel "
                                f"{r['channel']} while it is closed "
                                f"{min_to_hhmm(bs)}-{min_to_hhmm(be)}"
                                + (f" ({why})" if why else ""))
        if r.get("is_pinned") and r["channel"] != r.get("pin_channel", r["channel"]):
            problems.append(f"{r['id']} was pinned to channel {r.get('pin_channel')} "
                            f"but was placed on {r['channel']}")
        if r["is_fixed"] and r["start_hhmm"] != r["requested_hhmm"]:
            problems.append(f"{r['id']} is a hard deadline for {r['requested_hhmm']} but was "
                            f"placed at {r['start_hhmm']}")
        if r.get("earliest") is not None and r["start"] < r["earliest"]:
            problems.append(f"{r['id']} starts {r['start_hhmm']} but its truck cannot arrive "
                            f"before {r['earliest_hhmm']}")
        if now_min is not None and r["start"] < now_min:
            problems.append(f"{r['id']} starts {r['start_hhmm']}, in the past "
                            f"(now is {min_to_hhmm(now_min)})")

    if cfg.truck_gap_min > 0:
        by_truck = {}
        for r in schedule:
            truck = (r.get("plan_truck") or "").strip()
            if truck:
                by_truck.setdefault(truck, []).append(r)
        for truck, rows in by_truck.items():
            rows = sorted(rows, key=lambda r: r["start"])
            for a, b in zip(rows, rows[1:]):
                gap = b["start"] - a["end"]
                if gap < cfg.truck_gap_min:
                    problems.append(f"รถ {truck}: {a['id']} จบ {a['end_hhmm']} แล้ว {b['id']} "
                                    f"เริ่ม {b['start_hhmm']} ห่างกันแค่ {gap} นาที "
                                    f"(ต้องเว้นอย่างน้อย {cfg.truck_gap_min} นาที)")
    return problems


def channel_summary(schedule, cfg):
    """Busy minutes / truck count / utilisation per channel.

    Utilisation is measured against the time that bay was actually open, so a
    channel that was down for maintenance half the day is not reported as lazy.
    """
    break_min = sum(be - bs for bs, be in cfg.breaks_min)
    open_all = (cfg.window_end_min - cfg.window_start_min) - break_min
    out = []
    for c in CHANNELS:
        rows = [r for r in schedule if r["channel"] == c]
        busy = sum(r["end"] - r["start"] for r in rows)
        down = cfg.blackout_minutes(c)
        available = max(0, open_all - down)
        out.append({
            "Channel": c, "Trucks": len(rows), "Busy (min)": busy,
            "Closed (min)": down, "Available (min)": available,
            "Utilisation %": round(100.0 * busy / available, 1) if available else 0.0,
            "First start": min((r["start_hhmm"] for r in rows), default="-"),
            "Last end": max((r["end_hhmm"] for r in rows), default="-"),
        })
    return out


def diff_vs_baseline(schedule, dropped, baseline):
    """What the re-optimisation actually changed, so a planner can see the churn
    at a glance instead of re-reading the whole board."""
    if not baseline:
        return []
    out = []
    for r in schedule:
        base = baseline.get(r["id"])
        if base is None:
            out.append({"DO No.": r["id"], "Change": "เพิ่มเข้าแผน", "From": "-",
                        "To": f"{r['channel']} {r['start_hhmm']}", "Shift (min)": ""})
            continue
        c0, t0 = base
        if c0 == r["channel"] and t0 == r["start"]:
            continue
        delta = r["start"] - t0
        kind = "ย้ายช่อง" if c0 != r["channel"] else ("เลื่อนออก" if delta > 0 else "เลื่อนเข้า")
        out.append({"DO No.": r["id"], "Change": kind,
                    "From": f"{c0} {min_to_hhmm(t0)}",
                    "To": f"{r['channel']} {r['start_hhmm']}",
                    "Shift (min)": f"{delta:+d}"})
    scheduled_ids = {r["id"] for r in schedule}
    for d in dropped:
        if d["id"] in baseline and d["id"] not in scheduled_ids:
            c0, t0 = baseline[d["id"]]
            out.append({"DO No.": d["id"], "Change": "หลุดจากแผน",
                        "From": f"{c0} {min_to_hhmm(t0)}", "To": "-", "Shift (min)": ""})
    order = {"หลุดจากแผน": 0, "ย้ายช่อง": 1, "เลื่อนออก": 2, "เลื่อนเข้า": 3, "เพิ่มเข้าแผน": 4}
    out.sort(key=lambda r: (order.get(r["Change"], 9), r["DO No."]))
    return out


def baseline_from_schedule(schedule) -> dict:
    """Turn a solved schedule into the {id: (channel, start_min)} map used as a baseline."""
    return {r["id"]: (r["channel"], r["start"]) for r in schedule}
