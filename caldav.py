"""Read public iCalendar files and CalDAV collections (including Radicale)."""

import base64
import json
import os
import posixpath
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path

import outbound
from icalendar_parser import ICalendarError, parse_calendar_data, parse_calendar_resource

USER_AGENT = "Mozilla/5.0 (compatible; ContentFeed/1.0)"
TIMEOUT = 10
MAX_BYTES = 20_000_000
READ_ONLY_METHODS = {"GET", "PROPFIND", "REPORT"}
DAV = "DAV:"
CAL = "urn:ietf:params:xml:ns:caldav"


class CalDAVError(Exception):
    pass


class CredentialsInURL(CalDAVError):
    pass


def _origin(url):
    parsed = urllib.parse.urlsplit(url)
    return parsed.scheme.lower(), (parsed.hostname or "").lower(), parsed.port


class _SameOriginRedirect(urllib.request.HTTPRedirectHandler):
    """Keep Basic credentials on redirects only when scheme, host and port stay the same."""
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if _origin(req.full_url) != _origin(newurl):
            return None
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _url_and_auth(url, login=None):
    p = urllib.parse.urlsplit(url)
    if p.scheme not in ("http", "https") or not p.hostname:
        raise CalDAVError("That isn't a valid CalDAV web address.")
    if p.username is not None or p.password is not None:
        raise CredentialsInURL("Don't put CalDAV credentials in the subscription URL; store them in caldav.json.")
    try:
        p.port  # validate the port before making a request
    except ValueError as e:
        raise CalDAVError("That CalDAV address has an invalid port.") from e
    auth = None
    if login is not None:
        if (not isinstance(login, dict) or not isinstance(login.get("username"), str)
                or not login["username"] or not isinstance(login.get("password"), str)):
            raise CalDAVError("The login in caldav.json must include a username and password.")
        raw = f"{login['username']}:{login['password']}"
        auth = "Basic " + base64.b64encode(raw.encode()).decode()
    return url, auth


def _credential_key(url):
    """A credential is scoped to this exact collection address, ignoring fragments and host casing."""
    parsed = urllib.parse.urlsplit(url)
    if parsed.username is not None or parsed.password is not None:
        return None
    host = parsed.hostname or ""
    if ":" in host:
        host = f"[{host}]"
    try:
        port = parsed.port
    except ValueError:
        return None
    netloc = host + (f":{port}" if port else "")
    return urllib.parse.urlunsplit((parsed.scheme.lower(), netloc.lower(), parsed.path, parsed.query, ""))


def _normalized_path(path):
    """Normalize decoded path segments before applying collection credential scopes."""
    path = urllib.parse.unquote(path)
    if "\\" in path:
        return None
    return posixpath.normpath("/" + path.lstrip("/"))


def extract_url_login(url):
    """For migration only: separate credentials from a legacy credential-bearing subscription URL."""
    parsed = urllib.parse.urlsplit(url)
    if parsed.username is None and parsed.password is None:
        return url, None
    try:
        port = parsed.port
    except ValueError as e:
        raise CalDAVError("A saved CalDAV address has an invalid port.") from e
    host = parsed.hostname or ""
    if ":" in host:
        host = f"[{host}]"
    netloc = host + (f":{port}" if port else "")
    clean = urllib.parse.urlunsplit((parsed.scheme, netloc, parsed.path, parsed.query, parsed.fragment))
    login = {"username": urllib.parse.unquote(parsed.username or ""),
             "password": urllib.parse.unquote(parsed.password or "")}
    return clean, login


def login_for(url, logins):
    target = _credential_key(url)
    if target is None or not isinstance(logins, dict):
        return None
    target_parts = urllib.parse.urlsplit(target)
    matches = []
    for address, login in logins.items():
        scope = _credential_key(address)
        if scope is None or not isinstance(login, dict):
            continue
        scope_parts = urllib.parse.urlsplit(scope)
        if _origin(scope) != _origin(target) or (scope_parts.query and scope_parts.query != target_parts.query):
            continue
        scope_path = _normalized_path(scope_parts.path)
        target_path = _normalized_path(target_parts.path)
        if scope_path is None or target_path is None:
            continue
        in_scope = (scope_path == "/" or target_path == scope_path
                    or target_path.startswith(scope_path.rstrip("/") + "/"))
        if in_scope:
            matches.append((len(scope_path), login))
    return max(matches, key=lambda match: match[0])[1] if matches else None


def read_logins(path: Path):
    """Read service-side CalDAV credentials without exposing them through feed status."""
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}, None
    except (OSError, UnicodeDecodeError) as e:
        return {}, f"couldn't read {path}: {getattr(e, 'strerror', None) or e}"
    if os.name == "posix" and path.stat().st_mode & 0o077:
        return {}, f"{path} must not be accessible by group or others; set its mode to 600"
    if not text.strip():
        return {}, f"{path} is empty"
    try:
        logins = json.loads(text)
    except json.JSONDecodeError as e:
        return {}, f"{path} isn't valid JSON: {e.msg.lower()} at line {e.lineno}, column {e.colno}"
    if not isinstance(logins, dict):
        return {}, f"{path} should hold an object of calendar addresses"
    return logins, None


def _request(url, method="GET", body=None, headers=None, auth=None):
    if method not in READ_ONLY_METHODS:
        raise CalDAVError(f"CalDAV method {method} is not allowed; calendars are read-only.")
    h = {"User-Agent": USER_AGENT, **(headers or {})}
    if auth:
        h["Authorization"] = auth
    request = urllib.request.Request(url, data=body, headers=h, method=method)
    try:
        opener = _SameOriginRedirect() if auth else None
        with outbound.urlopen(request, timeout=TIMEOUT, redirect_handler=opener) as response:
            payload = response.read(MAX_BYTES + 1)
            if len(payload) > MAX_BYTES:
                raise CalDAVError("The calendar response is too large.")
            return payload, response.geturl(), response.headers
    except urllib.error.HTTPError as e:
        raise CalDAVError(f"CalDAV returned HTTP {e.code} from {url}") from e
    except OSError as e:
        raise CalDAVError(f"couldn't fetch calendar {url}: {e}") from e


def fetch(url, login=None):
    """Fetch an .ics URL or calendar collection using optional service-side Basic credentials."""
    clean_url, auth = _url_and_auth(url, login)
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
