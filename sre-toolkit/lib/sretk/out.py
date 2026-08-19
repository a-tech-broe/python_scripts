"""Terminal output: colour, headings, tables, status paint."""

from __future__ import annotations

import os
import re
import shutil
import sys
from typing import Iterable, Sequence

_COLOR = sys.stdout.isatty() and os.environ.get("NO_COLOR") is None

OK, WARN, CRIT, INFO, NODATA = "OK", "WARN", "CRIT", "INFO", "—"
SEVERITY = {NODATA: 0, INFO: 1, OK: 2, WARN: 3, CRIT: 4}
#: Order used when ranking findings — highest first.
RANK = {CRIT: 0, WARN: 1, INFO: 2, OK: 3, NODATA: 4}

_ANSI = re.compile(r"\033\[[0-9;]*m")


def color_enabled() -> bool:
    return _COLOR


def set_color(enabled: bool) -> None:
    global _COLOR
    _COLOR = enabled


def _c(text: str, code: str) -> str:
    return f"\033[{code}m{text}\033[0m" if _COLOR else text


def bold(t: str) -> str:
    return _c(t, "1")


def dim(t: str) -> str:
    return _c(t, "2")


def red(t: str) -> str:
    return _c(t, "31")


def green(t: str) -> str:
    return _c(t, "32")


def yellow(t: str) -> str:
    return _c(t, "33")


def blue(t: str) -> str:
    return _c(t, "34")


def cyan(t: str) -> str:
    return _c(t, "36")


def strip(text: str) -> str:
    """Length-safe: drop ANSI codes so padding maths works."""
    return _ANSI.sub("", text)


def width() -> int:
    return min(shutil.get_terminal_size((100, 24)).columns, 200)


def paint(status: str, text: str | None = None) -> str:
    text = status if text is None else text
    return {OK: green, WARN: yellow, CRIT: red, INFO: cyan, NODATA: dim}.get(status, str)(text)


def heading(text: str, rule: str = "=") -> None:
    line = rule * min(len(text) + 4, width())
    print(f"\n{bold(line)}\n{bold('  ' + text)}\n{bold(line)}")


def section(text: str, count: int | None = None) -> None:
    tail = dim(f" ({count})") if count is not None else ""
    print(f"\n{bold(text)}{tail}")


def kv(key: str, value: str, key_w: int = 16) -> None:
    print(f"  {dim(key.ljust(key_w))} {value}")


def bullet(text: str, status: str = INFO, indent: int = 2) -> None:
    glyph = {OK: "✓", WARN: "!", CRIT: "✗", INFO: "·", NODATA: "·"}.get(status, "·")
    print(" " * indent + paint(status, glyph) + " " + text)


def rule(char: str = "-", size: int | None = None) -> None:
    print(dim(char * (size or min(76, width()))))


def table(headers: Sequence[str], rows: Iterable[Sequence[str]], indent: int = 2) -> None:
    """Print an aligned table; cells may already contain colour codes."""
    body = [[str(c) for c in row] for row in rows]
    if not body:
        return
    cols = len(headers)
    widths = [len(h) for h in headers]
    for row in body:
        for i in range(cols):
            widths[i] = max(widths[i], len(strip(row[i])) if i < len(row) else 0)

    pad = " " * indent
    print(pad + dim("  ".join(h.upper().ljust(widths[i]) for i, h in enumerate(headers))).rstrip())
    for row in body:
        cells = []
        for i in range(cols):
            cell = row[i] if i < len(row) else ""
            cells.append(cell + " " * (widths[i] - len(strip(cell))))
        print((pad + "  ".join(cells)).rstrip())


SPARKS = "▁▂▃▄▅▆▇█"


def sparkline(values: Sequence[float], size: int = 12) -> str:
    pts = [v for v in values if v is not None]
    if len(pts) < 2 or size <= 0:
        return ""
    if len(pts) > size:
        step = len(pts) / size
        pts = [max(pts[int(i * step):max(int((i + 1) * step), int(i * step) + 1)])
               for i in range(size)]
    lo, hi = min(pts), max(pts)
    if hi == lo:
        return SPARKS[0] * len(pts) if hi == 0 else SPARKS[3] * len(pts)
    return "".join(SPARKS[min(7, int((v - lo) / (hi - lo) * 8))] for v in pts)


def fail(message: str, code: int = 2) -> None:
    print(red(message), file=sys.stderr)
    raise SystemExit(code)
