from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from croniter import croniter

from orchestrator.protocols.hub_v1 import HubInstanceScheduleV1


def latest_slot(schedule: HubInstanceScheduleV1, anchor: datetime, now: datetime) -> datetime | None:
    """Return the most recent schedule slot in ``(anchor, now]``, or ``None``.

    Deliberately returns at most one slot. A Hub that was offline across several
    slots fires once for the latest missed one and never replays the rest:
    silently backfilling runs on a machine that just came back is the more
    dangerous default. Both inputs must be timezone-aware; the result is UTC.
    """
    if anchor.tzinfo is None or now.tzinfo is None:
        raise ValueError("schedule anchor and now must be timezone-aware")
    if now <= anchor:
        return None
    if schedule.kind == "interval":
        seconds = int(schedule.seconds or 0)
        if seconds <= 0:
            return None
        steps = int((now - anchor).total_seconds() // seconds)
        if steps < 1:
            return None
        return (anchor + timedelta(seconds=steps * seconds)).astimezone(timezone.utc)

    tz = ZoneInfo(schedule.timezone) if schedule.timezone else timezone.utc
    local_now = now.astimezone(tz)
    # croniter's get_prev is strictly earlier than its base; shift by one second so
    # a slot that falls exactly on `now` still counts as due.
    candidate = croniter(schedule.expr or "", local_now + timedelta(seconds=1)).get_prev(datetime)
    if candidate.tzinfo is None:
        candidate = candidate.replace(tzinfo=tz)
    candidate = candidate.astimezone(timezone.utc)
    return candidate if candidate > anchor else None
