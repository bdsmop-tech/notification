"""Parse time from a single message: 9:05, 09:05, 14:30:00, 16 43, 9 5."""

from __future__ import annotations

import re
from datetime import time

_TIME_RE = re.compile(r"^\s*(\d{1,2}):(\d{2})(?::(\d{2}))?\s*$")
_SPACE_RE = re.compile(r"^\s*(\d{1,2})\s+(\d{1,2})(?:\s+(\d{2}))?\s*$")


def parse_time_one_line(text: str) -> time | None:
    s = text.strip()
    m = _TIME_RE.match(s)
    if m:
        h, mi, secg = int(m.group(1)), int(m.group(2)), m.group(3)
        sec = int(secg) if secg is not None else 0
        if h > 23 or mi > 59 or sec > 59:
            return None
        return time(h, mi, sec)
    m2 = _SPACE_RE.match(s)
    if m2:
        h, mi, secg = int(m2.group(1)), int(m2.group(2)), m2.group(3)
        sec = int(secg) if secg is not None else 0
        if h > 23 or mi > 59 or sec > 59:
            return None
        return time(h, mi, sec)
    return None
