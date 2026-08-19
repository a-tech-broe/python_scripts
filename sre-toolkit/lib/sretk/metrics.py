"""CloudWatch metric reads, batched."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta
from typing import Sequence

from .aws import Aws, utc_window


@dataclass
class Query:
    """One metric to read for one target."""

    alias: str
    namespace: str
    metric: str
    stat: str
    dims: list[tuple[str, str]] = field(default_factory=list)


def fetch(aws: Aws, queries: Sequence[Query], window: timedelta,
          period: int) -> dict[str, list[float]]:
    """Read every query in one batched sweep; returns alias -> datapoints (oldest first).

    Aliases must be unique. Missing metrics come back as an empty list rather than
    raising, so callers can treat "no data" as its own state.
    """
    if not queries:
        return {}
    start, end = utc_window(window)
    out: dict[str, list[float]] = {q.alias: [] for q in queries}
    ids = {f"q{i}": q.alias for i, q in enumerate(queries)}

    payload = [
        {
            "Id": qid,
            "MetricStat": {
                "Metric": {
                    "Namespace": q.namespace,
                    "MetricName": q.metric,
                    "Dimensions": [{"Name": n, "Value": v} for n, v in q.dims],
                },
                "Period": period,
                "Stat": q.stat,
            },
            "ReturnData": True,
        }
        for qid, q in zip(ids, queries)
    ]

    cw = aws.client("cloudwatch")
    for i in range(0, len(payload), 100):
        chunk, token = payload[i:i + 100], None
        while True:
            kwargs = {"MetricDataQueries": chunk, "StartTime": start, "EndTime": end,
                      "ScanBy": "TimestampAscending"}
            if token:
                kwargs["NextToken"] = token
            resp = cw.get_metric_data(**kwargs)
            for result in resp.get("MetricDataResults", []):
                alias = ids.get(result["Id"])
                if alias:
                    out[alias].extend(result.get("Values", []) or [])
            token = resp.get("NextToken")
            if not token:
                break
    return out


def total(series: dict[str, list[float]], alias: str) -> float:
    return sum(series.get(alias) or [])


def mean(series: dict[str, list[float]], alias: str) -> float | None:
    values = series.get(alias) or []
    return sum(values) / len(values) if values else None


def peak(series: dict[str, list[float]], alias: str) -> float | None:
    values = series.get(alias) or []
    return max(values) if values else None


def rate_pct(series: dict[str, list[float]], numerator: str, denominator: str) -> float | None:
    bottom = total(series, denominator)
    if bottom <= 0:
        return None
    return total(series, numerator) / bottom * 100


def spike_index(values: Sequence[float]) -> int | None:
    """Index of the first datapoint that jumps well above the earlier baseline.

    Used to date an error spike so it can be lined up against deploys on the
    timeline. Returns None when the series is flat or too short to judge.
    """
    points = [v for v in values if v is not None]
    if len(points) < 4:
        return None
    for i in range(2, len(points)):
        baseline = points[:i]
        avg = sum(baseline) / len(baseline)
        if points[i] > max(avg * 3, avg + 1) and points[i] > 0:
            return i
    return None
