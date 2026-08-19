"""Look-back windows: parsing, labels, and CloudWatch period selection."""

from __future__ import annotations

import argparse
import re
from datetime import timedelta

_PATTERN = re.compile(r"(\d+)\s*([mhd])", re.IGNORECASE)


def parse(raw: str) -> timedelta:
    """`30m`, `6h`, `2d` -> timedelta. Raises argparse errors so it drops into `type=`."""
    match = _PATTERN.fullmatch(raw.strip().lower())
    if not match:
        raise argparse.ArgumentTypeError(f"window must look like 30m, 6h or 2d, got {raw!r}")
    amount, unit = int(match.group(1)), match.group(2)
    delta = {"m": timedelta(minutes=amount),
             "h": timedelta(hours=amount),
             "d": timedelta(days=amount)}[unit]
    if delta < timedelta(minutes=5):
        raise argparse.ArgumentTypeError("window must be at least 5m")
    if delta > timedelta(days=14):
        raise argparse.ArgumentTypeError("window must be 14d or less")
    return delta


def label(delta: timedelta) -> str:
    secs = int(delta.total_seconds())
    if secs % 86400 == 0:
        return f"{secs // 86400}d"
    if secs % 3600 == 0:
        return f"{secs // 3600}h"
    return f"{secs // 60}m"


def period(delta: timedelta, points: int = 60) -> int:
    """A CloudWatch-legal period giving roughly `points` datapoints."""
    target = delta.total_seconds() / points
    for candidate in (60, 300, 900, 3600, 21600):
        if target <= candidate:
            return candidate
    return 86400
