"""
Fabricated data for the test suite only.

These generators used to sit in ui_common / gps_eta and were reachable from the app
through "load sample" buttons. The production app takes its DO list from an Excel or
CSV upload and its GPS fixes from a real feed, so nothing here ships with it - a
planner must never be one mis-click away from a schedule built on invented trucks.
"""
import random

import pandas as pd

from gps_eta import offset_position
from scheduler_engine import LORRY_COMPANIES, YUSEN
from ui_common import COLS

SAMPLE_VOLUMES = {
    "MAA1": [14, 20, 22, 24], "MAA2": [14, 20, 22, 24], "MAA3": [14, 21, 23],
    "MMA1": [14, 21, 23, 25, 28], "MMA2": [14, 20, 22, 25, 29],
    "i-BMA": [12, 14], "n-BMA1": [13, 14], "n-BMA2": [12, 14],
}
SAMPLE_PRODUCT_POOL = [p for p, w in [("MMA2", 5), ("MMA1", 4), ("MAA1", 4), ("MAA2", 3),
                                      ("MAA3", 3), ("i-BMA", 2), ("n-BMA1", 2), ("n-BMA2", 2)]
                       for _ in range(w)]
SAMPLE_FIXED_TIMES = ["08:00", "10:30", "14:00", "16:30"]


def sample_df(n: int = 26, seed: int = 7) -> pd.DataFrame:
    """A believable day's worth of LORRY DOs, including a few hard deadlines.

    Carriers are drawn from LORRY_COMPANIES only, so this sample keeps producing the
    exact same plan it did before the Yusen table existed - which is what the
    regression fixture in tests/fixtures/lorry_baseline.json pins down.
    """
    rnd = random.Random(seed)
    rows = []
    for i in range(n):
        product = rnd.choice(SAMPLE_PRODUCT_POOL)
        rows.append({
            "DO No.": f"DO{1001 + i}",
            "Product": product,
            "Volume (ton)": float(rnd.choice(SAMPLE_VOLUMES[product])),
            "Transport Co.": rnd.choice(LORRY_COMPANIES),
            "Requested Time": SAMPLE_FIXED_TIMES[i] if i < len(SAMPLE_FIXED_TIMES) else "",
            "Margin (min)": rnd.choice([0, 0, 0, 5, 10]),
            "Plan truck": "",
        })
    return pd.DataFrame(rows, columns=COLS)


def sample_df_mixed(n: int = 26, seed: int = 7, yusen_share: float = 0.35) -> pd.DataFrame:
    """The same day with part of the fleet handed to Yusen (ISO Tank), so the effect of
    the second standard-time table is visible."""
    df = sample_df(n, seed)
    rnd = random.Random(seed + 1)
    df["Transport Co."] = [YUSEN if rnd.random() < yusen_share else c
                           for c in df["Transport Co."]]
    return df


def sample_gps(pending, now_min: int, cfg, seed: int = 5,
               late_share: float = 0.25, near_share: float = 0.2):
    """Plausible GPS fixes for a mid-day scenario.

    `pending` is [{id, planned_start}].  Most trucks are roughly on time, some are
    already waiting in the yard (the ones that can fill a gap), and some are far
    enough away that they will miss their slot - which is exactly the situation
    the re-planner exists for.
    """
    rnd = random.Random(seed)
    rows = []
    for item in pending:
        planned = item.get("planned_start")
        slack = 30 if planned is None else max(-60, planned - now_min)
        roll = rnd.random()
        if roll < near_share:
            travel = rnd.uniform(0, 12)            # in the yard or a few minutes out
        elif roll < near_share + late_share:
            travel = slack + rnd.uniform(35, 100)  # will not make its slot
        else:
            travel = max(3.0, slack - rnd.uniform(0, 25))  # roughly on time
        travel = max(0.5, travel)
        distance = travel / 60.0 * cfg.avg_speed_kmh / cfg.detour_factor
        if roll < near_share and rnd.random() < 0.5:
            distance = rnd.uniform(0.05, cfg.at_plant_radius_km * 0.8)  # parked at the gate
        lat, lon = offset_position(distance, rnd.uniform(0, 360), cfg)
        rows.append({
            "id": item["id"], "lat": lat, "lon": lon,
            "last_seen_min": now_min - rnd.choice([0, 0, 1, 2, 3, 5, 25]),
        })
    return rows


def sample_gps_df(pending, now_min, cfg, **kw) -> pd.DataFrame:
    """The same fixes shaped like the app's GPS table."""
    from scheduler_engine import min_to_hhmm
    from ui_common import GPS_COLS
    rows = sample_gps(pending, now_min, cfg, **kw)
    return pd.DataFrame([{"DO No.": r["id"], "Lat": r["lat"], "Lon": r["lon"],
                          "Last seen": min_to_hhmm(r["last_seen_min"])} for r in rows],
                        columns=GPS_COLS)
