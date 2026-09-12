"""
Turns truck GPS fixes into the one number the scheduler actually needs:
a *release time* - the earliest minute a given DO's truck could realistically
start its weigh-in at the plant.

Deliberately simple and transparent (great-circle distance x a detour factor,
divided by an average road speed) rather than a routing API: the planner can see
every assumption, and every one of them is configurable.  Swap eta_for() for a
real ETA feed later and nothing downstream changes - the scheduler only ever
sees `earliest`.

No pandas / no Streamlit in here on purpose: this module is pure arithmetic so
it can be unit-tested and reused from a batch job.
"""
import math
from dataclasses import dataclass
from typing import Optional

EARTH_RADIUS_KM = 6371.0088
KM_PER_DEG_LAT = 111.32

# A rough placeholder near the Map Ta Phut estate, NOT the real weighbridge - this
# is public source code, so the real coordinates never live here. The actual site is
# supplied at deploy time via Streamlit secrets (see resolve_plant_latlon below) or
# typed into the sidebar; every ETA is measured from this point, so it is the one
# number in the system that absolutely has to be set correctly before trusting an ETA.
PLACEHOLDER_PLANT_LATLON = (12.6800, 101.1500)


def resolve_plant_latlon(secrets=None) -> tuple:
    """The real plant coordinates, if supplied out-of-band (Streamlit secrets, an
    env-backed mapping, ...), else the placeholder.

    `secrets` is anything that supports `secrets["plant"]["lat"/"lon"]` - a plain
    dict, st.secrets, etc. Never raises: a missing or malformed secret just falls
    back, since a broken deploy-time config should degrade, not crash the app.
    """
    try:
        plant = secrets["plant"]
        lat, lon = float(plant["lat"]), float(plant["lon"])
        if -90 <= lat <= 90 and -180 <= lon <= 180:
            return (lat, lon)
    except Exception:
        pass
    return PLACEHOLDER_PLANT_LATLON


def is_placeholder_plant(cfg: "TravelConfig", tol: float = 1e-6) -> bool:
    """True while the plant is still on the shipped placeholder coordinates -
    i.e. deploy-time secrets were never configured and nobody typed the real
    site into the sidebar either."""
    return (abs(cfg.plant_lat - PLACEHOLDER_PLANT_LATLON[0]) < tol
            and abs(cfg.plant_lon - PLACEHOLDER_PLANT_LATLON[1]) < tol)


@dataclass
class TravelConfig:
    """Everything about turning a GPS fix into an ETA. All of it is a guess you
    should tune against real trip data - hence all of it is exposed in the UI."""
    # DEFAULT IS A PLACEHOLDER - see PLACEHOLDER_PLANT_LATLON above.
    plant_lat: float = PLACEHOLDER_PLANT_LATLON[0]
    plant_lon: float = PLACEHOLDER_PLANT_LATLON[1]
    avg_speed_kmh: float = 45.0      # door-to-door average, not free-flow speed
    detour_factor: float = 1.35      # road distance / straight-line distance
    gate_buffer_min: int = 10        # gate check + queueing before weigh-in can start
    at_plant_radius_km: float = 1.0  # inside this, treat the truck as already waiting
    near_eta_min: int = 30           # "close enough to fill a gap"
    enroute_eta_min: int = 90        # beyond this it is a long way out
    stale_after_min: int = 20        # a fix older than this is not trustworthy

    def validate(self) -> list:
        problems = []
        if not -90 <= self.plant_lat <= 90 or not -180 <= self.plant_lon <= 180:
            problems.append("Plant coordinates are out of range.")
        if self.avg_speed_kmh <= 0:
            problems.append("Average speed must be greater than 0.")
        if self.detour_factor < 1:
            problems.append("Detour factor should be at least 1.0 (road ≥ straight line).")
        if self.gate_buffer_min < 0:
            problems.append("Gate buffer cannot be negative.")
        return problems


STATUS_AT_PLANT = "🅿️ ถึงโรงงานแล้ว"
STATUS_NEAR = "🟢 ใกล้ — เสียบคิวได้"
STATUS_ENROUTE = "🟡 กำลังมา"
STATUS_FAR = "🔴 ไกล"
STATUS_UNKNOWN = "⚪ ไม่มีสัญญาณ"


def haversine_km(lat1, lon1, lat2, lon2) -> float:
    """Great-circle distance in km."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = p2 - p1
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * EARTH_RADIUS_KM * math.asin(math.sqrt(a))


def road_km(straight_km: float, cfg: TravelConfig) -> float:
    return straight_km * cfg.detour_factor


def travel_minutes(straight_km: float, cfg: TravelConfig) -> float:
    return road_km(straight_km, cfg) / cfg.avg_speed_kmh * 60.0


def classify(distance_km: float, travel_min: float, cfg: TravelConfig) -> str:
    if distance_km <= cfg.at_plant_radius_km:
        return STATUS_AT_PLANT
    if travel_min <= cfg.near_eta_min:
        return STATUS_NEAR
    if travel_min <= cfg.enroute_eta_min:
        return STATUS_ENROUTE
    return STATUS_FAR


def _round_up(minutes: float, step: int = 5) -> int:
    return int(math.ceil(minutes / step) * step)


def eta_for(lat, lon, now_min: int, cfg: TravelConfig,
            last_seen_min: Optional[int] = None) -> dict:
    """One truck -> {distance_km, travel_min, eta_min, eta_hhmm, status, stale, age_min}.

    `eta_min` is minutes-from-midnight, rounded UP to the 5-minute grid the solver
    works on, so a rounding error can never make the plan optimistic.
    """
    if lat is None or lon is None:
        return {"distance_km": None, "travel_min": None, "eta_min": None,
                "status": STATUS_UNKNOWN, "stale": True, "age_min": None}

    distance = haversine_km(lat, lon, cfg.plant_lat, cfg.plant_lon)
    travel = travel_minutes(distance, cfg)
    status = classify(distance, travel, cfg)

    if status == STATUS_AT_PLANT:
        eta = float(now_min)  # already in the yard, it can be called forward now
    else:
        eta = now_min + travel + cfg.gate_buffer_min

    age = None if last_seen_min is None else max(0, now_min - last_seen_min)
    stale = age is not None and age > cfg.stale_after_min

    return {"distance_km": round(distance, 1), "road_km": round(road_km(distance, cfg), 1),
            "travel_min": int(round(travel)), "eta_min": _round_up(eta),
            "status": status, "stale": stale, "age_min": age}


def fleet_etas(gps_rows, now_min: int, cfg: TravelConfig) -> dict:
    """[{id, lat, lon, last_seen_min}] -> {id: eta_for(...)}"""
    out = {}
    for row in gps_rows:
        do_id = str(row.get("id") or "").strip()
        if not do_id:
            continue
        out[do_id] = eta_for(row.get("lat"), row.get("lon"), now_min, cfg,
                             row.get("last_seen_min"))
    return out


def offset_position(distance_km: float, bearing_deg: float, cfg: TravelConfig):
    """A lat/lon that sits `distance_km` from the plant on the given bearing."""
    bearing = math.radians(bearing_deg)
    dlat = distance_km * math.cos(bearing) / KM_PER_DEG_LAT
    dlon = distance_km * math.sin(bearing) / (KM_PER_DEG_LAT *
                                              max(0.1, math.cos(math.radians(cfg.plant_lat))))
    return round(cfg.plant_lat + dlat, 5), round(cfg.plant_lon + dlon, 5)
