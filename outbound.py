"""Per-host pacing for outbound HTTP requests."""

import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import urlparse

MIN_INTERVAL = 1.0
USER_AGENT = "Mozilla/5.0 (compatible; ContentFeed/1.0)"  # for web pages, feeds and calendars
_condition = threading.Condition()
_hosts = {}


class _Response:
    def __init__(self, response, host):
        self._response, self._host, self._closed = response, host, False

    def __getattr__(self, name):
        return getattr(self._response, name)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    def close(self):
        if not self._closed:
            self._closed = True
            try:
                self._response.close()
            finally:
                _release(self._host)


def _release(host):
    with _condition:
        state = _hosts[host]
        state["active"] = False
        state["next"] = max(state["next"], time.monotonic() + MIN_INTERVAL)
        _condition.notify_all()


def _retry_after(value):
    if not value:
        return 0
    try:
        return max(0, min(3600, float(value)))
    except ValueError:
        try:
            return max(0, min(3600, (parsedate_to_datetime(value) - datetime.now(timezone.utc)).total_seconds()))
        except (TypeError, ValueError, OverflowError):
            return 0


def _open(request, timeout, redirect_handler):
    if redirect_handler is None:
        return urllib.request.urlopen(request, timeout=timeout)
    return urllib.request.build_opener(redirect_handler).open(request, timeout=timeout)


def urlopen(request, timeout=10, *, redirect_handler=None, paced=True):
    """Open one request at a time per host, with a one second gap and 429 cooldown. paced=False skips
    that, for tiny requests that must answer at once, such as checking whether a calendar changed."""
    if not paced:
        return _open(request, timeout, redirect_handler)
    url = request.full_url if hasattr(request, "full_url") else str(request)
    host = urlparse(url).netloc.lower()
    with _condition:
        state = _hosts.setdefault(host, {"active": False, "next": 0.0})
        while state["active"] or time.monotonic() < state["next"]:
            _condition.wait(max(0.01, state["next"] - time.monotonic()) if not state["active"] else None)
        state["active"] = True
    try:
        response = _open(request, timeout, redirect_handler)
    except urllib.error.HTTPError as error:
        with _condition:
            if error.code == 429:
                state["next"] = max(state["next"], time.monotonic() + _retry_after(error.headers.get("Retry-After")))
        _release(host)
        raise
    except BaseException:
        _release(host)
        raise
    return _Response(response, host)
