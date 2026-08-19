#!/usr/bin/env python3
"""
golden_signals.py — check the four SRE golden signals across an AWS region.

Latency · Traffic · Errors · Saturation, pulled from CloudWatch for everything the
region is running, graded against per-service thresholds, and summarised as OK / WARN /
CRIT with an exit code you can wire into cron or CI.

    python3 golden_signals.py --region us-east-1
    python3 golden_signals.py -r us-east-1 --window 24h --kind lambda --kind alb
    python3 golden_signals.py -r us-east-1 --json | jq '.[] | select(.status=="CRIT")'
    python3 golden_signals.py -r us-east-1 --watch 60
    python3 golden_signals.py -r us-east-1 --thresholds prod.json --fail-on warn

Exit codes: 0 all clear · 1 at least one WARN · 2 at least one CRIT (see --fail-on).

Signals that a service does not publish with a usable dimension set are shown as "—"
rather than guessed at; `--explain` prints exactly which metric backs each column.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import json
import math
import os
import re
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Iterator, Sequence

try:
    import boto3
    from botocore.config import Config
    from botocore.exceptions import BotoCoreError, ClientError, NoCredentialsError
except ImportError:  # pragma: no cover
    sys.exit("boto3 is required:  pip install boto3")


SIGNALS = ("latency", "traffic", "errors", "saturation")
OK, WARN, CRIT, NODATA = "OK", "WARN", "CRIT", "—"
SEVERITY = {NODATA: 0, OK: 1, WARN: 2, CRIT: 3}

# --------------------------------------------------------------------------- #
# Terminal helpers
# --------------------------------------------------------------------------- #

_COLOR = sys.stdout.isatty() and os.environ.get("NO_COLOR") is None


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


def cyan(t: str) -> str:
    return _c(t, "36")


def paint(status: str, text: str) -> str:
    return {OK: green, WARN: yellow, CRIT: red, NODATA: dim}[status](text)


SPARKS = "▁▂▃▄▅▆▇█"


def sparkline(values: Sequence[float], width: int = 20) -> str:
    """Compact ASCII trend for a series, newest on the right."""
    pts = [v for v in values if v is not None]
    if len(pts) < 2:
        return ""
    if len(pts) > width:  # bucket down to `width` columns
        size = len(pts) / width
        pts = [max(pts[int(i * size):max(int((i + 1) * size), int(i * size) + 1)])
               for i in range(width)]
    lo, hi = min(pts), max(pts)
    if math.isclose(lo, hi):
        return SPARKS[0] * len(pts) if hi == 0 else SPARKS[3] * len(pts)
    span = hi - lo
    return "".join(SPARKS[min(len(SPARKS) - 1, int((v - lo) / span * len(SPARKS)))] for v in pts)


# --------------------------------------------------------------------------- #
# Formatting
# --------------------------------------------------------------------------- #


def fmt_ms(v: float) -> str:
    if v >= 1000:
        return f"{v / 1000:.2f} s"
    return f"{v:.0f} ms" if v >= 10 else f"{v:.1f} ms"


def fmt_pct(v: float) -> str:
    return f"{v:.2f}%" if 0 < v < 1 else f"{v:.1f}%"


def fmt_rate(v: float) -> str:
    if v >= 60:
        return f"{v / 60:.1f}/s"
    return f"{v:.1f}/min" if v >= 0.1 else f"{v:.2f}/min"


def fmt_count(v: float) -> str:
    for unit, size in (("B", 1e9), ("M", 1e6), ("k", 1e3)):
        if abs(v) >= size:
            return f"{v / size:.1f}{unit}"
    return f"{v:.0f}"


def fmt_secs(v: float) -> str:
    if v >= 3600:
        return f"{v / 3600:.1f} h"
    if v >= 60:
        return f"{v / 60:.0f} min"
    return f"{v:.0f} s"


def fmt_bytes_rate(v: float) -> str:
    for unit, size in (("GB/s", 1e9), ("MB/s", 1e6), ("kB/s", 1e3)):
        if v >= size:
            return f"{v / size:.1f} {unit}"
    return f"{v:.0f} B/s"


# --------------------------------------------------------------------------- #
# Series helpers used by the reducers
# --------------------------------------------------------------------------- #

Series = dict[str, list[float]]


def total(s: Series, key: str) -> float:
    return sum(s.get(key) or [])


def mean(s: Series, key: str) -> float | None:
    vals = s.get(key) or []
    return sum(vals) / len(vals) if vals else None


def peak(s: Series, key: str) -> float | None:
    vals = s.get(key) or []
    return max(vals) if vals else None


def ratio_pct(s: Series, num: str, den: str) -> float | None:
    bottom = total(s, den)
    if bottom <= 0:
        return None
    return total(s, num) / bottom * 100


def has_any(s: Series, *keys: str) -> bool:
    return any(s.get(k) for k in keys)


# --------------------------------------------------------------------------- #
# Model
# --------------------------------------------------------------------------- #


@dataclass
class SignalDef:
    """One column for one service kind."""

    label: str                                   # what the number means, e.g. "p95"
    metrics: dict[str, tuple[str, str, str]]     # alias -> (namespace, metric, stat)
    reduce: Callable[[Series], float | None]
    fmt: Callable[[float], str]
    thresholds: tuple[float, float] | None = None   # (warn, crit); None => informational
    spark: str | None = None                        # alias to draw the trend from
    lower_is_worse: bool = False

    def grade(self, value: float | None, override: tuple[float, float] | None) -> str:
        limits = override or self.thresholds
        if value is None:
            return NODATA
        if not limits:
            return OK
        warn, crit = limits
        if self.lower_is_worse:
            return CRIT if value <= crit else WARN if value <= warn else OK
        return CRIT if value >= crit else WARN if value >= warn else OK


@dataclass
class Target:
    """One thing being watched: a function, a load balancer, a queue…"""

    kind: str
    id: str
    name: str = ""
    dims: list[tuple[str, str]] = field(default_factory=list)
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def display(self) -> str:
        return self.name or self.id


@dataclass
class KindDef:
    key: str
    label: str
    discover: Callable[["Ctx"], list[Target]]
    signals: dict[str, SignalDef]


@dataclass
class Reading:
    value: float | None
    status: str
    text: str
    trend: list[float] = field(default_factory=list)  # series the sparkline is drawn from


@dataclass
class Row:
    target: Target
    readings: dict[str, Reading]

    @property
    def status(self) -> str:
        worst = max((r.status for r in self.readings.values()),
                    key=lambda s: SEVERITY[s], default=NODATA)
        return worst


KINDS: dict[str, KindDef] = {}


def register(key: str, label: str, signals: dict[str, SignalDef]):
    def outer(discover: Callable[["Ctx"], list[Target]]):
        KINDS[key] = KindDef(key, label, discover, signals)
        return discover

    return outer


class Ctx:
    def __init__(self, session: "boto3.Session", region: str, args: argparse.Namespace):
        self.session = session
        self.region = region
        self.args = args
        self.window: timedelta = args.window
        self.period: int = args.period
        self._clients: dict[str, Any] = {}
        self._cfg = Config(region_name=region, retries={"max_attempts": 6, "mode": "standard"})

    def client(self, service: str):
        if service not in self._clients:
            self._clients[service] = self.session.client(service, config=self._cfg)
        return self._clients[service]

    @property
    def window_minutes(self) -> float:
        return self.window.total_seconds() / 60


def paginate(client, op: str, key: str, **kwargs) -> Iterator[dict]:
    if client.can_paginate(op):
        for page in client.get_paginator(op).paginate(**kwargs):
            yield from page.get(key, []) or []
    else:
        yield from getattr(client, op)(**kwargs).get(key, []) or []


# =========================================================================== #
# Service definitions
# =========================================================================== #

# --- Application Load Balancers -------------------------------------------- #


@register("alb", "Application Load Balancers", {
    "latency": SignalDef(
        "p95", {"rt": ("AWS/ApplicationELB", "TargetResponseTime", "p95")},
        lambda s: (lambda v: v * 1000 if v is not None else None)(mean(s, "rt")),
        fmt_ms, (0.5 * 1000, 2 * 1000), spark="rt"),
    "traffic": SignalDef(
        "requests", {"req": ("AWS/ApplicationELB", "RequestCount", "Sum")},
        lambda s: total(s, "req") or None, fmt_count, None, spark="req"),
    "errors": SignalDef(
        "5xx rate",
        {"req": ("AWS/ApplicationELB", "RequestCount", "Sum"),
         "e5": ("AWS/ApplicationELB", "HTTPCode_Target_5XX_Count", "Sum"),
         "lb5": ("AWS/ApplicationELB", "HTTPCode_ELB_5XX_Count", "Sum")},
        lambda s: (None if total(s, "req") <= 0
                   else (total(s, "e5") + total(s, "lb5")) / total(s, "req") * 100),
        fmt_pct, (1.0, 5.0), spark="e5"),
    "saturation": SignalDef(
        "unhealthy targets",
        {"un": ("AWS/ApplicationELB", "UnHealthyHostCount", "Maximum"),
         "up": ("AWS/ApplicationELB", "HealthyHostCount", "Maximum")},
        lambda s: (None if not has_any(s, "un", "up")
                   else (peak(s, "un") or 0) / max((peak(s, "un") or 0) + (peak(s, "up") or 0), 1) * 100),
        fmt_pct, (1.0, 50.0), spark="un"),
})
def _discover_alb(ctx: Ctx) -> list[Target]:
    out = []
    for lb in paginate(ctx.client("elbv2"), "describe_load_balancers", "LoadBalancers"):
        if lb.get("Type") != "application":
            continue
        # CloudWatch wants the "app/name/id" tail of the ARN, not the whole thing.
        suffix = lb["LoadBalancerArn"].split(":loadbalancer/", 1)[-1]
        out.append(Target("alb", lb["LoadBalancerName"], lb["LoadBalancerName"],
                          [("LoadBalancer", suffix)]))
    return out


# --- Network Load Balancers ------------------------------------------------ #


@register("nlb", "Network Load Balancers", {
    "latency": SignalDef("n/a", {}, lambda s: None, fmt_ms),
    "traffic": SignalDef(
        "flows", {"f": ("AWS/NetworkELB", "ActiveFlowCount", "Average")},
        lambda s: mean(s, "f"), fmt_count, None, spark="f"),
    "errors": SignalDef(
        "target resets", {"r": ("AWS/NetworkELB", "TCP_Target_Reset_Count", "Sum")},
        lambda s: total(s, "r") or None, fmt_count, (100, 1000), spark="r"),
    "saturation": SignalDef(
        "unhealthy targets",
        {"un": ("AWS/NetworkELB", "UnHealthyHostCount", "Maximum"),
         "up": ("AWS/NetworkELB", "HealthyHostCount", "Maximum")},
        lambda s: (None if not has_any(s, "un", "up")
                   else (peak(s, "un") or 0) / max((peak(s, "un") or 0) + (peak(s, "up") or 0), 1) * 100),
        fmt_pct, (1.0, 50.0), spark="un"),
})
def _discover_nlb(ctx: Ctx) -> list[Target]:
    out = []
    for lb in paginate(ctx.client("elbv2"), "describe_load_balancers", "LoadBalancers"):
        if lb.get("Type") != "network":
            continue
        suffix = lb["LoadBalancerArn"].split(":loadbalancer/", 1)[-1]
        out.append(Target("nlb", lb["LoadBalancerName"], lb["LoadBalancerName"],
                          [("LoadBalancer", suffix)]))
    return out


# --- Lambda ---------------------------------------------------------------- #


@register("lambda", "Lambda Functions", {
    "latency": SignalDef(
        "p95", {"d": ("AWS/Lambda", "Duration", "p95")},
        lambda s: mean(s, "d"), fmt_ms, (1000, 5000), spark="d"),
    "traffic": SignalDef(
        "invocations", {"i": ("AWS/Lambda", "Invocations", "Sum")},
        lambda s: total(s, "i") or None, fmt_count, None, spark="i"),
    "errors": SignalDef(
        "error rate",
        {"i": ("AWS/Lambda", "Invocations", "Sum"), "e": ("AWS/Lambda", "Errors", "Sum")},
        lambda s: ratio_pct(s, "e", "i"), fmt_pct, (1.0, 5.0), spark="e"),
    "saturation": SignalDef(
        "throttle rate",
        {"i": ("AWS/Lambda", "Invocations", "Sum"), "t": ("AWS/Lambda", "Throttles", "Sum")},
        lambda s: (None if total(s, "i") + total(s, "t") <= 0
                   else total(s, "t") / (total(s, "i") + total(s, "t")) * 100),
        fmt_pct, (0.5, 2.0), spark="t"),
})
def _discover_lambda(ctx: Ctx) -> list[Target]:
    return [Target("lambda", f["FunctionName"], f["FunctionName"],
                   [("FunctionName", f["FunctionName"])])
            for f in paginate(ctx.client("lambda"), "list_functions", "Functions")]


# --- API Gateway (REST) ---------------------------------------------------- #


@register("apigw", "API Gateway (REST)", {
    "latency": SignalDef(
        "p95", {"l": ("AWS/ApiGateway", "Latency", "p95")},
        lambda s: mean(s, "l"), fmt_ms, (1000, 3000), spark="l"),
    "traffic": SignalDef(
        "requests", {"c": ("AWS/ApiGateway", "Count", "Sum")},
        lambda s: total(s, "c") or None, fmt_count, None, spark="c"),
    "errors": SignalDef(
        "5xx rate",
        {"c": ("AWS/ApiGateway", "Count", "Sum"), "e": ("AWS/ApiGateway", "5XXError", "Sum")},
        lambda s: ratio_pct(s, "e", "c"), fmt_pct, (1.0, 5.0), spark="e"),
    "saturation": SignalDef(
        "4xx rate",
        {"c": ("AWS/ApiGateway", "Count", "Sum"), "e": ("AWS/ApiGateway", "4XXError", "Sum")},
        lambda s: ratio_pct(s, "e", "c"), fmt_pct, (10.0, 40.0), spark="e"),
})
def _discover_apigw(ctx: Ctx) -> list[Target]:
    return [Target("apigw", a["id"], a.get("name", a["id"]), [("ApiName", a.get("name", a["id"]))])
            for a in paginate(ctx.client("apigateway"), "get_rest_apis", "items")]


# --- RDS ------------------------------------------------------------------- #


@register("rds", "RDS Instances", {
    "latency": SignalDef(
        "read+write", {"r": ("AWS/RDS", "ReadLatency", "Average"),
                       "w": ("AWS/RDS", "WriteLatency", "Average")},
        lambda s: (None if not has_any(s, "r", "w")
                   else ((mean(s, "r") or 0) + (mean(s, "w") or 0)) * 1000),
        fmt_ms, (20, 100), spark="r"),
    "traffic": SignalDef(
        "connections", {"c": ("AWS/RDS", "DatabaseConnections", "Average")},
        lambda s: mean(s, "c"), fmt_count, None, spark="c"),
    "errors": SignalDef("n/a", {}, lambda s: None, fmt_count),
    "saturation": SignalDef(
        "peak CPU", {"cpu": ("AWS/RDS", "CPUUtilization", "Average")},
        lambda s: peak(s, "cpu"), fmt_pct, (75, 90), spark="cpu"),
})
def _discover_rds(ctx: Ctx) -> list[Target]:
    return [Target("rds", db["DBInstanceIdentifier"], db["DBInstanceIdentifier"],
                   [("DBInstanceIdentifier", db["DBInstanceIdentifier"])])
            for db in paginate(ctx.client("rds"), "describe_db_instances", "DBInstances")
            if db.get("DBInstanceStatus") == "available"]


# --- DynamoDB -------------------------------------------------------------- #


@register("dynamodb", "DynamoDB Tables", {
    # SuccessfulRequestLatency is only published per Operation, so there is no
    # table-wide latency number to show without fanning out per operation.
    "latency": SignalDef("n/a", {}, lambda s: None, fmt_ms),
    "traffic": SignalDef(
        "consumed RCU+WCU",
        {"r": ("AWS/DynamoDB", "ConsumedReadCapacityUnits", "Sum"),
         "w": ("AWS/DynamoDB", "ConsumedWriteCapacityUnits", "Sum")},
        lambda s: (total(s, "r") + total(s, "w")) or None, fmt_count, None, spark="r"),
    "errors": SignalDef(
        "system errors", {"e": ("AWS/DynamoDB", "SystemErrors", "Sum")},
        lambda s: total(s, "e") or None, fmt_count, (1, 10), spark="e"),
    "saturation": SignalDef(
        "throttles",
        {"rt": ("AWS/DynamoDB", "ReadThrottleEvents", "Sum"),
         "wt": ("AWS/DynamoDB", "WriteThrottleEvents", "Sum")},
        lambda s: (total(s, "rt") + total(s, "wt")) or None, fmt_count, (1, 50), spark="rt"),
})
def _discover_dynamodb(ctx: Ctx) -> list[Target]:
    return [Target("dynamodb", name, name, [("TableName", name)])
            for name in paginate(ctx.client("dynamodb"), "list_tables", "TableNames")]


# --- SQS ------------------------------------------------------------------- #


@register("sqs", "SQS Queues", {
    "latency": SignalDef(
        "oldest message", {"a": ("AWS/SQS", "ApproximateAgeOfOldestMessage", "Maximum")},
        lambda s: peak(s, "a"), fmt_secs, (300, 3600), spark="a"),
    "traffic": SignalDef(
        "messages sent", {"m": ("AWS/SQS", "NumberOfMessagesSent", "Sum")},
        lambda s: total(s, "m") or None, fmt_count, None, spark="m"),
    "errors": SignalDef(
        "not deleted",
        {"e": ("AWS/SQS", "NumberOfMessagesReceived", "Sum"),
         "d": ("AWS/SQS", "NumberOfMessagesDeleted", "Sum")},
        lambda s: (None if total(s, "e") <= 0
                   else max(total(s, "e") - total(s, "d"), 0) / total(s, "e") * 100),
        fmt_pct, (5.0, 25.0)),
    "saturation": SignalDef(
        "backlog", {"v": ("AWS/SQS", "ApproximateNumberOfMessagesVisible", "Maximum")},
        lambda s: peak(s, "v"), fmt_count, (1000, 10000), spark="v"),
})
def _discover_sqs(ctx: Ctx) -> list[Target]:
    out = []
    for url in paginate(ctx.client("sqs"), "list_queues", "QueueUrls"):
        name = url.rsplit("/", 1)[-1]
        out.append(Target("sqs", name, name, [("QueueName", name)]))
    return out


# --- ECS services ---------------------------------------------------------- #


@register("ecs", "ECS Services", {
    "latency": SignalDef("n/a", {}, lambda s: None, fmt_ms),
    "traffic": SignalDef(
        "running tasks", {"t": ("ECS/ContainerInsights", "RunningTaskCount", "Average")},
        lambda s: mean(s, "t"), fmt_count, None, spark="t"),
    "errors": SignalDef("n/a", {}, lambda s: None, fmt_count),
    "saturation": SignalDef(
        "peak CPU/mem",
        {"cpu": ("AWS/ECS", "CPUUtilization", "Average"),
         "mem": ("AWS/ECS", "MemoryUtilization", "Average")},
        lambda s: (None if not has_any(s, "cpu", "mem")
                   else max(peak(s, "cpu") or 0, peak(s, "mem") or 0)),
        fmt_pct, (75, 90), spark="cpu"),
})
def _discover_ecs(ctx: Ctx) -> list[Target]:
    ecs, out = ctx.client("ecs"), []
    for cluster_arn in paginate(ecs, "list_clusters", "clusterArns"):
        cluster = cluster_arn.rsplit("/", 1)[-1]
        for svc_arn in paginate(ecs, "list_services", "serviceArns", cluster=cluster_arn):
            svc = svc_arn.rsplit("/", 1)[-1]
            out.append(Target("ecs", f"{cluster}/{svc}", f"{cluster}/{svc}",
                              [("ClusterName", cluster), ("ServiceName", svc)]))
    return out


# --- EC2 ------------------------------------------------------------------- #


@register("ec2", "EC2 Instances", {
    "latency": SignalDef("n/a", {}, lambda s: None, fmt_ms),
    "traffic": SignalDef(
        "network", {"i": ("AWS/EC2", "NetworkIn", "Sum"), "o": ("AWS/EC2", "NetworkOut", "Sum")},
        lambda s: None, fmt_bytes_rate, None, spark="i"),  # rate filled in per-window below
    "errors": SignalDef(
        "status checks", {"f": ("AWS/EC2", "StatusCheckFailed", "Sum")},
        lambda s: total(s, "f") or None, fmt_count, (1, 5), spark="f"),
    "saturation": SignalDef(
        "peak CPU", {"cpu": ("AWS/EC2", "CPUUtilization", "Average")},
        lambda s: peak(s, "cpu"), fmt_pct, (75, 90), spark="cpu"),
})
def _discover_ec2(ctx: Ctx) -> list[Target]:
    out = []
    for res in paginate(ctx.client("ec2"), "describe_instances", "Reservations"):
        for i in res.get("Instances", []):
            if i["State"]["Name"] != "running":
                continue
            name = next((t["Value"] for t in i.get("Tags", []) if t["Key"] == "Name"),
                        i["InstanceId"])
            out.append(Target("ec2", i["InstanceId"], name, [("InstanceId", i["InstanceId"])]))
    return out


# =========================================================================== #
# Collection
# =========================================================================== #


def discover_targets(ctx: Ctx, kinds: list[str]) -> tuple[list[Target], dict[str, str]]:
    targets: list[Target] = []
    errors: dict[str, str] = {}

    def run(key: str):
        try:
            return key, KINDS[key].discover(ctx)
        except Exception as exc:  # noqa: BLE001 — one unavailable service must not stop the run
            return key, exc

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        for key, result in pool.map(run, kinds):
            if isinstance(result, Exception):
                msg = str(result)
                if isinstance(result, ClientError):
                    msg = result.response.get("Error", {}).get("Message", msg)
                errors[key] = msg.split("\n")[0][:140]
            else:
                targets.extend(result)
    return targets, errors


def fetch_series(ctx: Ctx, targets: list[Target]) -> dict[tuple[str, str, str], list[float]]:
    """One get_metric_data sweep for every (target, signal, alias) needed."""
    end = datetime.now(timezone.utc).replace(second=0, microsecond=0)
    start = end - ctx.window

    queries: list[dict] = []
    index: dict[str, tuple[str, str, str]] = {}
    for t_i, target in enumerate(targets):
        for signal, sdef in KINDS[target.kind].signals.items():
            for alias, (namespace, metric, stat) in sdef.metrics.items():
                qid = f"q{len(queries)}"
                index[qid] = (target.kind + "|" + target.id, signal, alias)
                queries.append({
                    "Id": qid,
                    "MetricStat": {
                        "Metric": {
                            "Namespace": namespace,
                            "MetricName": metric,
                            "Dimensions": [{"Name": n, "Value": v} for n, v in target.dims],
                        },
                        "Period": ctx.period,
                        "Stat": stat,
                    },
                    "ReturnData": True,
                })

    out: dict[tuple[str, str, str], list[float]] = {}
    cw = ctx.client("cloudwatch")
    chunks = [queries[i:i + 100] for i in range(0, len(queries), 100)]

    def run(chunk: list[dict]) -> list[dict]:
        results, token = [], None
        while True:
            kwargs = {"MetricDataQueries": chunk, "StartTime": start, "EndTime": end,
                      "ScanBy": "TimestampAscending"}
            if token:
                kwargs["NextToken"] = token
            resp = cw.get_metric_data(**kwargs)
            results.extend(resp.get("MetricDataResults", []))
            token = resp.get("NextToken")
            if not token:
                return results

    with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
        for results in pool.map(run, chunks):
            for item in results:
                key = index.get(item["Id"])
                if key:
                    out.setdefault(key, []).extend(item.get("Values", []) or [])
    return out


def evaluate(ctx: Ctx, targets: list[Target], raw: dict, overrides: dict) -> list[Row]:
    rows = []
    for target in targets:
        readings = {}
        for signal, sdef in KINDS[target.kind].signals.items():
            series: Series = {
                alias: raw.get((target.kind + "|" + target.id, signal, alias), [])
                for alias in sdef.metrics
            }
            value = sdef.reduce(series)

            # Counts are easier to read as a rate over the window.
            if signal == "traffic" and value is not None and sdef.fmt is fmt_count \
                    and any(st == "Sum" for _, _, st in sdef.metrics.values()):
                text = f"{fmt_count(value)} ({fmt_rate(value / ctx.window_minutes)})"
            elif target.kind == "ec2" and signal == "traffic":
                byte_total = total(series, "i") + total(series, "o")
                value = byte_total / ctx.window.total_seconds() if byte_total else None
                text = fmt_bytes_rate(value) if value is not None else NODATA
            else:
                text = sdef.fmt(value) if value is not None else NODATA

            status = sdef.grade(value, overrides.get(target.kind, {}).get(signal))
            # No number means no trend to draw — an all-zero sparkline would imply
            # a measurement that was never taken.
            trend = list(series.get(sdef.spark) or []) if (sdef.spark and value is not None) else []
            readings[signal] = Reading(value, status, text, trend)
        rows.append(Row(target, readings))
    return rows


# =========================================================================== #
# Output
# =========================================================================== #


def render(ctx: Ctx, rows: list[Row], errors: dict[str, str], account: str) -> None:
    if not rows:
        print(yellow("Nothing to check in this region."))
        return

    term = min(shutil_width(), 200)
    name_w = max(18, min(38, max(len(r.target.display) for r in rows) + 1))
    fixed = 2 + 8 + name_w  # indent + status column + name column

    by_kind: dict[str, list[Row]] = {}
    for row in rows:
        by_kind.setdefault(row.target.kind, []).append(row)
    if ctx.args.only_problems:
        by_kind = {k: [r for r in v if SEVERITY[r.status] >= SEVERITY[WARN]]
                   for k, v in by_kind.items()}
        by_kind = {k: v for k, v in by_kind.items() if v}
        if not by_kind:
            print(green("\nNo warnings or criticals."))

    def widths(spark_w: int) -> dict[str, int]:
        """Width each signal column needs, given a sparkline size."""
        out = {}
        for signal in SIGNALS:
            longest = max(
                [len(KINDS[k].signals[signal].label)
                 for k in by_kind] +
                [len(r.readings[signal].text)
                 + (spark_w + 1 if (spark_w and r.readings[signal].trend) else 0)
                 for v in by_kind.values() for r in v]
            )
            out[signal] = longest + 2
        return out

    # Shrink (then drop) sparklines rather than letting rows wrap.
    spark_w = ctx.args.spark_width if ctx.args.spark else 0
    while spark_w and fixed + sum(widths(spark_w).values()) > term:
        spark_w = 0 if spark_w <= 4 else spark_w - 2
    col = widths(spark_w)

    for kind, kind_rows in by_kind.items():
        kdef = KINDS[kind]
        kind_rows.sort(key=lambda r: (-SEVERITY[r.status], r.target.display))

        print(f"\n{bold(kdef.label)} {dim(f'({len(kind_rows)})')}")
        header = "  " + "STATUS".ljust(8) + "NAME".ljust(name_w)
        for signal in SIGNALS:
            header += kdef.signals[signal].label.upper().ljust(col[signal])
        print(dim(header.rstrip()))

        for row in kind_rows:
            line = "  " + paint(row.status, row.status.ljust(8))
            line += row.target.display[:name_w - 1].ljust(name_w)
            for signal in SIGNALS:
                reading = row.readings[signal]
                cell, plain = reading.text, reading.text
                if spark_w and reading.trend:
                    spark = sparkline(reading.trend, spark_w)
                    if spark:
                        plain = f"{cell} {spark}"
                        cell = f"{cell} {dim(spark)}"
                line = line + paint(reading.status, cell) + " " * max(1, col[signal] - len(plain))
            print(line.rstrip())

    counts = {s: sum(1 for r in rows if r.status == s) for s in (CRIT, WARN, OK, NODATA)}
    print()
    print(bold("-" * min(76, term)))
    parts = [red(f"{counts[CRIT]} CRIT"), yellow(f"{counts[WARN]} WARN"),
             green(f"{counts[OK]} OK"), dim(f"{counts[NODATA]} no data")]
    print("  " + " · ".join(parts)
          + dim(f"   ({len(rows)} targets · {human_window(ctx.window)} window"
                f" · {ctx.period}s period · {ctx.region} · {account})"))
    if errors:
        print(dim(f"  {len(errors)} service(s) unavailable: "
                  + ", ".join(f"{KINDS[k].label}" for k in errors)))


def _strip(text: str) -> str:
    return re.sub(r"\033\[[0-9;]*m", "", text)


def shutil_width() -> int:
    try:
        import shutil
        return shutil.get_terminal_size((100, 24)).columns
    except Exception:  # noqa: BLE001
        return 100


def render_json(ctx: Ctx, rows: list[Row], errors: dict[str, str], account: str) -> None:
    payload = {
        "account": account,
        "region": ctx.region,
        "window": human_window(ctx.window),
        "period_seconds": ctx.period,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "unavailable": errors,
        "targets": [
            {
                "kind": row.target.kind,
                "id": row.target.id,
                "name": row.target.display,
                "status": row.status,
                "signals": {
                    signal: {
                        "label": KINDS[row.target.kind].signals[signal].label,
                        "value": reading.value,
                        "display": reading.text,
                        "status": reading.status,
                    }
                    for signal, reading in row.readings.items()
                },
            }
            for row in rows
        ],
    }
    print(json.dumps(payload, indent=2, default=str))


def explain() -> None:
    print(bold("Metrics behind each column\n"))
    for kdef in KINDS.values():
        print(bold(kdef.label))
        for signal in SIGNALS:
            sdef = kdef.signals[signal]
            if not sdef.metrics:
                print(f"  {signal:<12} {dim('not published with a usable dimension set')}")
                continue
            srcs = ", ".join(f"{m} ({stat})" for _, m, stat in sdef.metrics.values())
            limits = (f"warn ≥ {sdef.thresholds[0]}, crit ≥ {sdef.thresholds[1]}"
                      if sdef.thresholds else "informational")
            print(f"  {signal:<12} {sdef.label:<18} {srcs}")
            print(f"  {'':<12} {dim(limits)}")
        print()


# =========================================================================== #
# Thresholds / CLI
# =========================================================================== #


def load_overrides(args: argparse.Namespace) -> dict[str, dict[str, tuple[float, float]]]:
    """File first, then --threshold flags, which win."""
    out: dict[str, dict[str, tuple[float, float]]] = {}

    if args.thresholds:
        with open(args.thresholds, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        for kind, signals in data.items():
            if kind not in KINDS:
                raise ValueError(f"unknown kind {kind!r} in {args.thresholds}")
            for signal, pair in signals.items():
                if signal not in SIGNALS:
                    raise ValueError(f"unknown signal {kind}.{signal!r}")
                if not isinstance(pair, (list, tuple)) or len(pair) != 2:
                    raise ValueError(f"{kind}.{signal} must be [warn, crit]")
                out.setdefault(kind, {})[signal] = (float(pair[0]), float(pair[1]))

    for raw in args.threshold or []:
        m = re.fullmatch(r"([\w-]+)\.(\w+)=([\d.]+):([\d.]+)", raw.strip())
        if not m:
            raise ValueError(f"--threshold must look like lambda.errors=1:5, got {raw!r}")
        kind, signal, warn, crit = m.groups()
        if kind not in KINDS:
            raise ValueError(f"unknown kind {kind!r}")
        if signal not in SIGNALS:
            raise ValueError(f"unknown signal {signal!r}")
        out.setdefault(kind, {})[signal] = (float(warn), float(crit))
    return out


def parse_window(raw: str) -> timedelta:
    m = re.fullmatch(r"(\d+)\s*([mhd])", raw.strip().lower())
    if not m:
        raise argparse.ArgumentTypeError(f"window must look like 30m, 6h or 2d, got {raw!r}")
    n, unit = int(m.group(1)), m.group(2)
    delta = {"m": timedelta(minutes=n), "h": timedelta(hours=n), "d": timedelta(days=n)}[unit]
    if delta < timedelta(minutes=5):
        raise argparse.ArgumentTypeError("window must be at least 5m")
    if delta > timedelta(days=14):
        raise argparse.ArgumentTypeError("window must be 14d or less")
    return delta


def human_window(delta: timedelta) -> str:
    secs = int(delta.total_seconds())
    if secs % 86400 == 0:
        return f"{secs // 86400}d"
    if secs % 3600 == 0:
        return f"{secs // 3600}h"
    return f"{secs // 60}m"


def auto_period(window: timedelta) -> int:
    """Aim for roughly 60 datapoints, snapped to a CloudWatch-friendly period."""
    target = window.total_seconds() / 60
    for period in (60, 300, 900, 3600, 21600):
        if target <= period:
            return period
    return 86400


def parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="golden_signals",
        description="Check latency, traffic, errors and saturation across an AWS region.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="Exit codes: 0 clear · 1 warnings · 2 criticals (tune with --fail-on).",
    )
    p.add_argument("--region", "-r", help="AWS region (falls back to AWS_REGION, else prompts)")
    p.add_argument("--profile", "-p", help="AWS profile from ~/.aws/credentials")
    p.add_argument("--window", "-w", type=parse_window, default=parse_window("1h"),
                   metavar="30m|6h|2d", help="look-back window (default 1h)")
    p.add_argument("--period", type=int, metavar="SECONDS",
                   help="CloudWatch period (default: derived from the window)")
    p.add_argument("--kind", "-k", action="append", metavar="KIND",
                   help=f"limit to a service kind: {', '.join(KINDS)}; repeatable")
    p.add_argument("--name", "-n", action="append", metavar="GLOB",
                   help="limit to targets whose name matches a glob; repeatable")
    p.add_argument("--only-problems", "-P", action="store_true",
                   help="show only rows that are WARN or CRIT")
    p.add_argument("--threshold", "-T", action="append", metavar="KIND.SIGNAL=WARN:CRIT",
                   help="override one threshold, e.g. lambda.errors=1:5; repeatable")
    p.add_argument("--thresholds", metavar="PATH", help="JSON file of threshold overrides")
    p.add_argument("--fail-on", choices=("warn", "crit", "never"), default="crit",
                   help="lowest status that makes the exit code non-zero (default crit)")
    p.add_argument("--json", action="store_true", help="machine-readable output")
    p.add_argument("--watch", type=int, metavar="SECONDS",
                   help="re-run every N seconds until interrupted")
    p.add_argument("--no-spark", dest="spark", action="store_false",
                   help="hide the inline trend sparklines")
    p.add_argument("--spark-width", type=int, default=12, metavar="N",
                   help="sparkline width in characters (default 12)")
    p.add_argument("--explain", action="store_true",
                   help="print the metric and thresholds behind every column, then exit")
    return p.parse_args(argv)


def run_once(ctx: Ctx, kinds: list[str], overrides: dict, account: str) -> int:
    targets, errors = discover_targets(ctx, kinds)

    if ctx.args.name:
        import fnmatch
        targets = [t for t in targets
                   if any(fnmatch.fnmatch(t.display.lower(), g.lower()) or
                          fnmatch.fnmatch(t.id.lower(), g.lower()) for g in ctx.args.name)]

    if not targets:
        if ctx.args.json:
            render_json(ctx, [], errors, account)
        else:
            print(yellow(f"No matching targets in {ctx.region}."))
        return 0

    raw = fetch_series(ctx, targets)
    rows = evaluate(ctx, targets, raw, overrides)

    if ctx.args.json:
        render_json(ctx, rows, errors, account)
    else:
        render(ctx, rows, errors, account)

    worst = max((SEVERITY[r.status] for r in rows), default=0)
    if ctx.args.fail_on == "never":
        return 0
    if worst >= SEVERITY[CRIT]:
        return 2
    if worst >= SEVERITY[WARN] and ctx.args.fail_on == "warn":
        return 1
    return 0


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv if argv is not None else sys.argv[1:])

    if args.explain:
        explain()
        return 0

    kinds = list(KINDS)
    if args.kind:
        unknown = [k for k in args.kind if k not in KINDS]
        if unknown:
            print(red(f"Unknown kind(s): {', '.join(unknown)}. Known: {', '.join(KINDS)}"))
            return 2
        kinds = args.kind

    try:
        overrides = load_overrides(args)
    except (ValueError, OSError, json.JSONDecodeError) as exc:
        print(red(f"Bad thresholds: {exc}"))
        return 2

    region = args.region or os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION")
    if not region:
        try:
            region = input("Region: ").strip()
        except (EOFError, KeyboardInterrupt):
            return 1
    if not region:
        print(red("A region is required."))
        return 2
    args.region = region
    args.period = args.period or auto_period(args.window)

    try:
        session = boto3.Session(profile_name=args.profile, region_name=region)
        ident = session.client("sts", region_name=region).get_caller_identity()
    except (NoCredentialsError, ClientError, BotoCoreError) as exc:
        print(red(f"Could not authenticate to AWS: {exc}"))
        return 2
    account = ident["Account"]

    ctx = Ctx(session, region, args)

    if not args.watch:
        return run_once(ctx, kinds, overrides, account)

    code = 0
    while True:
        if sys.stdout.isatty():
            print("\033[2J\033[H", end="")
        stamp = datetime.now().strftime("%H:%M:%S")
        print(bold(f"golden signals · {region} · {human_window(args.window)} window · {stamp}"))
        try:
            code = run_once(ctx, kinds, overrides, account)
        except (ClientError, BotoCoreError) as exc:
            print(red(f"Refresh failed: {exc}"))
        print(dim(f"\nrefreshing every {args.watch}s — ctrl-c to stop"))
        time.sleep(args.watch)
    return code


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print(yellow("\nStopped."))
        sys.exit(130)
