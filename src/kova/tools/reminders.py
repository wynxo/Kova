"""Tools for creating and listing reminders."""

from __future__ import annotations

import json
import re
from datetime import date, datetime, time, timedelta

from . import Tool, ToolContext, ToolError

_BAD_TIME = (
    'Could not understand that time. Try "in 10 min", "tomorrow 5pm" '
    'or "2026-11-20 18:30".'
)

# One unit per entry; the value is a number of seconds.
_RELATIVE_UNITS = {
    "s": 1, "sec": 1, "secs": 1, "second": 1, "seconds": 1, "sekunde": 1, "sekunden": 1,
    "m": 60, "min": 60, "mins": 60, "minute": 60, "minutes": 60,
    "minuten": 60,
    "h": 3600, "hr": 3600, "hrs": 3600, "hour": 3600, "hours": 3600,
    "stunde": 3600, "stunden": 3600,
    "d": 86400, "day": 86400, "days": 86400, "tag": 86400, "tage": 86400,
    "tagen": 86400,
    "w": 604800, "week": 604800, "weeks": 604800, "woche": 604800,
    "wochen": 604800,
}

_WEEKDAYS = {
    "monday": 0, "mon": 0, "montag": 0, "mo": 0,
    "tuesday": 1, "tue": 1, "tues": 1, "dienstag": 1, "di": 1,
    "wednesday": 2, "wed": 2, "mittwoch": 2, "mi": 2,
    "thursday": 3, "thu": 3, "thur": 3, "thurs": 3, "donnerstag": 3, "do": 3,
    "friday": 4, "fri": 4, "freitag": 4, "fr": 4,
    "saturday": 5, "sat": 5, "samstag": 5, "sonnabend": 5, "sa": 5,
    "sunday": 6, "sun": 6, "sonntag": 6, "so": 6,
}
_WEEKDAY_NAMES = "|".join(sorted(_WEEKDAYS, key=len, reverse=True))

_DAY_RE = re.compile(
    r"^(?P<day>day after tomorrow|übermorgen|uebermorgen|heute abend|tonight|"
    r"tomorrow|morgen|today|heute)(?:\s+(?P<rest>.+))?$"
)
_WEEKDAY_RE = re.compile(
    rf"^(?:(?P<next>next|nächsten|naechsten|kommenden)\s+)?"
    rf"(?:(?:on|am)\s+)?(?P<day>{_WEEKDAY_NAMES})\b"
    r"(?:\s+(?P<rest>.+))?$"
)
_RELATIVE_RE = re.compile(r"(?:\d+\s*[^\W\d_]+)(?:\s+\d+\s*[^\W\d_]+)*", re.UNICODE)
_RELATIVE_PART_RE = re.compile(r"(\d+)\s*([^\W\d_]+)", re.UNICODE)


def _localize(naive: datetime, now: datetime) -> datetime:
    """Attach the local zone to a naive wall time, following DST rules where possible."""
    tz = now.tzinfo
    if tz is None:
        return naive
    # A real zone (ZoneInfo) knows its own summer-time rules for the target date.
    if getattr(tz, "key", None):
        return naive.replace(tzinfo=tz)
    # The real clock is a fixed offset: re-resolve through the system zone so that
    # a reminder scheduled across a DST boundary gets the right offset.
    if now.utcoffset() == datetime.now().astimezone().utcoffset():
        return naive.astimezone()
    return naive.replace(tzinfo=tz)


def _ensure_future(when: datetime, now: datetime) -> datetime:
    if now.tzinfo is None and when.tzinfo is not None:
        when = when.replace(tzinfo=None)
    if when <= now:
        raise ToolError("Reminder time must be in the future")
    return when


def _build(day: date, hour: int, minute: int, now: datetime) -> datetime:
    return _localize(datetime.combine(day, time(hour, minute)), now)


def _parse_time(text: str) -> tuple[int, int] | None:
    """Parse a clock expression: ``17:30``, ``5pm``, ``5:30 p.m.``, ``9 Uhr``."""
    text = " ".join(text.strip().lower().split())
    text = re.sub(r"^(?:at|um|@)\s*", "", text)
    text = re.sub(r"^(?:around|about|gegen)\s+", "", text)

    # German "9 Uhr 30" means 9:30.
    match = re.fullmatch(r"(\d{1,2})\s*uhr\s*(\d{2})", text)
    if match:
        hour, minute = int(match.group(1)), int(match.group(2))
        return (hour, minute) if hour < 24 and minute < 60 else None

    text = re.sub(r"\s*(?:uhr|o'clock|oclock)$", "", text)

    match = re.fullmatch(r"(\d{1,2})(?:[:.](\d{2}))?\s*(a\.?m\.?|p\.?m\.?)", text)
    if match:
        hour = int(match.group(1))
        minute = int(match.group(2) or 0)
        if not 1 <= hour <= 12 or minute > 59:
            return None
        suffix = match.group(3).replace(".", "")
        if suffix == "am":
            hour = 0 if hour == 12 else hour
        else:
            hour = 12 if hour == 12 else hour + 12
        return hour, minute

    match = re.fullmatch(r"(\d{1,2})[:.](\d{2})(?::\d{2})?", text)
    if match:
        hour, minute = int(match.group(1)), int(match.group(2))
        return (hour, minute) if hour < 24 and minute < 60 else None

    match = re.fullmatch(r"(\d{1,2})", text)
    if match:
        hour = int(match.group(1))
        return (hour, 0) if hour < 24 else None
    return None


def _parse_relative(text: str, now: datetime) -> datetime | None:
    """Parse ``in 10 min``, ``2h`` or ``in 1 hour 30 minutes`` relative to ``now``."""
    body = text[3:].strip() if text.startswith("in ") else text
    if not _RELATIVE_RE.fullmatch(body):
        return None
    parts = _RELATIVE_PART_RE.findall(body)
    total = 0
    for amount, unit in parts:
        seconds = _RELATIVE_UNITS.get(unit)
        if seconds is None:
            return None
        total += int(amount) * seconds
    return now + timedelta(seconds=total)


def _parse_day(text: str, now: datetime) -> datetime | None:
    """Parse day words such as ``today 18:30``, ``tomorrow 5pm`` and ``tonight``."""
    match = _DAY_RE.fullmatch(text)
    if not match:
        return None
    word = match.group("day")
    rest = match.group("rest")

    if word in ("tomorrow", "morgen"):
        day = now.date() + timedelta(days=1)
    elif word in ("übermorgen", "uebermorgen", "day after tomorrow"):
        day = now.date() + timedelta(days=2)
    else:  # today, heute, tonight, heute abend
        day = now.date()

    if rest is None:
        hour, minute = (20, 0) if word in ("tonight", "heute abend") else (9, 0)
    else:
        parsed = _parse_time(rest)
        if parsed is None:
            raise ToolError(_BAD_TIME)
        hour, minute = parsed
    return _build(day, hour, minute, now)


def _parse_weekday(text: str, now: datetime) -> datetime | None:
    """Parse ``monday 9am`` and ``next friday 14:00``."""
    match = _WEEKDAY_RE.fullmatch(text)
    if not match:
        return None
    target = _WEEKDAYS[match.group("day")]
    is_next = match.group("next") is not None
    rest = match.group("rest")

    if rest is None:
        hour, minute = 9, 0
    else:
        parsed = _parse_time(rest)
        if parsed is None:
            raise ToolError(_BAD_TIME)
        hour, minute = parsed

    delta = (target - now.weekday()) % 7
    if is_next and delta == 0:
        delta = 7
    when = _build(now.date() + timedelta(days=delta), hour, minute, now)
    # A plain weekday may mean today if its time is still ahead; otherwise next week.
    if not is_next and delta == 0 and when <= now:
        when = _build(now.date() + timedelta(days=7), hour, minute, now)
    return when


def _parse_clock(text: str, now: datetime) -> datetime | None:
    """Parse a bare clock time: today if still ahead, otherwise tomorrow."""
    if re.fullmatch(r"\d{1,2}", text):
        return None  # A bare number is too ambiguous outside a day phrase.
    parsed = _parse_time(text)
    if parsed is None:
        return None
    hour, minute = parsed
    when = _build(now.date(), hour, minute, now)
    if when <= now:
        when = _build(now.date() + timedelta(days=1), hour, minute, now)
    return when


def parse_when(text: str, now: datetime) -> datetime:
    """Parse a reminder time in local time; it must be in the future.

    Accepts ISO 8601 (``2026-11-20 18:30``), relative phrases (``in 10 min``,
    ``2h``), day words (``tomorrow 5pm``, ``tonight``), weekday phrases
    (``monday 9am``, ``next friday 14:00``) and clock times (``17:30``,
    ``5:30pm``). German words are understood too (``morgen 9 Uhr``,
    ``übermorgen``). Naive times are interpreted in the local zone.
    """
    text = " ".join(text.strip().lower().split())
    if not text:
        raise ToolError(_BAD_TIME)

    try:
        when = datetime.fromisoformat(text)
    except ValueError:
        when = None
    if when is not None:
        if when.tzinfo is None:
            when = _localize(when, now)
        return _ensure_future(when, now)

    for parser in (_parse_relative, _parse_day, _parse_weekday, _parse_clock):
        when = parser(text, now)
        if when is not None:
            return _ensure_future(when, now)

    raise ToolError(_BAD_TIME)


def format_when(when: datetime, now: datetime) -> str:
    """A human day and clock, e.g. ``tomorrow at 17:00``."""
    days = (when.date() - now.date()).days
    clock = when.strftime("%H:%M")
    if days == 0:
        return f"today at {clock}"
    if days == 1:
        return f"tomorrow at {clock}"
    if 2 <= days <= 6:
        return f"{when.strftime('%A')} at {clock}"
    return f"{when.strftime('%Y-%m-%d')} at {clock}"


def format_relative(when: datetime, now: datetime, *, spaces: bool = False) -> str:
    """How far away ``when`` is, e.g. ``in 2h`` (or ``in 19 h`` with spaces)."""
    seconds = int((when - now).total_seconds())
    if seconds < 1:
        return "now"
    sep = " " if spaces else ""
    if seconds < 60:
        return f"in {seconds}{sep}s"
    minutes = seconds // 60
    if minutes < 60:
        return f"in {minutes}{sep}m"
    hours = minutes // 60
    if hours < 24:
        return f"in {hours}{sep}h"
    days = hours // 24
    if days < 14:
        return f"in {days}{sep}d"
    return f"in {days // 7}{sep}w"


def tools(ctx: ToolContext) -> list[Tool]:
    """Create reminder tools for a context."""
    def add(args: dict) -> str:
        when = parse_when(args["when"], ctx.now())
        rid = ctx.store.add_reminder(when, args["text"])
        return f"Reminder #{rid} set for {when.isoformat()}"

    def list_reminders(args: dict) -> str:
        return json.dumps(ctx.store.list_reminders())

    return [
        Tool(
            "reminder_add",
            "Set a reminder that pops up at a given time, even if the user closed the chat. "
            "Accept natural language times in the user's local time, e.g. 'in 10 min', "
            "'tomorrow 5pm', 'monday 9am', 'tonight', or ISO 8601 like 2026-11-12T18:30. "
            "Work it out from the current time in your instructions.",
            {
                "type": "object",
                "properties": {
                    "when": {
                        "type": "string",
                        "description": "Natural time in local time: 'in 10 min', 'tomorrow 5pm', "
                                       "'next friday 14:00', 'tonight', or ISO 8601 e.g. "
                                       "2026-11-12T18:30.",
                    },
                    "text": {"type": "string"},
                },
                "required": ["when", "text"],
            },
            add,
        ),
        Tool("reminder_list", "List the user's upcoming reminders.", {"type": "object", "properties": {}}, list_reminders),
    ]
