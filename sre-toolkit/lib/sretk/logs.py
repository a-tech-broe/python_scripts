"""CloudWatch Logs: group discovery, Insights queries, error clustering.

This is the Splunk-shaped seam. Everything here returns plain dicts/Findings, so a
Splunk or Loki backend can be added later by implementing the same three calls
(`find_groups`, `error_histogram`, `top_errors`) without touching the callers.
"""

from __future__ import annotations

import re
import sys
import time
from datetime import datetime, timedelta, timezone
from typing import Any

from .aws import Aws, AwsError, utc_window

#: Lines worth surfacing during an incident. Deliberately word-based: an earlier
#: version matched a bare `5\d{2}` for HTTP 5xx and happily classified Postgres
#: "distance=65536 kB" checkpoint lines as errors.
#: The second alternative is case-sensitive on purpose: `\bexception\b` cannot match
#: the tail of `NullPointerException`, so every Java/C# class name was slipping past.
ERROR_PATTERN = (r"(?i)\b(error|errors|exception|fatal|panic|traceback|timeout|timed out|"
                 r"refused|denied|unavailable|unreachable|critical|severe|crash|"
                 r"failed|failure|oom|oomkilled|killed)\b"
                 r"|(?-i:\b\w+(?:Exception|Error)\b)")

#: Structured logs state their own severity, and that beats a keyword guess: an INFO
#: line carrying `"failed": 0` in its payload is not an error. Excluded server-side.
NOISE_PATTERN = r'(?i)"level"\s*:\s*"(INFO|DEBUG|TRACE)"|\blevel=(info|debug|trace)\b'


def _filter_clause(pattern: str | None = None) -> str:
    return (f"filter @message like /{pattern or ERROR_PATTERN}/"
            f" and @message not like /{NOISE_PATTERN}/")

#: Bits of a log line that differ per occurrence — stripped so that otherwise
#: identical errors cluster into one row instead of thousands.
_NOISE = [
    (re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", re.I), "<uuid>"),
    (re.compile(r"\b\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:\.\d+)?Z?\b"), "<ts>"),
    (re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b"), "<ip>"),
    (re.compile(r"\b0x[0-9a-f]+\b", re.I), "<hex>"),
    (re.compile(r"\b[0-9A-F]{2,}/[0-9A-F]{6,}\b"), "<lsn>"),   # WAL positions, offsets
    (re.compile(r"\b\d+\.\d+\b"), "<num>"),                    # durations, percentages
    (re.compile(r"\b\d{3,}\b"), "<num>"),                      # counts, sizes, pids
    (re.compile(r"\b[A-Za-z0-9+/]{24,}={0,2}\b"), "<token>"),
    (re.compile(r"<num>(?:\s*<num>)+"), "<num>"),              # collapse runs
]


def fingerprint(message: str) -> str:
    """Collapse a log line to its shape so repeats group together."""
    text = message.strip()
    for pattern, replacement in _NOISE:
        text = pattern.sub(replacement, text)
    return re.sub(r"\s+", " ", text)[:240]


def find_groups(aws: Aws, service: str, limit: int = 8) -> list[str]:
    """Log groups that plausibly belong to `service`, best match first."""
    needle = service.lower()
    scored: list[tuple[int, str]] = []
    try:
        for group in aws.paginate("logs", "describe_log_groups", "logGroups"):
            name = group["logGroupName"]
            lower = name.lower()
            if needle not in lower:
                continue
            # Prefer the service's own group over something that merely mentions it.
            score = 0
            if lower.endswith(needle) or lower.endswith(needle + "/"):
                score -= 3
            if any(lower.startswith(p) for p in ("/aws/lambda/", "/ecs/", "/aws/ecs/")):
                score -= 2
            score += len(name)
            scored.append((score, name))
    except AwsError:
        return []
    scored.sort()
    return [name for _, name in scored[:limit]]


def _run_insights(aws: Aws, groups: list[str], query: str, window: timedelta,
                  limit: int = 100, timeout: int = 60) -> list[dict[str, str]]:
    """Run a Logs Insights query and wait for it, politely."""
    if not groups:
        return []
    start, end = utc_window(window)
    client = aws.client("logs")
    started = client.start_query(
        logGroupNames=groups[:20],
        startTime=int(start.timestamp()),
        endTime=int(end.timestamp()),
        queryString=query,
        limit=limit,
    )
    query_id = started["queryId"]
    deadline = time.time() + timeout
    try:
        while time.time() < deadline:
            result = client.get_query_results(queryId=query_id)
            status = result.get("status")
            if status == "Complete":
                return [{f["field"]: f["value"] for f in row}
                        for row in result.get("results", [])]
            if status in ("Failed", "Cancelled", "Timeout"):
                # Never let a broken query masquerade as "no errors found" — that is
                # the one wrong answer this tool must not give during an incident.
                print(f"warning: log query {status.lower()} — results are incomplete",
                      file=sys.stderr)
                return []
            time.sleep(1)
        client.stop_query(queryId=query_id)
        print("warning: log query timed out — results are incomplete", file=sys.stderr)
    except AwsError as exc:
        print(f"warning: log query failed — {exc}", file=sys.stderr)
        return []
    return []


def error_histogram(aws: Aws, groups: list[str], window: timedelta,
                    bin_minutes: int = 5) -> list[tuple[datetime, float]]:
    """Error-ish lines per time bucket, oldest first."""
    rows = _run_insights(
        aws, groups,
        f"{_filter_clause()}"
        f" | stats count() as hits by bin({bin_minutes}m) as bucket"
        f" | sort bucket asc",
        window, limit=500,
    )
    out = []
    for row in rows:
        stamp = row.get("bucket")
        if not stamp:
            continue
        try:
            when = datetime.fromisoformat(stamp.replace(" ", "T")).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
        out.append((when, float(row.get("hits", 0))))
    return out


def top_errors(aws: Aws, groups: list[str], window: timedelta,
               limit: int = 200) -> list[dict[str, Any]]:
    """Most frequent distinct error shapes, with a sample line and first/last seen."""
    rows = _run_insights(
        aws, groups,
        f"{_filter_clause()}"
        f" | fields @timestamp, @message"
        f" | sort @timestamp desc",
        window, limit=limit,
    )
    clusters: dict[str, dict[str, Any]] = {}
    for row in rows:
        message = row.get("@message", "").strip()
        if not message:
            continue
        key = fingerprint(message)
        entry = clusters.setdefault(key, {"shape": key, "count": 0, "sample": message,
                                          "first": None, "last": None})
        entry["count"] += 1
        stamp = row.get("@timestamp")
        if stamp:
            try:
                when = datetime.fromisoformat(stamp.replace(" ", "T")).replace(tzinfo=timezone.utc)
            except ValueError:
                when = None
            if when:
                entry["first"] = min(entry["first"] or when, when)
                entry["last"] = max(entry["last"] or when, when)
    return sorted(clusters.values(), key=lambda c: -c["count"])


def sample_lines(aws: Aws, groups: list[str], window: timedelta,
                 pattern: str | None = None, limit: int = 20) -> list[str]:
    """Raw recent lines, for when you just want to read the log."""
    filter_clause = _filter_clause(pattern)
    rows = _run_insights(
        aws, groups,
        f"{filter_clause} | fields @timestamp, @message | sort @timestamp desc",
        window, limit=limit,
    )
    return [f"{r.get('@timestamp', '')}  {r.get('@message', '').strip()}" for r in rows]
