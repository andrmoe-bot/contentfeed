"""Read public iCalendar files and CalDAV collections (including Radicale)."""

import base64
import calendar
import math
import re
import time as clock_time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, time, timedelta, timezone
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
    try:
        port = p.port
    except ValueError as e:
        raise CalDAVError("That CalDAV address has an invalid port.") from e
    auth = None
    if p.username is not None:
        raw = f"{urllib.parse.unquote(p.username)}:{urllib.parse.unquote(p.password or '')}"
        auth = "Basic " + base64.b64encode(raw.encode()).decode()
        hostname = f"[{p.hostname}]" if ":" in p.hostname and not p.hostname.startswith("[") else p.hostname
        host = hostname + (f":{port}" if port else "")
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


def _date_prop(component, name):
    params, value = _prop(component, name)
    return _date(value, params)


def _occurrences(component, start, base_value="", base_params=""):
    """Expand common daily, weekly, monthly and yearly RRULEs for the next 90 days."""
    rules = dict(part.split("=", 1) for part in _prop(component, "RRULE")[1].upper().split(";") if "=" in part)
    freq = rules.get("FREQ")
    if not start or freq not in ("DAILY", "WEEKLY", "MONTHLY", "YEARLY"):
        return [start]
    lower, upper = clock_time.time() - 86400, clock_time.time() + 90 * 86400
    try:
        interval = max(1, int(rules.get("INTERVAL", "1")))
    except ValueError:
        return [start]
    if interval > 1_000_000:
        return [start] if lower <= start <= upper else []
    count_text = rules.get("COUNT", "")
    count = int(count_text) if count_text.isdigit() and len(count_text) < 10 else None
    until = _date("", "")
    if rules.get("UNTIL"):
        until = _date(rules["UNTIL"])
    dt = _datetime(base_value, base_params) or datetime.fromtimestamp(start, timezone.utc)
    lower_local = datetime.fromtimestamp(lower, dt.tzinfo)
    upper_local = datetime.fromtimestamp(upper, dt.tzinfo)
    out = []
    weekdays = ["MO", "TU", "WE", "TH", "FR", "SA", "SU"]
    byday_specs = []
    for token in rules.get("BYDAY", "").split(","):
        match = re.fullmatch(r"([+-]?\d{1,2})?(MO|TU|WE|TH|FR|SA|SU)", token)
        if match:
            ordinal = int(match.group(1)) if match.group(1) else None
            if ordinal:
                byday_specs.append((ordinal, weekdays.index(match.group(2))))
            elif ordinal is None:
                byday_specs.append((None, weekdays.index(match.group(2))))
    byday = sorted({weekday for _, weekday in byday_specs})

    def month_date(index):
        month = dt.year * 12 + (dt.month - 1) + index * interval
        year, month0 = divmod(month, 12)
        month = month0 + 1
        last_day = calendar.monthrange(year, month)[1]
        return dt.replace(year=year, month=month, day=dt.day) if dt.day <= last_day else None

    if freq == "DAILY" and byday:
        step_days = interval
        day_delta = (lower_local.date() - dt.date()).days
        first = max(0, day_delta // step_days - 1)
        last = max(first, (upper_local.date() - dt.date()).days // step_days + 1)
        cycle = 7 // math.gcd(step_days, 7)
        pattern = [((dt.weekday() + k * step_days) % 7) in byday for k in range(1, cycle + 1)]
        prior_periods, prior_days = divmod(max(0, first - 1), cycle)
        ordinal = (1 if first else 0) + prior_periods * sum(pattern) + sum(pattern[:prior_days])
        for index in range(first, min(last + 1, first + 100)):
            occurrence = dt + timedelta(days=index * step_days)
            stamp = occurrence.timestamp()
            if index and occurrence.weekday() not in byday:
                continue
            if stamp > upper or (until is not None and stamp > until) or (count is not None and ordinal >= count):
                break
            if stamp >= lower:
                out.append(stamp)
            ordinal += 1
    elif freq in ("DAILY", "WEEKLY") and not (freq == "WEEKLY" and byday):
        step_days = interval * (1 if freq == "DAILY" else 7)
        day_delta = (lower_local.date() - dt.date()).days
        first = max(0, day_delta // step_days - 1)
        last = max(first, (upper_local.date() - dt.date()).days // step_days + 1)
        stop = min(last + 1, first + 100, count if count is not None else last + 1)
        out = [(dt + timedelta(days=i * step_days)).timestamp() for i in range(first, stop)
               if lower <= (dt + timedelta(days=i * step_days)).timestamp() <= upper]
    elif freq == "MONTHLY" and byday_specs:
        months_apart = (lower_local.year - dt.year) * 12 + lower_local.month - dt.month
        months_until = (upper_local.year - dt.year) * 12 + upper_local.month - dt.month
        first = max(0, months_apart // interval - 1)
        last = max(first, months_until // interval + 1)
        ordinal = 0
        for index in range(0 if count is not None else first, last + 1):
            month_index = dt.year * 12 + dt.month - 1 + index * interval
            year, month0 = divmod(month_index, 12)
            month = month0 + 1
            matching = {}
            for nth, weekday in byday_specs:
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
                    break
                if stamp >= lower:
                    out.append(stamp)
                ordinal += 1
            else:
                continue
            break
    elif freq in ("MONTHLY", "YEARLY"):
        period = interval if freq == "MONTHLY" else 12 * interval
        months_apart = (lower_local.year - dt.year) * 12 + lower_local.month - dt.month
        first = max(0, months_apart // period - 1)
        months_until = (upper_local.year - dt.year) * 12 + upper_local.month - dt.month
        last = max(first, months_until // period + 1)
        ordinal = 0
        for index in range(first):
            if month_date(index if freq == "MONTHLY" else index * 12) is not None:
                ordinal += 1
        for index in range(first, min(first + 100, last + 1)):
            occurrence = month_date(index if freq == "MONTHLY" else index * 12)
            if occurrence is None:
                continue
            stamp = occurrence.timestamp()
            if stamp > upper or (count is not None and ordinal >= count):
                break
            if stamp >= lower:
                out.append(stamp)
            ordinal += 1
    else:  # weekly with BYDAY
        selected = sorted(byday or [dt.weekday()])
        start_is_selected = dt.weekday() in selected
        anchor = dt - timedelta(days=dt.weekday())
        week_index = max(0, (lower_local.date() - anchor.date()).days // (7 * interval) - 1)
        last_week = max(week_index, (upper_local.date() - anchor.date()).days // (7 * interval) + 1)
        if (not start_is_selected and start >= lower and start <= upper
                and (until is None or start <= until) and (count is None or count > 0)):
            out.append(start)
        for week in range(week_index, min(week_index + 100, last_week + 1)):
            for weekday in selected:
                occurrence = anchor + timedelta(days=week * 7 * interval + weekday)
                stamp = occurrence.timestamp()
                if stamp < start:
                    continue
                index = selected.index(weekday)
                first_week_count = sum(day >= dt.weekday() for day in selected) + (not start_is_selected)
                ordinal = (index - sum(day < dt.weekday() for day in selected) + (not start_is_selected) if week == 0
                           else first_week_count + (week - 1) * len(selected) + index)
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
    components = list(_components(calendar_data))
    overrides = set()
    for component in components:
        recurrence = _date_prop(component, "RECURRENCE-ID")
        if recurrence is not None:
            overrides.add((_prop(component, "UID")[1], recurrence))
    for comp in components:
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
        excluded = {_date(part, params) for params, value in comp.get("EXDATE", []) for part in value.split(",")}
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
                "deadline": item_deadline, "calendar_uid": uid,
            })
    return out


def _calendar_data(body, resource_url):
    text = body.decode("utf-8-sig", errors="replace")
    if not re.search(r"(?im)^BEGIN:VCALENDAR\s*$", text) or not re.search(r"(?im)^END:VCALENDAR\s*$", text):
        raise CalDAVError("The .ics response is not a complete iCalendar object.")
    items = _items(text, resource_url)
    items.sort(key=lambda item: item.get("published") or 0, reverse=True)
    return {"title": urllib.parse.unquote(urllib.parse.urlsplit(resource_url).path.rstrip("/").split("/")[-1]) or "Calendar",
            "site": resource_url, "icon": None, "items": items[:1000], "complete": True}


def fetch(url):
    """Fetch an .ics URL or query a CalDAV collection. URL userinfo supplies Basic auth."""
    clean_url, auth = _url_and_auth(url)
    path = urllib.parse.urlsplit(clean_url).path
    # Shared iCalendar files are plain GET URLs. Probe only .ics paths so RSS
    # subscriptions do not incur a DAV request on every refresh.
    if path.lower().endswith(".ics") and not path.endswith("/"):
        try:
            body, final_url, headers = _request(clean_url, headers={"Accept": "text/calendar, */*"}, auth=auth)
            if body.lstrip().startswith(b"BEGIN:VCALENDAR") or "text/calendar" in headers.get("Content-Type", "").lower():
                return _calendar_data(body, final_url)
        except CalDAVError:
            pass  # Some CalDAV collection URLs also end in .ics.

    propfind = f'''<?xml version="1.0" encoding="utf-8"?>
<D:propfind xmlns:D="{DAV}"><D:prop><D:resourcetype/><D:displayname/></D:prop></D:propfind>'''.encode()
    body, _, _ = _request(clean_url, "PROPFIND", propfind,
        {"Depth": "0", "Content-Type": "application/xml; charset=utf-8", "Accept": "application/xml"}, auth)
    try:
        root = ET.fromstring(body)
    except ET.ParseError as e:
        raise CalDAVError("The CalDAV server returned invalid XML.") from e
    resources = root.findall(f".//{{{DAV}}}resourcetype")
    if not any(resource.find(f"{{{CAL}}}calendar") is not None for resource in resources):
        raise CalDAVError("That address is not a CalDAV calendar collection or .ics file.")
    display = root.find(f".//{{{DAV}}}displayname")
    title = display.text if display is not None else None

    items = []
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
    items.sort(key=lambda item: item.get("published") or 0, reverse=True)
    return {"title": title or urllib.parse.unquote(urllib.parse.urlsplit(clean_url).path.rstrip("/").split("/")[-1]) or "Calendar",
            "site": clean_url, "icon": None, "items": items[:1000], "complete": True}
