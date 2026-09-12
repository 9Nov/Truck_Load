"""Manual swap workflow: who is late, who may replace them, and what the
planner's decision does to the solver.

Run:  py tests\\test_swap.py
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import swap_planner as sp  # noqa: E402
from scheduler_engine import (  # noqa: E402
    SchedulerConfig,
    baseline_from_schedule,
    build_and_solve,
    hhmm_to_min,
    min_to_hhmm,
)

CFG = SchedulerConfig()
NOW = hhmm_to_min("09:00")


def row(do_id, product, channel, start, eta, company="SV", dur=60):
    return {"id": do_id, "product": product, "company": company,
            "planned_channel": channel, "planned_start": start,
            "planned_end": start + dur, "eta": eta}


# ---------------------------------------------------------------------------
print("### 1. who is late — and only past the tolerance")
policy = sp.SwapPolicy()   # 15 late / 30 lead / 60 notice
pending = [
    row("T1", "MMA1", "C", hhmm_to_min("10:00"), hhmm_to_min("09:50")),  # early
    row("T2", "MMA1", "C", hhmm_to_min("11:00"), hhmm_to_min("11:10")),  # 10 late -> tolerated
    row("T3", "MMA1", "C", hhmm_to_min("12:00"), hhmm_to_min("12:35")),  # 35 late -> act
    row("T4", "MMA1", "C", hhmm_to_min("13:00"), None),                  # no signal
]
board = sp.inbound_board(pending, NOW, policy)
for b in board:
    print(f"  {b['id']} นัด {min_to_hhmm(b['planned_start'])} "
          f"eta {min_to_hhmm(b['eta']) if b['eta'] else '-':>5} -> {b['status']} "
          f"({b['late_by']})")
assert [b["status"] for b in board] == [sp.ON_TIME, sp.SLIGHTLY_LATE, sp.LATE, sp.NO_SIGNAL]

# ---------------------------------------------------------------------------
print("\n### 2. a truck already at the plant can be called in without notice")
at_plant = row("P1", "MMA1", "C", hhmm_to_min("15:00"), hhmm_to_min("08:40"))  # arrived 08:40
on_road = row("P2", "MMA1", "C", hhmm_to_min("15:30"), hhmm_to_min("09:20"))   # still driving
b = sp.inbound_board([at_plant, on_road], NOW, policy)
ready = {x["id"]: (x["ready"], x["at_plant"]) for x in b}
print(f"  P1 (ถึงแล้ว 08:40) พร้อม {min_to_hhmm(ready['P1'][0])} at_plant={ready['P1'][1]}")
print(f"  P2 (ETA 09:20)     พร้อม {min_to_hhmm(ready['P2'][0])} at_plant={ready['P2'][1]}")
assert ready["P1"] == (hhmm_to_min("08:40"), True), "a waiting truck needs no notice"
assert ready["P2"] == (NOW + policy.notice_min, False), "a driving truck needs the notice window"

# ---------------------------------------------------------------------------
print("\n### 3. candidates must fit the bay, the clock and the lead time")
target = row("LATE1", "MMA1", "C", hhmm_to_min("12:00"), hhmm_to_min("12:40"))
others = [
    row("OK1", "MMA1", "C", hhmm_to_min("14:00"), hhmm_to_min("09:00")),   # at plant, same bay
    row("OK2", "n-BMA1", "C", hhmm_to_min("15:00"), hhmm_to_min("10:00")),  # driving, fits C
    row("BAD_BAY", "MAA3", "B", hhmm_to_min("13:00"), hhmm_to_min("09:00")),  # MAA3 is B-only
    row("BAD_LATE", "MMA1", "C", hhmm_to_min("16:00"), hhmm_to_min("11:45")),  # too tight
    row("BAD_BLIND", "MMA1", "C", hhmm_to_min("17:00"), None),              # no position
]
board = sp.inbound_board([target] + others, NOW, policy)
tgt = next(b for b in board if b["id"] == "LATE1")
cands = sp.find_candidates(tgt, board, NOW, policy)
print("  candidates:", [(c["id"], min_to_hhmm(c["ready"]), c["slack"]) for c in cands])
assert [c["id"] for c in cands] == ["OK1", "OK2"], [c["id"] for c in cands]
assert cands[0]["at_plant"] and cands[0]["same_channel"]
for c in cands:
    assert c["ready"] + policy.lead_min <= tgt["planned_start"]
print("  MAA3 ถูกตัดเพราะลงช่อง C ไม่ได้ · BAD_LATE ถูกตัดเพราะพร้อมช้าเกิน · "
      "BAD_BLIND ถูกตัดเพราะไม่รู้ตำแหน่ง ✓")

note = sp.candidate_note(tgt, cands[0], policy, NOW)
print("  note:", note)
assert "จอดรออยู่หน้าโรงงานแล้ว" in note

# ---------------------------------------------------------------------------
print("\n### 4. relaxing the rules surfaces more candidates, never fewer")
loose = sp.SwapPolicy(late_threshold_min=15, lead_min=0, notice_min=0)
board2 = sp.inbound_board([target] + others, NOW, loose)
more = sp.find_candidates(next(b for b in board2 if b["id"] == "LATE1"), board2, NOW, loose)
print("  strict:", [c["id"] for c in cands], "| loose:", [c["id"] for c in more])
assert set(c["id"] for c in cands) <= set(c["id"] for c in more)

# ---------------------------------------------------------------------------
print("\n### 5. a decision becomes a solver constraint — and the board stays legal")
jobs = [
    {"id": "LATE1", "product": "MMA1", "volume": 14, "company": "SV",
     "requested": None, "margin": 0, "earliest": hhmm_to_min("12:40")},
    {"id": "OK1", "product": "MMA1", "volume": 14, "company": "SV",
     "requested": None, "margin": 0, "earliest": hhmm_to_min("09:00")},
    {"id": "OTHER", "product": "MMA1", "volume": 14, "company": "SV",
     "requested": None, "margin": 0, "earliest": hhmm_to_min("09:00")},
]
plan = build_and_solve([dict(j) for j in jobs], config=CFG, now_min=NOW)
baseline = baseline_from_schedule(plan["schedule"])
# 14:00 is a real slot: a 12:00 start would run into the lunch break, and the
# solver would (correctly) refuse the pin rather than quietly shifting it
decision = [{"target_id": "LATE1", "replacement_id": "OK1", "channel": "C",
             "start": hhmm_to_min("14:00"), "target_product": "MMA1",
             "rep_product": "MMA1", "note": ""}]
jobs2, applied = sp.apply_decisions([dict(j) for j in jobs], decision,
                                    plan["schedule"], policy, NOW)
print("  applied:", applied[0]["result"], "|", applied[0]["why"])
pinned = next(j for j in jobs2 if j["id"] == "OK1")
assert pinned["pin_channel"] == "C" and pinned["requested"] == hhmm_to_min("14:00")

res = build_and_solve(jobs2, config=CFG, now_min=NOW, baseline=baseline)
assert not res.get("error"), res.get("error")
assert not res["validation"], res["validation"]
placed = {r["id"]: r for r in res["schedule"]}
for r in res["schedule"]:
    print(f"  {r['channel']} {r['start_hhmm']}-{r['end_hhmm']} {r['id']}"
          f"{' PINNED' if r.get('is_pinned') else ''}")
assert placed["OK1"]["channel"] == "C" and placed["OK1"]["start_hhmm"] == "14:00", placed["OK1"]
assert placed["OK1"]["is_pinned"]
assert placed["LATE1"]["start"] >= hhmm_to_min("12:40"), "the bumped truck keeps its ETA"
print("  self-check:", res["validation"] or "NONE — กระดานยังถูกกติกาทุกข้อ")

# ---------------------------------------------------------------------------
print("\n### 6. 'ต่อท้ายคิว' policy pushes the bumped truck behind that channel")
tail_policy = sp.SwapPolicy(bumped=sp.BUMPED_TAIL)
tail = sp.channel_tail(plan["schedule"], "C")
jobs3, _ = sp.apply_decisions([dict(j) for j in jobs], decision, plan["schedule"],
                              tail_policy, NOW)
bumped = next(j for j in jobs3 if j["id"] == "LATE1")
print(f"  ท้ายคิวช่อง C = {min_to_hhmm(tail)} · LATE1 earliest -> {min_to_hhmm(bumped['earliest'])}")
assert bumped["earliest"] >= tail
res3 = build_and_solve(jobs3, config=CFG, now_min=NOW, baseline=baseline)
assert not res3["validation"], res3["validation"]
assert next(r for r in res3["schedule"] if r["id"] == "LATE1")["start"] >= tail

# ---------------------------------------------------------------------------
print("\n### 7. a pin onto a bay the product cannot use is refused, not silently moved")
bad = [{"id": "X1", "product": "MAA3", "volume": 14, "company": "SV",
        "requested": hhmm_to_min("10:00"), "margin": 0, "pin_channel": "A"}]
res4 = build_and_solve(bad, config=CFG)
dropped = res4["dropped"][0]
print("  ผิดช่อง :", dropped["reason"])

across_break = [{"id": "X2", "product": "MMA1", "volume": 14, "company": "SV",
                 "requested": hhmm_to_min("12:00"), "margin": 0, "pin_channel": "C"}]
res5 = build_and_solve(across_break, config=CFG)
print("  คร่อมพัก:", res5["dropped"][0]["reason"])
assert res5["dropped"], "a pin that runs into a break must not be silently shifted"
assert "ลงช่องนั้นไม่ได้" in dropped["reason"]

print("\nALL SWAP TESTS PASSED")
