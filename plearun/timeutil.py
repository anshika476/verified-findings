"""Timestamp parsing.

Records mix four styles: epoch milliseconds, ISO 8601 with a UTC offset,
naive "YYYY-MM-DD HH:MM:SS" (no zone), and bare time of day "21:15".
Parsing is two-step: `classify` reads the value without guessing, and
`resolve` turns it into epoch seconds once the default zone and incident
date are known. Every result carries an uncertainty in seconds (its precision).
"""
import re
from collections import Counter
from datetime import date, datetime, time, timedelta, timezone

TOD_RE = re.compile(r"^\s*(\d{1,2}):(\d{2})(?::(\d{2}))?\s*$")
EPOCH_RE = re.compile(r"^\s*\d{9,13}(?:\.\d+)?\s*$")
NAIVE_FORMATS = ("%m/%d/%Y %H:%M:%S", "%m/%d/%Y %H:%M", "%d.%m.%Y %H:%M:%S")

MINUTE_UNC = 30.0   # "21:15" could be 21:14:30..21:15:30 if rounded
SECOND_UNC = 1.0


def classify(value):
    """Return (style, payload, uncertainty) or None if not a timestamp."""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return _epoch(float(value))
    if not isinstance(value, str):
        return None
    s = value.strip()
    if EPOCH_RE.match(s):
        return _epoch(float(s))
    m = TOD_RE.match(s)
    if m:
        h, mi, sec = int(m.group(1)), int(m.group(2)), m.group(3)
        if h > 23 or mi > 59 or (sec and int(sec) > 59):
            return None
        unc = SECOND_UNC if sec else MINUTE_UNC
        return ("tod", (h, mi, int(sec or 0)), unc)
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        dt = None
        for fmt in NAIVE_FORMATS:
            try:
                dt = datetime.strptime(s, fmt)
                break
            except ValueError:
                continue
        if dt is None:
            return None
    unc = SECOND_UNC if re.search(r"\d:\d{2}:\d{2}", s) else MINUTE_UNC
    return ("aware" if dt.tzinfo else "naive", dt, unc)


def _epoch(x):
    secs = x / 1000.0 if x > 1e11 else x        # 13 digits = milliseconds
    if not (9.0e8 < secs < 4.1e9):              # 1998..2099; anything else is junk
        return None
    return ("epoch", secs, SECOND_UNC)


def dominant_offset(classified):
    """Most common explicit UTC offset among aware timestamps (else None)."""
    offs = Counter(c[1].utcoffset() for c in classified if c and c[0] == "aware")
    return offs.most_common(1)[0][0] if offs else None


def resolve(c, tz, day, near=None):
    """Epoch seconds for a classified value.

    tz:   default timezone for naive values and times of day.
    day:  incident date for times of day.
    near: epoch seconds the value should be close to; a time of day is put on
          day-1, day or day+1, whichever is closest (handles midnight).
    """
    style, payload, _ = c
    if style == "epoch":
        return payload
    if style == "aware":
        return payload.timestamp()
    if style == "naive":
        return payload.replace(tzinfo=tz).timestamp()
    h, mi, sec = payload
    options = [datetime.combine(day + timedelta(days=k), time(h, mi, sec), tz).timestamp()
               for k in (-1, 0, 1)]
    if near is None:
        return options[1]
    return min(options, key=lambda t: abs(t - near))


def fmt(t, tz):
    return datetime.fromtimestamp(t, tz).strftime("%H:%M:%S")


def parse_date(text):
    m = re.search(r"\b(\d{4})-(\d{2})-(\d{2})\b", text or "")
    if not m:
        return None
    try:
        return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    except ValueError:
        return None


UTC = timezone.utc
