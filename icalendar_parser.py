"""Parse iCalendar resources into ContentFeed's normalized post shape."""

import calendar
import math
import re
import time as clock_time
import urllib.parse
from datetime import datetime, time, timedelta, timezone
from typing import Literal, TypedDict
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


class ICalendarError(ValueError):
    """The downloaded resource is not a complete iCalendar object."""


class CalendarPost(TypedDict):
    """Normalized calendar item consumed by the feed and ranking layers."""

    url: str
    title: str
    description: str
    published: float | None
    calendar_kind: Literal["event", "task"]
    calendar_start: float | None
    deadline: float | None
    calendar_uid: str
    priority: int | None  # PRIORITY 1 (highest) to 9 (lowest); None if undefined


def _datetime(value, params=""):
    if not value:
        return None
    value = value.strip()
    try:
        if re.fullmatch(r"\d{8}", value):
            return datetime.combine(datetime.strptime(value, "%Y%m%d").date(), time.min, timezone.utc)
        if re.fullmatch(r"\d{8}T\d{6}Z", value):
            return datetime.strptime(value, "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
        if re.fullmatch(r"\d{8}T\d{6}", value):
            dt = datetime.strptime(value, "%Y%m%dT%H%M%S")
            tzid = re.search(r'(?:^|;)TZID="?([^;:"]+)', params)
            if tzid:
                try:
                    dt = dt.replace(tzinfo=ZoneInfo(tzid.group(1)))
                except ZoneInfoNotFoundError:
                    dt = dt.replace(tzinfo=timezone.utc)
            else:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt
    except ValueError:
        return None
    return None


def _date(value, params=""):
    dt = _datetime(value, params)
    return dt.timestamp() if dt else None


def _unescape(value):
    return value.replace("\\n", "\n").replace("\\N", "\n").replace("\\,", ",").replace("\\;", ";").replace("\\\\", "\\").strip()


PROPERTY_LINE = re.compile(r'((?:[^":]|"[^"]*")*):(.*)', re.S)


def _components(text):
    """Yield unfolded VEVENT and VTODO property maps from an iCalendar resource."""
    lines = []
    for line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        if line.startswith((" ", "\t")) and lines:
            lines[-1] += line[1:]
        else:
            lines.append(line)
    stack = []
    current = None
    for line in lines:
        # The value starts after the first colon outside quotes: quoted parameter values can hold
        # colons, such as Thunderbird's DESCRIPTION;ALTREP="data:text/html,…":plain text.
        match = PROPERTY_LINE.match(line)
        if not match:
            continue
        key, value = match.groups()
        name = key.split(";", 1)[0].upper()
        if name == "BEGIN":
            stack.append(value.upper())
            if value.upper() in ("VEVENT", "VTODO"):
                current = {"_type": value.upper()}
        elif name == "END":
            if current is not None and value.upper() == current["_type"]:
                yield current
                current = None
            if stack:
                stack.pop()
        elif current is not None:
            params = key[len(name):]
            current.setdefault(name, []).append((params, value))


def _prop(component, name):
    values = component.get(name, [])
    return values[0] if values else ("", "")


def _date_prop(component, name):
    params, value = _prop(component, name)
    return _date(value, params)


def _parse_rrule(component):
    rules = dict(part.split("=", 1) for part in _prop(component, "RRULE")[1].upper().split(";") if "=" in part)
    freq = rules.get("FREQ")
    try:
        interval = max(1, int(rules.get("INTERVAL", "1")))
    except ValueError:
        return None
    count_text = rules.get("COUNT", "")
    count = int(count_text) if count_text.isdigit() and len(count_text) < 10 else None
    until = _date(rules.get("UNTIL", ""))
    weekdays = ["MO", "TU", "WE", "TH", "FR", "SA", "SU"]
    byday = []
    for token in rules.get("BYDAY", "").split(","):
        match = re.fullmatch(r"([+-]?\d{1,2})?(MO|TU|WE|TH|FR|SA|SU)", token)
        if match:
            ordinal = int(match.group(1)) if match.group(1) else None
            byday.append((ordinal, weekdays.index(match.group(2))))
    return freq, interval, count, until, byday


def _daily_occurrences(dt, lower, upper, interval, count, byday):
    day_delta = (datetime.fromtimestamp(lower, dt.tzinfo).date() - dt.date()).days
    first = max(0, day_delta // interval - 1)
    last = max(first, (datetime.fromtimestamp(upper, dt.tzinfo).date() - dt.date()).days // interval + 1)
    if not byday:
        stop = min(last + 1, first + 100, count if count is not None else last + 1)
        return [(dt + timedelta(days=i * interval)).timestamp() for i in range(first, stop)
                if lower <= (dt + timedelta(days=i * interval)).timestamp() <= upper]

    selected = {weekday for _, weekday in byday}
    cycle = 7 // math.gcd(interval, 7)
    pattern = [((dt.weekday() + k * interval) % 7) in selected for k in range(1, cycle + 1)]
    prior_periods, prior_days = divmod(max(0, first - 1), cycle)
    ordinal = (1 if first else 0) + prior_periods * sum(pattern) + sum(pattern[:prior_days])
    occurrences = []
    for index in range(first, min(last + 1, first + 100)):
        occurrence = dt + timedelta(days=index * interval)
        stamp = occurrence.timestamp()
        if index and occurrence.weekday() not in selected:
            continue
        if stamp > upper or (count is not None and ordinal >= count):
            break
        if stamp >= lower:
            occurrences.append(stamp)
        ordinal += 1
    return occurrences


def _weekly_occurrences(dt, start, lower, upper, interval, count, byday):
    if not byday:
        return _daily_occurrences(dt, lower, upper, interval * 7, count, [])

    selected = sorted({weekday for _, weekday in byday})
    start_is_selected = dt.weekday() in selected
    anchor = dt - timedelta(days=dt.weekday())
    lower_date, upper_date = datetime.fromtimestamp(lower, dt.tzinfo).date(), datetime.fromtimestamp(upper, dt.tzinfo).date()
    first = max(0, (lower_date - anchor.date()).days // (7 * interval) - 1)
    last = max(first, (upper_date - anchor.date()).days // (7 * interval) + 1)
    occurrences = []
    if not start_is_selected and lower <= start <= upper and (count is None or count > 0):
        occurrences.append(start)
    first_week_count = sum(day >= dt.weekday() for day in selected) + (not start_is_selected)
    before_start = sum(day < dt.weekday() for day in selected)
    for week in range(first, min(first + 100, last + 1)):
        for index, weekday in enumerate(selected):
            occurrence = anchor + timedelta(days=week * 7 * interval + weekday)
            stamp = occurrence.timestamp()
            if stamp < start:
                continue
            ordinal = (index - before_start + (not start_is_selected) if week == 0
                       else first_week_count + (week - 1) * len(selected) + index)
            if (count is not None and ordinal >= count) or stamp > upper:
                break
            if stamp >= lower:
                occurrences.append(stamp)
    return occurrences


def _month_occurrence(dt, index, interval):
    month_index = dt.year * 12 + dt.month - 1 + index * interval
    year, month0 = divmod(month_index, 12)
    month = month0 + 1
    if dt.day > calendar.monthrange(year, month)[1]:
        return None
    return dt.replace(year=year, month=month)


def _monthly_byday_occurrences(dt, lower, upper, interval, count, until, byday):
    lower_date, upper_date = datetime.fromtimestamp(lower, dt.tzinfo), datetime.fromtimestamp(upper, dt.tzinfo)
    months_apart = (lower_date.year - dt.year) * 12 + lower_date.month - dt.month
    months_until = (upper_date.year - dt.year) * 12 + upper_date.month - dt.month
    first = max(0, months_apart // interval - 1)
    last = max(first, months_until // interval + 1)
    occurrences, ordinal = [], 0
    for index in range(0 if count is not None else first, last + 1):
        month_index = dt.year * 12 + dt.month - 1 + index * interval
        year, month0 = divmod(month_index, 12)
        month = month0 + 1
        matching = {}
        for nth, weekday in byday:
            days = [day for day in range(1, calendar.monthrange(year, month)[1] + 1)
                    if datetime(year, month, day).weekday() == weekday]
            chosen = days if nth is None else [days[nth - 1] if nth > 0 and nth <= len(days)
                                               else days[nth] if nth < 0 and -nth <= len(days) else None]
            matching.update((day, dt.replace(year=year, month=month, day=day))
                            for day in chosen if day is not None)
        if index == 0:
            matching.setdefault(dt.day, dt)
        for occurrence in (candidate for day, candidate in sorted(matching.items()) if candidate >= dt):
            stamp = occurrence.timestamp()
            if stamp > upper or (until is not None and stamp > until) or (count is not None and ordinal >= count):
                return occurrences
            if stamp >= lower:
                occurrences.append(stamp)
            ordinal += 1
    return occurrences


def _monthly_yearly_occurrences(dt, lower, upper, interval, count, yearly):
    period = interval * (12 if yearly else 1)
    lower_date, upper_date = datetime.fromtimestamp(lower, dt.tzinfo), datetime.fromtimestamp(upper, dt.tzinfo)
    months_apart = (lower_date.year - dt.year) * 12 + lower_date.month - dt.month
    first = max(0, months_apart // period - 1)
    months_until = (upper_date.year - dt.year) * 12 + upper_date.month - dt.month
    last = max(first, months_until // period + 1)
    occurrences, ordinal = [], 0
    for index in range(first):
        if _month_occurrence(dt, index * (12 if yearly else 1), interval) is not None:
            ordinal += 1
    for index in range(first, min(first + 100, last + 1)):
        occurrence = _month_occurrence(dt, index * (12 if yearly else 1), interval)
        if occurrence is None:
            continue
        stamp = occurrence.timestamp()
        if stamp > upper or (count is not None and ordinal >= count):
            break
        if stamp >= lower:
            occurrences.append(stamp)
        ordinal += 1
    return occurrences


def _occurrences(component, start, base_value="", base_params="", window=None):
    """Expand supported RRULEs into instances in a bounded window: (lower, upper) unix times,
    by default from a day ago to 90 days ahead."""
    rule = _parse_rrule(component)
    if not start or rule is None:
        return [start]
    freq, interval, count, until, byday = rule
    if freq not in ("DAILY", "WEEKLY", "MONTHLY", "YEARLY"):
        return [start]
    lower, upper = window or (clock_time.time() - 86400, clock_time.time() + 90 * 86400)
    if interval > 1_000_000:
        return [start] if lower <= start <= upper else []
    dt = _datetime(base_value, base_params) or datetime.fromtimestamp(start, timezone.utc)
    if freq == "DAILY":
        occurrences = _daily_occurrences(dt, lower, upper, interval, count, byday)
    elif freq == "WEEKLY":
        occurrences = _weekly_occurrences(dt, start, lower, upper, interval, count, byday)
    elif freq == "MONTHLY" and byday:
        occurrences = _monthly_byday_occurrences(dt, lower, upper, interval, count, until, byday)
    else:
        occurrences = _monthly_yearly_occurrences(dt, lower, upper, interval, count, freq == "YEARLY")
    if until is not None:
        occurrences = [stamp for stamp in occurrences if stamp <= until]
    return occurrences or ([start] if start >= lower else [])


def _base(comp):
    """(time, value, params) that a component's recurrences repeat: its start, or a task's due date."""
    start_params, start_value = _prop(comp, "DTSTART")
    start = _date(start_value, start_params)
    if comp["_type"] == "VEVENT" or start is not None:
        return start, start_value, start_params
    due_params, due_value = _prop(comp, "DUE")
    return _date(due_value, due_params), due_value, due_params


def _excluded(comp):
    return {_date(part, params) for params, value in comp.get("EXDATE", []) for part in value.split(",")}


def _is_instance(master, recurrence):
    """Whether the recurring master still has an instance at recurrence. An edited instance whose series
    has since been shortened or changed no longer belongs to it, even if the edit is still stored."""
    base, base_value, base_params = _base(master)
    instances = set(_occurrences(master, base, base_value, base_params, (recurrence - 1, recurrence + 1)))
    for params, value in master.get("RDATE", []):
        instances.update(_date(part, params) for part in value.split(","))
    return recurrence in instances and recurrence not in _excluded(master)


def _items(calendar_data, resource_url) -> list[CalendarPost]:
    out = []
    components = list(_components(calendar_data))
    overrides = set()
    masters = {}
    for component in components:
        recurrence = _date_prop(component, "RECURRENCE-ID")
        if recurrence is not None:
            overrides.add((_prop(component, "UID")[1], recurrence))
        else:
            masters.setdefault(_prop(component, "UID")[1], component)
    for comp in components:
        recurrence = _date_prop(comp, "RECURRENCE-ID")
        master = masters.get(_prop(comp, "UID")[1])
        if recurrence is not None and master is not None and not _is_instance(master, recurrence):
            continue  # left over from before its series changed
        kind = "event" if comp["_type"] == "VEVENT" else "task"
        title = _unescape(_prop(comp, "SUMMARY")[1]) or "Untitled calendar item"
        description = _unescape(_prop(comp, "DESCRIPTION")[1])
        start_params, start_value = _prop(comp, "DTSTART")
        due_params, due_value = _prop(comp, "DUE")
        end_params, end_value = _prop(comp, "DTEND")
        created_params, created_value = _prop(comp, "CREATED")
        deadline = (_date(due_value, due_params) if kind == "task"
                    else (_date(end_value, end_params) or _date(due_value, due_params)))
        start = _date(start_value, start_params)
        status = _prop(comp, "STATUS")[1].upper()
        completed = bool(_prop(comp, "COMPLETED")[1])
        priority = _prop(comp, "PRIORITY")[1].strip()
        priority = int(priority) if re.fullmatch(r"[1-9]", priority) else None  # 0 means undefined
        if (kind == "task" and (status in ("COMPLETED", "CANCELLED") or completed)) or (kind == "event" and status == "CANCELLED"):
            continue
        uid = _prop(comp, "UID")[1] or title
        recur = _date_prop(comp, "RECURRENCE-ID")
        external = _unescape(_prop(comp, "URL")[1])
        base = start if kind == "event" or start is not None else deadline
        base_value, base_params = ((start_value, start_params) if base == start
                                   else (due_value, due_params))
        occurrences = [recur] if recur else _occurrences(comp, base, base_value, base_params)
        for params, value in comp.get("RDATE", []):
            occurrences.extend(stamp for stamp in (_date(part, params) for part in value.split(",")) if stamp is not None)
        occurrences = sorted(set(occurrences), key=lambda stamp: stamp if stamp is not None else float("-inf"))
        excluded = _excluded(comp)
        for occurrence in occurrences:
            if occurrence in excluded:
                continue
            if recur is None and occurrence is not None and (uid, occurrence) in overrides:
                continue
            shift = occurrence - base if recur is None and occurrence is not None and base is not None else 0
            item_start = start + shift if start is not None else None
            item_deadline = deadline + shift if deadline is not None else None
            stable = urllib.parse.quote(uid, safe="") + (f"-{int(occurrence)}" if occurrence else "")
            target = external if external.startswith(("http://", "https://")) else resource_url
            parsed_target = urllib.parse.urlsplit(target)
            fragment = f"{parsed_target.fragment}&item={stable}" if parsed_target.fragment else f"item={stable}"
            url = urllib.parse.urlunsplit(parsed_target._replace(fragment=fragment))
            published = item_start if kind == "event" else (item_deadline or item_start or _date(created_value, created_params))
            out.append({
                "url": url, "title": title[:300], "description": description[:500],
                "published": published, "calendar_kind": kind, "calendar_start": item_start,
                "deadline": item_deadline, "calendar_uid": uid, "priority": priority,
            })
    return out


def parse_calendar_resource(text, resource_url) -> list[CalendarPost]:
    """Convert one VCALENDAR resource into the app's normalized post dictionaries."""
    return _items(text, resource_url)


def parse_calendar_data(body, resource_url):
    text = body.decode("utf-8-sig", errors="replace")
    if not re.search(r"(?im)^BEGIN:VCALENDAR\s*$", text) or not re.search(r"(?im)^END:VCALENDAR\s*$", text):
        raise ICalendarError("The .ics response is not a complete iCalendar object.")
    return calendar_result(parse_calendar_resource(text, resource_url), None, resource_url)


MAX_ENTRIES = 1000  # tasks, and separately events, kept per calendar


def calendar_result(entries: list[CalendarPost], title: str | None, url: str) -> dict:
    """A calendar subscription's fetch result. Tasks are its posts ("items"); events take place when
    scheduled, so they aren't posts and are kept apart ("events") to act only as stop times."""
    entries = sorted(entries, key=lambda entry: entry.get("published") or 0, reverse=True)
    name = title or urllib.parse.unquote(urllib.parse.urlsplit(url).path.rstrip("/").split("/")[-1]) or "Calendar"
    return {"title": name, "site": url, "icon": None, "complete": True,
            "items": [e for e in entries if e["calendar_kind"] != "event"][:MAX_ENTRIES],
            "events": [e for e in entries if e["calendar_kind"] == "event"][:MAX_ENTRIES]}


