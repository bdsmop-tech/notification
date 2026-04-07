"""Parse time from a single message: 9:05, 09:05, 14:30:00."""

from __future__ import annotations

import re
from datetime import time

_TIME_RE = re.compile(r"^\s*(\d{1,2}):(\d{2})(?::(\d{2}))?\s*$")


def parse_time_one_line(text: str) -> time | None:
    m = _TIME_RE.match(text.strip())
    if not m:
        return None
    h, mi, s = int(m.group(1)), int(m.group(2)), m.group(3)
    sec = int(s) if s is not None else 0
    if h > 23 or mi > 59 or sec > 59:
        return None
    return time(h, mi, sec)
