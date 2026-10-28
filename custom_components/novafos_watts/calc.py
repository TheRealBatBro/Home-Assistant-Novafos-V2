"""Pure calculations over the stored hourly series (no Home Assistant imports)."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, tzinfo


RECENT_HOURS = 48


@dataclass
class Summary:
    consumption: float = 0.0
    last_full_day: float | None = None
    last_full_day_date: date | None = None
    last_7_days: float | None = None
    this_month: float = 0.0
    this_year: float = 0.0
    data_until: datetime | None = None
    daily: dict[date, float] = field(default_factory=dict)  # local date -> volume
    recent_hours: list[tuple[datetime, float]] = field(default_factory=list)  # newest 48 h
    # (interval start, cumulative sum through the end of that hour), oldest first
    sums: list[tuple[datetime, float]] = field(default_factory=list)


def parse_utc(stamp: str) -> datetime:
    return datetime.fromisoformat(stamp.replace("Z", "+00:00"))


def summarize(hourly: dict[str, float], now: datetime, tz: tzinfo, factor: float) -> Summary:
    """Turn {interval start (UTC iso): raw volume} into the sensor values."""
    series = sorted((parse_utc(k), v * factor) for k, v in hourly.items())
    if not series:
        return Summary()

    running = 0.0
    sums: list[tuple[datetime, float]] = []
    daily: dict[date, float] = {}
    for start, value in series:
        running += value
        sums.append((start, running))
        day = start.astimezone(tz).date()
        daily[day] = daily.get(day, 0.0) + value

    data_until = series[-1][0] + timedelta(hours=1)
    # A day is complete once data reaches its end, so the newest complete day is
    # the one before the local date of data_until (also true at exact midnight).
    complete = data_until.astimezone(tz).date() - timedelta(days=1)
    local_now = now.astimezone(tz)
    return Summary(
        consumption=running,
        last_full_day=daily.get(complete),
        last_full_day_date=complete if complete in daily else None,
        last_7_days=sum(daily.get(complete - timedelta(days=k), 0.0) for k in range(7)),
        this_month=sum(
            v for d, v in daily.items() if (d.year, d.month) == (local_now.year, local_now.month)
        ),
        this_year=sum(v for d, v in daily.items() if d.year == local_now.year),
        data_until=data_until,
        daily=daily,
        recent_hours=series[-RECENT_HOURS:],
        sums=sums,
    )
