"""
Manual swap workflow: which trucks will miss their slot, who could take it
instead, and what the planner's decision means to the solver.

Design rule: a decision the planner makes here is NOT applied by shuffling rows
in the UI. It is turned into a constraint (`pin_channel` + `requested`) and fed
back into the same MILP, so the resulting board still satisfies every hard
constraint - one truck per bay, one truck on the camera/weighbridge, nobody
starting before their truck can arrive - and still passes the self-check. The
planner chooses; the solver keeps the plan legal.

No Streamlit in here on purpose: the rules below are the part worth unit-testing.
"""
from dataclasses import dataclass

from scheduler_engine import CHANNEL_ELIGIBILITY, min_to_hhmm

# status of a truck that is still waiting to come in
ON_TIME = "on_time"      # ETA is at or before its slot
SLIGHTLY_LATE = "slight"  # late, but inside the tolerance - not worth disturbing the board
LATE = "late"            # late beyond tolerance - offer a replacement
NO_SIGNAL = "no_signal"  # no GPS fix and no manual ETA - cannot promise anything

BUMPED_ETA = "eta"    # the truck that lost its slot re-enters wherever its real ETA allows
BUMPED_TAIL = "tail"  # ... or goes to the back of that channel's queue (the old rule)


@dataclass
class SwapPolicy:
    """The floor rules. All of them are judgement calls, so all of them are settings."""
    late_threshold_min: int = 15  # only warn when a truck will miss its slot by more than this
    lead_min: int = 30            # a replacement must be in place this long before the slot
    notice_min: int = 60          # a driver who is still on the road needs this much warning
    bumped: str = BUMPED_ETA      # where the truck that lost its slot goes

    def describe(self) -> str:
        return (f"เตือนเมื่อจะสายเกิน {self.late_threshold_min} นาที · "
                f"คันแทนต้องพร้อมก่อนคิวอย่างน้อย {self.lead_min} นาที · "
                f"คันที่ยังอยู่บนถนนต้องแจ้งคนขับล่วงหน้า {self.notice_min} นาที")


def _ready_time(eta, now_min, policy):
    """The earliest this truck could realistically be called into the bay.

    A truck already waiting at the plant can be called forward immediately; one
    still on the road needs notice, counted from now - not from its ETA.
    """
    if eta is None:
        return None, False
    at_plant = eta <= now_min
    return (eta if at_plant else max(eta, now_min + policy.notice_min)), at_plant


def classify(row, eta, now_min, policy) -> tuple:
    """(status, late_by_min) for one waiting truck against its planned slot."""
    if eta is None:
        return NO_SIGNAL, None
    late_by = eta - row["planned_start"]
    if late_by <= 0:
        return ON_TIME, late_by
    if late_by <= policy.late_threshold_min:
        return SLIGHTLY_LATE, late_by
    return LATE, late_by


def inbound_board(pending, now_min, policy) -> list:
    """Every truck still to come in, soonest slot first, with its verdict.

    `pending`: [{id, product, company, planned_channel, planned_start, planned_end, eta}]
    """
    out = []
    for row in pending:
        status, late_by = classify(row, row.get("eta"), now_min, policy)
        ready, at_plant = _ready_time(row.get("eta"), now_min, policy)
        out.append({**row, "status": status, "late_by": late_by,
                    "ready": ready, "at_plant": at_plant})
    out.sort(key=lambda r: (r["planned_start"], r["id"]))
    return out


def find_candidates(target, board, now_min, policy) -> list:
    """Trucks that could take `target`'s slot instead of it.

    A candidate must (a) be allowed on that bay by the product's channel
    eligibility, (b) have a known position, and (c) be able to stand in the bay
    at least `lead_min` before the slot starts, counting the driver's notice.
    Ranked by how early it could actually start.
    """
    slot_start = target["planned_start"]
    channel = target["planned_channel"]
    out = []
    for row in board:
        if row["id"] == target["id"]:
            continue
        if channel not in CHANNEL_ELIGIBILITY.get(row["product"], []):
            continue
        ready, at_plant = row["ready"], row["at_plant"]
        if ready is None or ready + policy.lead_min > slot_start:
            continue
        out.append({
            "id": row["id"], "product": row["product"], "company": row.get("company", ""),
            "eta": row.get("eta"), "ready": ready, "at_plant": at_plant,
            "planned_channel": row["planned_channel"], "planned_start": row["planned_start"],
            "same_channel": row["planned_channel"] == channel,
            "pulled_forward": row["planned_start"] - slot_start,
            "slack": slot_start - ready,
        })
    # A truck whose own slot is LATER than this one is being pulled forward - that fills
    # the gap and costs nobody anything. One planned EARLIER would be pushed back to take
    # this slot, which is a worse trade, so it is offered last and labelled as such.
    out.sort(key=lambda c: (c["pulled_forward"] <= 0, c["ready"], not c["same_channel"], c["id"]))
    return out


def candidate_note(target, candidate, policy, now_min) -> str:
    """One line the planner can read out loud before committing."""
    where = ("จอดรออยู่หน้าโรงงานแล้ว" if candidate["at_plant"]
             else f"ยังอยู่บนถนน · แจ้งคนขับล่วงหน้า {policy.notice_min} นาที "
                  f"เรียกเข้าได้เร็วสุด {min_to_hhmm(candidate['ready'])}")
    swap = ("สินค้าเดิมของช่องนี้" if candidate["product"] == target["product"]
            else f"สลับสินค้าในช่อง {target['planned_channel']}: "
                 f"{target['product']} → {candidate['product']}")
    return (f"{where} · {swap} · พร้อมก่อนคิวเดิม {candidate['slack']} นาที "
            f"(เกณฑ์ {policy.lead_min} นาที)")


def channel_tail(baseline_schedule, channel) -> int:
    """When the last planned job on a channel finishes - the 'back of the queue'."""
    ends = [r["end"] for r in baseline_schedule if r["channel"] == channel]
    return max(ends) if ends else None


def apply_decisions(jobs, decisions, baseline_schedule, policy, now_min):
    """Turn accepted swaps into solver input.

    The chosen replacement is pinned to the bay and start time the planner agreed
    to; the truck it displaced keeps its release time and is re-placed by the
    solver (or sent to the back of that channel's queue, if that policy is set).
    Everything else in the day is still optimised freely, which is what keeps the
    board feasible after a manual decision.

    Returns (jobs, applied) - `applied` describes what actually changed.
    """
    by_id = {j["id"]: j for j in jobs}
    applied = []
    for d in decisions:
        rep = by_id.get(d["replacement_id"])
        bumped = by_id.get(d["target_id"])
        if rep is None or bumped is None:
            applied.append({**d, "result": "skipped", "why": "DO ไม่อยู่ในรายการที่จัดรอบนี้"})
            continue

        rep["pin_channel"] = d["channel"]
        rep["requested"] = d["start"]
        note = f"ล็อก {rep['id']} ลงช่อง {d['channel']} เวลา {min_to_hhmm(d['start'])}"

        if policy.bumped == BUMPED_TAIL:
            tail = channel_tail(baseline_schedule, d["channel"])
            if tail is not None:
                current = bumped.get("earliest")
                bumped["earliest"] = tail if current is None else max(current, tail)
                note += f" · {bumped['id']} ไปต่อท้ายคิวช่อง {d['channel']} ({min_to_hhmm(tail)})"
        else:
            note += f" · {bumped['id']} เสียบกลับตาม ETA จริง"

        if bumped.get("requested") is not None:
            # it no longer owns that minute - otherwise the two would fight for it
            bumped["requested"] = None
            note += " (ปลดเวลาที่ล็อกไว้เดิมของคันที่ถูกแทน)"

        applied.append({**d, "result": "applied", "why": note})
    return jobs, applied
