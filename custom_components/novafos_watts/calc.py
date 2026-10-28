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


def hour_of(moment: datetime) -> datetime:
    return moment.replace(minute=0, second=0, microsecond=0)


@dataclass
class Rebase:
    frame_start: datetime  # first short-term row of the recorder's current series
    offset: float  # state - sum in that series; added to every sum from frame_start on
    rows: list[tuple[datetime, float]]  # (hour start, sum) to write as hourly statistics


def rebase_entity_statistics(
    true_sums: list[tuple[datetime, float]],
    hours: list[datetime],
    short: list[tuple[datetime, float, float]],
) -> Rebase | None:
    """Make the sensor's statistics one non-decreasing series that the recorder continues.

    Versions 0.3.0-0.4.1 imported hourly sums (cumulative since the first stored
    hour) into the sensor's own statistics while the recorder compiled the same
    statistic from the sensor's state. The recorder continues from its newest
    short-term row, which imports never touch, so its series started over at 0.

    true_sums is the hour-placed cumulative series, hours the existing hourly rows
    and short the short-term rows as (start, state, sum), all oldest first. The
    state is always the same cumulative total, so the fix is sum = state: the
    recorder's series moves up by `offset`, and older hours take the true sums,
    capped at the state when the recorder's series began so no hour goes down.
    Returns None without short-term rows (nothing for the recorder to continue).
    """
    if not short:
        return None
    offset = short[-1][1] - short[-1][2]
    # The current series is the newest run of rows that share this offset.
    first = len(short) - 1
    while first > 0 and abs(short[first - 1][1] - short[first - 1][2] - offset) < 1e-6:
        first -= 1
    frame_start, cap, _ = short[first]
    frame_hour = hour_of(frame_start)

    in_frame: dict[datetime, float] = {}
    for start, state, _ in short[first:]:
        in_frame[hour_of(start)] = state  # the hour's last row wins, as in the recorder
    before = {start: total for start, total in true_sums if start < frame_hour}

    rows: list[tuple[datetime, float]] = []
    value = 0.0
    for start in sorted(set(hours) | set(before) | set(in_frame)):
        if start in in_frame:
            value = in_frame[start]
        elif start < frame_hour:
            value = min(before.get(start, value), cap)
        rows.append((start, value))
    return Rebase(frame_start=frame_start, offset=offset, rows=rows)
