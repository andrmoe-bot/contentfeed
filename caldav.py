"""Read public iCalendar files and CalDAV collections (including Radicale)."""

import base64
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

import outbound
from icalendar_parser import ICalendarError, parse_calendar_data, parse_calendar_resource

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
                return _parse_calendar_data(body, final_url)
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
                items.extend(parse_calendar_resource(data.text, resource_url))
    items.sort(key=lambda item: item.get("published") or 0, reverse=True)
    return {"title": title or urllib.parse.unquote(urllib.parse.urlsplit(clean_url).path.rstrip("/").split("/")[-1]) or "Calendar",
            "site": clean_url, "icon": None, "items": items[:1000], "complete": True}


def _parse_calendar_data(body, resource_url):
    try:
        return parse_calendar_data(body, resource_url)
    except ICalendarError as e:
        raise CalDAVError(str(e)) from e
