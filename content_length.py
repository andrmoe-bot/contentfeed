"""Content duration estimates, stored in minutes; absent estimates stay unknown."""

import json
import math
import re


def duration_minutes(value):
    """Parse seconds, HH:MM:SS / MM:SS, or an ISO 8601 day/time duration."""
    if isinstance(value, bool) or value is None:
        return None
    try:
        text = str(value).strip()
        if text.startswith('P'):
            match = re.fullmatch(r'P(?:(\d+(?:\.\d+)?)D)?(?:T(?:(\d+(?:\.\d+)?)H)?(?:(\d+(?:\.\d+)?)M)?(?:(\d+(?:\.\d+)?)S)?)?', text)
            if not match or not any(match.groups()):
                return None
            days, hours, minutes, seconds = (float(n or 0) for n in match.groups())
            seconds += days * 86400 + hours * 3600 + minutes * 60
        elif ':' in text:
            parts = text.split(':')
            if len(parts) not in (2, 3):
                return None
            seconds = 0
            for part in parts:
                seconds = seconds * 60 + float(part)
        else:
            seconds = float(text)
        return seconds / 60 if math.isfinite(seconds) and seconds > 0 else None
    except (ValueError, TypeError, OverflowError):
        return None


def reading_minutes(text):
    words = len(re.findall(r'\b\w+\b', text))
    return max(1, words / 200) if words else None


def page_minutes(parser, video=False):
    for key in ('duration', 'video:duration', 'og:video:duration'):
        duration = duration_minutes(parser.meta.get(key))
        if duration is not None:
            return duration

    def durations(obj):
        if isinstance(obj, dict):
            if 'duration' in obj:
                yield obj['duration']
            for value in obj.values():
                yield from durations(value)
        elif isinstance(obj, list):
            for value in obj:
                yield from durations(value)

    for block in parser.json_ld:
        try:
            for value in durations(json.loads(block)):
                duration = duration_minutes(value)
                if duration is not None:
                    return duration
        except (ValueError, TypeError):
            pass
    if not video and not parser.video:
        return reading_minutes(' '.join(parser.text))
    return None
