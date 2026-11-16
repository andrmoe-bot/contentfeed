"""Read public iCalendar files and CalDAV collections (including Radicale)."""

import base64
import calendar
import re
import math
import time as clock_time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import outbound

USER_AGENT = "Mozilla/5.0 (compatible; ContentFeed/1.0)"
TIMEOUT = 10
MAX_BYTES = 20_000_000
DAV = "DAV:"
CAL = "urn:ietf:params:xml:ns:caldav"


class CalDAVError(Exception):
    pass


def _url_and_auth(url):
    p = urllib.parse.urlsplit(url)
    if p.scheme not in ("http", "https") or not p.hostname:
        raise CalDAVError("That isn't a valid CalDAV web address.")
    auth = None
    if p.username is not None:
        raw = f"{urllib.parse.unquote(p.username)}:{urllib.parse.unquote(p.password or '')}"
        auth = "Basic " + base64.b64encode(raw.encode()).decode()
        host = p.hostname + (f":{p.port}" if p.port else "")
        url = urllib.parse.urlunsplit((p.scheme, host, p.path, p.query, p.fragment))
    return url, auth


def _request(url, method="GET", body=None, headers=None, auth=None):
    h = {"User-Agent": USER_AGENT, **(headers or {})}
    if auth:
        h["Authorization"] = auth
    request = urllib.request.Request(url, data=body, headers=h, method=method)
    try:
        with outbound.urlopen(request, timeout=TIMEOUT) as response:
            payload = response.read(MAX_BYTES + 1)
            if len(payload) > MAX_BYTES:
                raise CalDAVError("The calendar response is too large.")
            return payload, response.geturl(), response.headers
    except urllib.error.HTTPError as e:
        raise CalDAVError(f"CalDAV returned HTTP {e.code} from {url}") from e
    except OSError as e:
        raise CalDAVError(f"couldn't fetch calendar {url}: {e}") from e


def _date(value, params=""):
    if not value:
        return None
    value = value.strip()
    try:
        if re.fullmatch(r"\d{8}", value):
            dt = datetime.combine(datetime.strptime(value, "%Y%m%d").date(), time.min, timezone.utc)
        elif re.fullmatch(r"\d{8}T\d{6}Z", value):
            dt = datetime.strptime(value, "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
        elif re.fullmatch(r"\d{8}T\d{6}", value):
            dt = datetime.strptime(value, "%Y%m%dT%H%M%S")
            tzid = re.search(r'(?:^|;)TZID="?([^;:"]+)', params)
            if tzid:
                try:
                    dt = dt.replace(tzinfo=ZoneInfo(tzid.group(1)))
                except ZoneInfoNotFoundError:
                    dt = dt.replace(tzinfo=timezone.utc)
            else:
                dt = dt.replace(tzinfo=timezone.utc)
        else:
            return None
        return dt.timestamp()
    except ValueError:
        return None


def _unescape(value):
    return value.replace("\\n", "\n").replace("\\N", "\n").replace("\\,", ",").replace("\\;", ";").replace("\\\\", "\\").strip()


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
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
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


def _occurrences(component, start):
    """Expand common daily, weekly, monthly and yearly RRULEs for the next 90 days."""
    rules = dict(part.split("=", 1) for part in _prop(component, "RRULE")[1].upper().split(";") if "=" in part)
    freq = rules.get("FREQ")
    if not start or freq not in ("DAILY", "WEEKLY", "MONTHLY", "YEARLY"):
        return [start]
    interval = max(1, int(rules.get("INTERVAL", "1")))
    count = int(rules["COUNT"]) if rules.get("COUNT", "").isdigit() else None
    until = _date("", "")
    if rules.get("UNTIL"):
        until = _date(rules["UNTIL"])
    lower, upper = clock_time.time() - 86400, clock_time.time() + 90 * 86400
    dt = datetime.fromtimestamp(start, timezone.utc)
    out = []
    weekdays = ["MO", "TU", "WE", "TH", "FR", "SA", "SU"]
    byday = [weekdays.index(d) for d in rules.get("BYDAY", "").split(",") if d in weekdays]

    def month_date(index):
        month = dt.year * 12 + (dt.month - 1) + index * interval
        year, month0 = divmod(month, 12)
        month = month0 + 1
        return dt.replace(year=year, month=month, day=min(dt.day, calendar.monthrange(year, month)[1]))

    if freq in ("DAILY", "WEEKLY") and not (freq == "WEEKLY" and byday):
        step = interval * (86400 if freq == "DAILY" else 7 * 86400)
        first = max(0, int(math.ceil((lower - start) / step)))
        last = int((upper - start) // step)
        indices = range(first, min(last + 1, count or last + 1, first + 100))
        out = [start + i * step for i in indices]
    elif freq in ("MONTHLY", "YEARLY"):
        period = interval if freq == "MONTHLY" else 12 * interval
        months_apart = (datetime.fromtimestamp(lower, timezone.utc).year - dt.year) * 12 + datetime.fromtimestamp(lower, timezone.utc).month - dt.month
        first = max(0, months_apart // period)
        for index in range(first, first + 100):
            occurrence = month_date(index if freq == "MONTHLY" else index * 12)
            stamp = occurrence.timestamp()
            if stamp > upper or (count is not None and index >= count):
                break
            if stamp >= lower:
                out.append(stamp)
    else:  # weekly with BYDAY
        selected = sorted(byday or [dt.weekday()])
        anchor = dt - timedelta(days=dt.weekday())
        week_index = max(0, int((datetime.fromtimestamp(lower, timezone.utc) - anchor).days // (7 * interval)))
        seen = 0
        for week in range(week_index, week_index + 100):
            for weekday in selected:
                occurrence = anchor + timedelta(days=week * 7 * interval + weekday)
                stamp = occurrence.timestamp()
                if stamp < start:
                    continue
                ordinal = week * len(selected) + selected.index(weekday) - sum(day < dt.weekday() for day in selected)
                if (count is not None and ordinal >= count) or stamp > upper:
                    break
                if (until is None or stamp <= until) and stamp >= lower:
                    out.append(stamp)
            if out and out[-1] > upper:
                break
    if until is not None:
        out = [stamp for stamp in out if stamp <= until]
    return out or ([start] if start >= lower else [])


def _items(calendar_data, resource_url):
    out = []
    for comp in _components(calendar_data):
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
        if (kind == "task" and status in ("COMPLETED", "CANCELLED")) or (kind == "event" and status == "CANCELLED"):
            continue
        uid = _prop(comp, "UID")[1] or title
        recur = _date(*_prop(comp, "RECURRENCE-ID"))
        external = _unescape(_prop(comp, "URL")[1])
        base = start if kind == "event" else (deadline or start)
        occurrences = [recur] if recur else _occurrences(comp, base)
        excluded = {_date(value, params) for params, value in comp.get("EXDATE", [])}
        for occurrence in occurrences:
            if occurrence in excluded:
                continue
            shift = occurrence - base if occurrence is not None and base is not None else 0
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
                "deadline": item_deadline, "calendar_uid": uid,
            })
    return out


def _calendar_data(body, resource_url):
    text = body.decode("utf-8-sig", errors="replace")
    items = _items(text, resource_url)
    return {"title": urllib.parse.unquote(urllib.parse.urlsplit(resource_url).path.rstrip("/").split("/")[-1]) or "Calendar",
            "site": resource_url, "icon": None, "items": items, "complete": True}


def fetch(url):
    """Fetch an .ics URL or query a CalDAV collection. URL userinfo supplies Basic auth."""
    clean_url, auth = _url_and_auth(url)
    # A shared .ics calendar is often a plain HTTP GET rather than a DAV collection.
    raw_calendar = None
    try:
        body, final_url, headers = _request(clean_url, headers={"Accept": "text/calendar, */*"}, auth=auth)
        if body.lstrip().startswith(b"BEGIN:VCALENDAR") or "text/calendar" in headers.get("Content-Type", "").lower():
            raw_calendar = (body, final_url)
            path = urllib.parse.urlsplit(clean_url).path
            if path.lower().endswith(".ics") and not path.endswith("/"):
                return _calendar_data(body, final_url)
    except CalDAVError:
        pass  # collection URLs commonly reject GET and require REPORT

    items = []
    title = None
    report_succeeded = False
    for component in ("VEVENT", "VTODO"):
        report = f'''<?xml version="1.0" encoding="utf-8"?>
<C:calendar-query xmlns:C="{CAL}" xmlns:D="{DAV}">
 <D:prop><D:getetag/><C:calendar-data/></D:prop>
 <C:filter><C:comp-filter name="VCALENDAR"><C:comp-filter name="{component}"/></C:comp-filter></C:filter>
</C:calendar-query>'''.encode()
        try:
            body, _, _ = _request(clean_url, "REPORT", report,
                {"Depth": "1", "Content-Type": "application/xml; charset=utf-8", "Accept": "application/xml"}, auth)
            report_succeeded = True
        except CalDAVError:
            if not report_succeeded and raw_calendar:
                return _calendar_data(*raw_calendar)
            if report_succeeded:
                continue
            raise
        try:
            root = ET.fromstring(body)
        except ET.ParseError as e:
            raise CalDAVError("The CalDAV server returned invalid XML.") from e
        for response in root.iter(f"{{{DAV}}}response"):
            href = next((e.text for e in response.findall(f"{{{DAV}}}href") if e.text), None)
            resource_url = urllib.parse.urljoin(clean_url, href or "")
            data = response.find(f".//{{{CAL}}}calendar-data")
            if data is not None and data.text:
                items.extend(_items(data.text, resource_url))
    if not report_succeeded and raw_calendar:
        return _calendar_data(*raw_calendar)
    items.sort(key=lambda item: item.get("published") or 0, reverse=True)
    return {"title": title or urllib.parse.unquote(urllib.parse.urlsplit(clean_url).path.rstrip("/").split("/")[-1]) or "Calendar",
            "site": clean_url, "icon": None, "items": items[:1000], "complete": True}
