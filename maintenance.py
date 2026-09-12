"""
Maintenance windows that can run across several days.

The scheduler plans one day at a time and thinks in minutes-from-midnight, so a
multi-day shutdown has to be projected onto the day being planned before it can
become a constraint. That projection is the whole job of this module:

    12 Sep 22:00  ->  14 Sep 16:00   on channel B

    planning 12 Sep  ->  B closed 22:00 - end of day
    planning 13 Sep  ->  B closed all day
    planning 14 Sep  ->  B closed start of day - 16:00
    planning 15 Sep  ->  B open, nothing to apply

Pure date arithmetic - no Streamlit, no solver - so the edge cases above are
unit-testable on their own.
"""
import datetime as dt
from dataclasses import dataclass, field
from typing import Optional

from scheduler_engine import CHANNELS, min_to_hhmm


@dataclass
class Maintenance:
    """One shutdown, as the planner entered it."""
    channel: str
    start_date: dt.date
    start_time: dt.time
    end_date: dt.date
    end_time: dt.time
    reason: str = ""

    # ---- derived ----
    @property
    def start_at(self) -> dt.datetime:
        return dt.datetime.combine(self.start_date, self.start_time)

    @property
    def end_at(self) -> dt.datetime:
        return dt.datetime.combine(self.end_date, self.end_time)

    @property
    def days(self) -> int:
        """How many calendar days this shutdown touches."""
        return (self.end_date - self.start_date).days + 1

    def covers(self, day: dt.date) -> bool:
        return self.start_date <= day <= self.end_date

    def errors(self) -> list:
        out = []
        if self.channel not in CHANNELS:
            out.append("ต้องเลือกช่องเป็น " + " / ".join(CHANNELS))
        if self.end_at <= self.start_at:
            out.append("เวลาสิ้นสุดต้องอยู่หลังเวลาเริ่ม")
        return out

    def describe(self) -> str:
        if self.start_date == self.end_date:
            return (f"{self.start_date:%d/%m} "
                    f"{self.start_time:%H:%M}–{self.end_time:%H:%M}")
        return (f"{self.start_date:%d/%m} {self.start_time:%H:%M} → "
                f"{self.end_date:%d/%m} {self.end_time:%H:%M}  ({self.days} วัน)")


def window_on(item: Maintenance, day: dt.date, window_start_min: int,
              window_end_min: int) -> Optional[tuple]:
    """The (start_min, end_min) this shutdown blocks on `day`, or None if it does
    not touch that day at all. A day in the middle of a multi-day shutdown is
    closed for the whole operating window."""
    if not item.covers(day):
        return None
    start = (item.start_time.hour * 60 + item.start_time.minute
             if day == item.start_date else window_start_min)
    end = (item.end_time.hour * 60 + item.end_time.minute
           if day == item.end_date else window_end_min)
    start = max(start, window_start_min)
    end = min(end, window_end_min)
    if end <= start:
        return None          # it only covers hours the plant is closed anyway
    return start, end


def to_blackouts(items, day: dt.date, window_start_min: int, window_end_min: int) -> list:
    """Project every shutdown onto one planning day, in the shape SchedulerConfig
    takes: [(channel, start_min, end_min, reason)]."""
    out = []
    for item in items:
        span = window_on(item, day, window_start_min, window_end_min)
        if span is None:
            continue
        out.append((item.channel, span[0], span[1], item.reason))
    return sorted(out)


def active_on(items, day: dt.date) -> list:
    return [i for i in items if i.covers(day)]


def summarise(items, day: dt.date, window_start_min: int, window_end_min: int) -> str:
    """One line for the sidebar: which bays are down today and for how long."""
    blackouts = to_blackouts(items, day, window_start_min, window_end_min)
    if not blackouts:
        return ""
    per_channel = {}
    for ch, s, e, _ in blackouts:
        per_channel[ch] = per_channel.get(ch, 0) + (e - s)
    return " · ".join(f"{ch} {mins} น." for ch, mins in sorted(per_channel.items()))


def from_rows(rows) -> list:
    """Rebuild Maintenance objects from plain dicts (what session state stores)."""
    out = []
    for r in rows:
        out.append(Maintenance(
            channel=r["channel"], start_date=r["start_date"], start_time=r["start_time"],
            end_date=r["end_date"], end_time=r["end_time"], reason=r.get("reason", "")))
    return out


def to_rows(items) -> list:
    return [{"channel": i.channel, "start_date": i.start_date, "start_time": i.start_time,
             "end_date": i.end_date, "end_time": i.end_time, "reason": i.reason}
            for i in items]
