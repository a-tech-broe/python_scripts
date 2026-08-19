#!/usr/bin/env python3
"""
incident.py — one command from alert to probable cause.

    ALERT
      ↓
    ./incident.py --service payments --env prod
      ↓
    ECS · ALB · Lambda · RDS · CloudWatch alarms · logs · CloudTrail
      ↓
    correlated evidence  →  probable cause  →  remediation  →  incident summary

Give it a service name. It finds everything in the region that answers to that name,
reads each one's health, works out *when* the incident started, then asks CloudTrail
what changed just before that — which is usually the answer.

    ./incident.py -r us-east-1 --service payments --env prod
    ./incident.py -r us-east-1 -s payments -w 6h --report incident.md
    ./incident.py -r us-east-1 -s payments --json | jq .probable_cause
    ./incident.py -r us-east-1 -s payments --no-logs        # skip Insights billing

Read-only. Exit codes: 0 clear · 1 warnings · 2 criticals.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "lib"))

from sretk import (  # noqa: E402
    Aws, AwsError, CRIT, Finding, INFO, OK, Report, WARN,
    arn_tail, changes, ecs, error_message, logs, metrics, out, timewin,
)


# =========================================================================== #
# Resolution — what does "payments" actually mean in this account?
# =========================================================================== #


def _matches(name: str, needle: str, env: str | None) -> bool:
    lower = name.lower()
    if needle.lower() not in lower:
        return False
    return env.lower() in lower if env else True


def resolve(aws: Aws, service: str, env: str | None) -> dict[str, list[Any]]:
    """Find every resource in the region whose name answers to `service`."""
    found: dict[str, list[Any]] = {"ecs": [], "lambda": [], "alb": [], "rds": [],
                                   "targetgroup": [], "logs": [], "alarms": []}

    def ecs_services():
        return [s for s in ecs.find_services(aws, service)
                if _matches(f"{s['cluster']}/{s['service']}", service, env)]

    def lambdas():
        return [f["FunctionName"] for f in aws.paginate("lambda", "list_functions", "Functions")
                if _matches(f["FunctionName"], service, env)]

    def load_balancers():
        out_ = []
        for lb in aws.paginate("elbv2", "describe_load_balancers", "LoadBalancers"):
            if _matches(lb["LoadBalancerName"], service, env):
                out_.append({"name": lb["LoadBalancerName"],
                             "dim": lb["LoadBalancerArn"].split(":loadbalancer/", 1)[-1],
                             "arn": lb["LoadBalancerArn"]})
        return out_

    def target_groups():
        out_ = []
        for tg in aws.paginate("elbv2", "describe_target_groups", "TargetGroups"):
            if _matches(tg["TargetGroupName"], service, env):
                out_.append({"name": tg["TargetGroupName"], "arn": tg["TargetGroupArn"]})
        return out_

    def databases():
        return [db["DBInstanceIdentifier"]
                for db in aws.paginate("rds", "describe_db_instances", "DBInstances")
                if _matches(db["DBInstanceIdentifier"], service, env)]

    def log_groups():
        return logs.find_groups(aws, service)

    def alarms():
        out_ = []
        for alarm in aws.paginate("cloudwatch", "describe_alarms", "MetricAlarms"):
            haystack = alarm["AlarmName"] + " " + " ".join(
                d.get("Value", "") for d in alarm.get("Dimensions", []))
            if _matches(haystack, service, env):
                out_.append(alarm)
        return out_

    jobs = {"ecs": ecs_services, "lambda": lambdas, "alb": load_balancers,
            "targetgroup": target_groups, "rds": databases, "logs": log_groups,
            "alarms": alarms}

    with concurrent.futures.ThreadPoolExecutor(max_workers=7) as pool:
        futures = {pool.submit(fn): key for key, fn in jobs.items()}
        for future in concurrent.futures.as_completed(futures):
            key = futures[future]
            try:
                found[key] = future.result()
            except AwsError:
                found[key] = []
    return found


# =========================================================================== #
# Evidence collectors
# =========================================================================== #


def check_alarms(aws: Aws, alarms: list[dict], report: Report,
                 window: timedelta) -> None:
    """Alarms are the highest-value evidence: they carry an exact incident time."""
    if not alarms:
        return
    report.note_checked("alarms")
    firing = [a for a in alarms if a.get("StateValue") == "ALARM"]

    for alarm in firing:
        report.add(Finding(
            CRIT, "alarms", f"Alarm firing: {alarm['AlarmName']}",
            alarm.get("StateReason", "")[:200],
            evidence=[f"{alarm.get('MetricName', '?')} "
                      f"{alarm.get('ComparisonOperator', '')} {alarm.get('Threshold', '')}"],
            remediation="This alarm is the one paging you — its metric is the symptom to chase.",
            at=alarm.get("StateUpdatedTimestamp"), confidence=0.7,
        ))

    # State transitions inside the window date the incident precisely.
    start, _ = _bounds(window)

    def history_of(alarm: dict) -> tuple[dict, list[dict]]:
        try:
            return alarm, aws.client("cloudwatch").describe_alarm_history(
                AlarmName=alarm["AlarmName"], HistoryItemType="StateUpdate",
                StartDate=start, EndDate=datetime.now(timezone.utc), MaxRecords=20,
            ).get("AlarmHistoryItems", [])
        except AwsError:
            return alarm, []

    with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
        for alarm, history in pool.map(history_of, alarms):
            for item in history:
                summary = item.get("HistorySummary", "")
                if "to ALARM" in summary:
                    report.event(item["Timestamp"], "alarms",
                                 f"{alarm['AlarmName']} → ALARM", CRIT)
                elif "to OK" in summary:
                    report.event(item["Timestamp"], "alarms",
                                 f"{alarm['AlarmName']} → OK", OK)

    if not firing:
        report.add(Finding(OK, "alarms", f"{len(alarms)} related alarm(s), none firing"))


def check_lambda(aws: Aws, function: str, report: Report, window: timedelta,
                 period: int) -> None:
    report.note_checked("lambda")
    dims = [("FunctionName", function)]
    series = metrics.fetch(aws, [
        metrics.Query("inv", "AWS/Lambda", "Invocations", "Sum", dims),
        metrics.Query("err", "AWS/Lambda", "Errors", "Sum", dims),
        metrics.Query("thr", "AWS/Lambda", "Throttles", "Sum", dims),
        metrics.Query("dur", "AWS/Lambda", "Duration", "p95", dims),
    ], window, period)

    invocations = metrics.total(series, "inv")
    if not invocations and not metrics.total(series, "err"):
        report.add(Finding(INFO, "lambda", f"{function}: no invocations in this window"))
        return

    error_rate = metrics.rate_pct(series, "err", "inv")
    if error_rate is not None and error_rate > 0:
        severity = CRIT if error_rate >= 5 else WARN if error_rate >= 1 else INFO
        finding = Finding(
            severity, "lambda", f"{function}: {error_rate:.1f}% of invocations failing",
            f"{metrics.total(series, 'err'):.0f} errors of {invocations:.0f} invocations",
            remediation="Read the function's logs for the exception, then check whether a "
                        "recent code or config change lines up with the spike.",
            confidence=0.6 if severity == CRIT else 0.45,
        )
        # Date the spike so CloudTrail correlation has something to aim at.
        index = metrics.spike_index(series.get("err") or [])
        if index is not None:
            finding.at = _bucket_time(window, period, index, len(series["err"]))
        report.add(finding)
    elif invocations:
        report.add(Finding(OK, "lambda", f"{function}: no errors "
                                         f"({invocations:.0f} invocations)"))

    throttles = metrics.total(series, "thr")
    if throttles:
        report.add(Finding(
            CRIT if throttles > invocations * 0.02 else WARN, "lambda",
            f"{function}: {throttles:.0f} throttled invocation(s)",
            remediation="Raise reserved concurrency, or the account concurrency limit.",
            confidence=0.65,
        ))

    p95 = metrics.mean(series, "dur")
    if p95 is None:
        return
    try:
        config = aws.client("lambda").get_function_configuration(FunctionName=function)
        timeout_ms = config.get("Timeout", 0) * 1000
    except AwsError:
        timeout_ms = 0
    if timeout_ms and p95 >= timeout_ms * 0.8:
        report.add(Finding(
            WARN, "lambda",
            f"{function}: p95 duration {p95 / 1000:.1f}s is near the "
            f"{timeout_ms / 1000:.0f}s timeout",
            remediation="Raise the timeout or make the slow path faster — invocations are "
                        "about to start failing on time, not on logic.",
            confidence=0.6,
        ))
    else:
        report.add(Finding(OK, "lambda", f"{function}: p95 duration {p95:.0f} ms"))


def check_alb(aws: Aws, lb: dict, report: Report, window: timedelta, period: int) -> None:
    report.note_checked("alb")
    dims = [("LoadBalancer", lb["dim"])]
    series = metrics.fetch(aws, [
        metrics.Query("req", "AWS/ApplicationELB", "RequestCount", "Sum", dims),
        metrics.Query("t5", "AWS/ApplicationELB", "HTTPCode_Target_5XX_Count", "Sum", dims),
        metrics.Query("e5", "AWS/ApplicationELB", "HTTPCode_ELB_5XX_Count", "Sum", dims),
        metrics.Query("rt", "AWS/ApplicationELB", "TargetResponseTime", "p95", dims),
        metrics.Query("un", "AWS/ApplicationELB", "UnHealthyHostCount", "Maximum", dims),
    ], window, period)

    requests = metrics.total(series, "req")
    errors = metrics.total(series, "t5") + metrics.total(series, "e5")
    if requests:
        rate = errors / requests * 100
        if rate >= 1:
            finding = Finding(
                CRIT if rate >= 5 else WARN, "alb",
                f"{lb['name']}: {rate:.1f}% of requests returning 5xx",
                f"{errors:.0f} 5xx of {requests:.0f} requests",
                remediation="ELB 5xx means the load balancer could not reach a healthy "
                            "target; target 5xx means the app itself errored.",
                confidence=0.6,
            )
            index = metrics.spike_index([a + b for a, b in
                                         zip(series.get("t5") or [], series.get("e5") or [])])
            if index is not None:
                finding.at = _bucket_time(window, period, index,
                                          len(series.get("t5") or []))
            report.add(finding)
        else:
            report.add(Finding(OK, "alb", f"{lb['name']}: 5xx rate {rate:.2f}%"))

    latency = metrics.mean(series, "rt")
    if latency is not None and latency >= 1:
        report.add(Finding(
            WARN if latency < 3 else CRIT, "alb",
            f"{lb['name']}: p95 target response {latency:.2f}s",
            remediation="Targets are slow — check their CPU, their database, and downstream "
                        "dependencies before blaming the load balancer.",
            confidence=0.5,
        ))

    unhealthy = metrics.peak(series, "un")
    if unhealthy:
        report.add(Finding(
            CRIT, "alb", f"{lb['name']}: up to {unhealthy:.0f} unhealthy target(s)",
            remediation="Check the target group health check path and the app's readiness.",
            confidence=0.7,
        ))


def check_rds(aws: Aws, instance: str, report: Report, window: timedelta,
              period: int) -> None:
    report.note_checked("rds")
    dims = [("DBInstanceIdentifier", instance)]
    series = metrics.fetch(aws, [
        metrics.Query("cpu", "AWS/RDS", "CPUUtilization", "Average", dims),
        metrics.Query("conn", "AWS/RDS", "DatabaseConnections", "Maximum", dims),
        metrics.Query("free", "AWS/RDS", "FreeStorageSpace", "Minimum", dims),
        metrics.Query("rlat", "AWS/RDS", "ReadLatency", "Average", dims),
        metrics.Query("wlat", "AWS/RDS", "WriteLatency", "Average", dims),
    ], window, period)

    cpu = metrics.peak(series, "cpu")
    if cpu is not None:
        if cpu >= 90:
            report.add(Finding(
                CRIT, "rds", f"{instance}: CPU peaked at {cpu:.0f}%",
                remediation="Find the expensive query (pg_stat_statements / performance "
                            "insights) before scaling the instance.",
                confidence=0.6))
        elif cpu >= 75:
            report.add(Finding(WARN, "rds", f"{instance}: CPU peaked at {cpu:.0f}%",
                               confidence=0.4))
        else:
            report.add(Finding(OK, "rds", f"{instance}: CPU peak {cpu:.0f}%"))

    free = metrics.peak(series, "free")
    if free is not None:
        try:
            detail = aws.client("rds").describe_db_instances(
                DBInstanceIdentifier=instance)["DBInstances"][0]
            allocated = detail.get("AllocatedStorage", 0) * 1024 ** 3
        except (AwsError, IndexError, KeyError):
            allocated = 0
        low = min(series.get("free") or [free])
        if allocated:
            pct = low / allocated * 100
            if pct < 10:
                report.add(Finding(
                    CRIT, "rds", f"{instance}: {pct:.1f}% storage free",
                    f"{low / 1024 ** 3:.1f} GB of {allocated / 1024 ** 3:.0f} GB remaining",
                    remediation="Extend storage now — a full volume takes the database "
                                "read-only and no query tuning will save it.",
                    confidence=0.85))
            elif pct < 20:
                report.add(Finding(WARN, "rds", f"{instance}: {pct:.1f}% storage free",
                                   confidence=0.5))

    latency = [v for v in ((metrics.mean(series, "rlat") or 0),
                           (metrics.mean(series, "wlat") or 0)) if v]
    if latency and max(latency) * 1000 >= 50:
        report.add(Finding(
            WARN, "rds", f"{instance}: disk latency {max(latency) * 1000:.0f} ms",
            remediation="Check IOPS/burst balance and whether a big query is scanning.",
            confidence=0.5))


def check_logs(aws: Aws, groups: list[str], report: Report, window: timedelta,
               bin_minutes: int = 5) -> datetime | None:
    """Cluster the error logs; return when the errors started, if they did."""
    if not groups:
        return None
    report.note_checked("logs")
    # Two independent Insights queries, each of which waits on AWS — run them together.
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        hist_future = pool.submit(logs.error_histogram, aws, groups, window, bin_minutes)
        top_future = pool.submit(logs.top_errors, aws, groups, window, 200)
        histogram, clusters = hist_future.result(), top_future.result()

    if not clusters:
        report.add(Finding(OK, "logs", f"No error-shaped lines in {len(groups)} log group(s)"))
        return None

    onset: datetime | None = None
    if histogram:
        counts = [hits for _, hits in histogram]
        index = metrics.spike_index(counts)
        if index is not None:
            onset = histogram[index][0]
            report.event(onset, "logs",
                         f"error rate jumped to {counts[index]:.0f} per {bin_minutes}m", WARN)

    top = clusters[0]
    total_errors = sum(c["count"] for c in clusters)
    report.add(Finding(
        CRIT if total_errors > 50 else WARN, "logs",
        f"{total_errors} error lines across {len(clusters)} distinct shape(s)",
        f"most frequent ({top['count']}×): {top['shape'][:160]}",
        evidence=[f"{c['count']}× {c['shape'][:120]}" for c in clusters[1:4]],
        remediation="Fix the most frequent shape first — the long tail is usually "
                    "downstream of it.",
        at=onset or top.get("first"),
        confidence=0.55,
    ))
    return onset or top.get("first")


def check_changes(aws: Aws, service: str, report: Report, window: timedelta,
                  incident_at: datetime | None) -> None:
    """What changed just before things broke."""
    report.note_checked("changes")
    events = changes.recent(aws, window, resource=service)
    if not events:
        # The resource-name lookup missed, so sweep — but CloudTrail LookupEvents is
        # rate-limited, and a multi-day unfiltered sweep takes minutes nobody has
        # during an incident. Aim at the incident time when we know it, and cap the
        # blind fallback at 6 hours.
        if incident_at:
            events = changes.around(aws, incident_at)
        else:
            events = changes.recent(aws, min(window, timedelta(hours=6)))

    if not events:
        report.add(Finding(INFO, "changes", "No mutating API calls recorded in this window"))
        return

    # A failing API call is a symptom in its own right — and often the whole story.
    # It is not a "change", so it never competes for the what-changed slot.
    failures: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for event in events:
        if event.get("error"):
            failures.setdefault((event["name"], event["error"]), []).append(event)
    for (name, code), group in sorted(failures.items(), key=lambda kv: -len(kv[1])):
        report.add(Finding(
            CRIT if len(group) >= 10 else WARN, "changes",
            f"{len(group)}× {name} failed with {code}",
            f"called by {group[0]['actor']}",
            evidence=[f"{e['at']:%H:%M:%S} {changes.describe(e)}" for e in group[:3]],
            remediation=_remediate_error(code, name),
            at=min(e["at"] for e in group if e.get("at")),
            confidence=0.7,
        ))

    # Only changes that could plausibly break something earn a timeline slot; the
    # rest are counted, not listed, so the timeline stays readable.
    notable = [e for e in events if e.get("at") and e["weight"] >= 0.5][:10]
    for event in notable:
        report.event(event["at"], "changes", changes.describe(event),
                     WARN if event["weight"] >= 0.7 else INFO)
    if len(events) > len(notable):
        report.add(Finding(
            INFO, "changes",
            f"{len(events)} change(s) in the window, {len(notable)} potentially disruptive"))

    suspects = changes.correlate(events, incident_at)
    if not suspects:
        report.add(Finding(
            INFO, "changes", f"{len(events)} change(s) in the window, none just before the "
                             "incident" if incident_at else
                             f"{len(events)} change(s) in the window",
            evidence=[changes.describe(e) for e in events[:3]],
        ))
        return

    top = suspects[0]
    minutes = top["gap_seconds"] / 60
    report.add(Finding(
        WARN, "changes",
        f"{top['name']} ran {minutes:.0f} min before the incident",
        changes.describe(top),
        evidence=[f"{changes.describe(e)} (−{e['gap_seconds'] / 60:.0f} min)"
                  for e in suspects[1:4]],
        remediation=f"Review or roll back this change first — it is the closest "
                    f"mutation to the start of the incident.",
        at=top["at"],
        # The strongest signal in the whole tool: a disruptive call, minutes before
        # things broke. Confidence is deliberately above every symptom-level finding.
        confidence=min(0.95, 0.6 + top["score"] * 0.4),
    ))


# =========================================================================== #
# Helpers
# =========================================================================== #


#: What to actually do about the API error codes that show up during incidents.
_ERROR_REMEDIES = {
    "ThrottlingException": "Rate limited by the service — add backoff/retries and request "
                           "a quota increase; this will keep happening under load.",
    "TooManyRequestsException": "Rate limited — back off, or raise the concurrency/quota.",
    "RequestLimitExceeded": "API rate limit hit — spread the calls out or request a raise.",
    "AccessDenied": "The caller lacks permission — check the role policy and any SCP.",
    "AccessDeniedException": "The caller lacks permission — check the role policy and any SCP.",
    "UnauthorizedOperation": "The caller lacks permission for this action.",
    "ValidationException": "The request itself is malformed — likely a bad config change.",
    "ResourceNotFoundException": "The target no longer exists — something deleted it, or "
                                 "the reference is stale.",
    "ServiceUnavailable": "The AWS service itself is degraded — check the health dashboard.",
    "InternalError": "AWS-side failure — retry, then check the health dashboard.",
    "ExpiredTokenException": "Credentials expired mid-run — refresh the session.",
    "InsufficientInstanceCapacity": "No capacity in this AZ/instance type — try another.",
}


def _remediate_error(code: str, api: str) -> str:
    return _ERROR_REMEDIES.get(
        code, f"Investigate why {api} is returning {code}.")


def _bounds(window: timedelta) -> tuple[datetime, datetime]:
    end = datetime.now(timezone.utc).replace(second=0, microsecond=0)
    return end - window, end


def _bucket_time(window: timedelta, period: int, index: int, count: int) -> datetime:
    """Approximate wall-clock time of datapoint `index` in a series of `count`."""
    start, end = _bounds(window)
    if count <= 0:
        return end
    return start + timedelta(seconds=period * index)


def incident_time(report: Report) -> datetime | None:
    """When did this start? Earliest hard signal wins."""
    candidates = [e.at for e in report.events if e.severity in (CRIT, WARN)]
    candidates += [f.at for f in report.findings if f.at and f.severity in (CRIT, WARN)]
    return min(candidates) if candidates else None


# =========================================================================== #
# CLI
# =========================================================================== #


def parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="incident",
        description="Correlate AWS evidence for one service into a probable cause.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="Exit codes: 0 clear · 1 warnings · 2 criticals.",
    )
    p.add_argument("--service", "-s", required=True, help="service name or substring")
    p.add_argument("--env", "-e", help="environment filter, e.g. prod")
    p.add_argument("--region", "-r", help="AWS region (falls back to AWS_REGION)")
    p.add_argument("--profile", "-p", help="AWS profile")
    p.add_argument("--window", "-w", type=timewin.parse, default=timewin.parse("1h"),
                   metavar="30m|6h|2d", help="look-back window (default 1h)")
    p.add_argument("--report", metavar="PATH", help="write a markdown incident summary")
    p.add_argument("--json", action="store_true", help="machine-readable output")
    p.add_argument("--no-logs", action="store_true",
                   help="skip Logs Insights queries (they bill by bytes scanned)")
    p.add_argument("--no-changes", action="store_true", help="skip the CloudTrail lookup")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv if argv is not None else sys.argv[1:])

    try:
        aws = Aws(args.region, args.profile)
        aws.check()
    except (ValueError, RuntimeError) as exc:
        out.fail(str(exc))
        return 2

    stream = sys.stderr if args.json else sys.stdout
    print(out.dim(f"resolving {args.service!r} in {aws.region}…"), file=stream)
    found = resolve(aws, args.service, args.env)

    total = sum(len(v) for v in found.values())
    if not total:
        message = (f"Nothing in {aws.region} matches {args.service!r}"
                   + (f" + env {args.env!r}" if args.env else ""))
        if args.json:
            print("{}")
        else:
            print(out.yellow(message))
            print(out.dim("  Try a shorter substring, or ../aws/aws-find.sh "
                          f"{args.service}"))
        return 0

    report = Report(args.service, aws.region, aws.account,
                    timewin.label(args.window), args.env)
    period = timewin.period(args.window)

    summary = ", ".join(f"{len(v)} {k}" for k, v in found.items() if v)
    print(out.dim(f"found {summary}"), file=stream)

    # --- symptoms ---------------------------------------------------------- #
    try:
        check_alarms(aws, found["alarms"], report, args.window)
    except AwsError as exc:
        report.skip("alarms", error_message(exc))

    for target in found["ecs"]:
        try:
            ecs.diagnose(aws, target["cluster"], target["service"], report, args.window)
        except AwsError as exc:
            report.skip("ecs", error_message(exc))

    for function in found["lambda"]:
        try:
            check_lambda(aws, function, report, args.window, period)
        except AwsError as exc:
            report.skip("lambda", error_message(exc))

    for lb in found["alb"]:
        try:
            check_alb(aws, lb, report, args.window, period)
        except AwsError as exc:
            report.skip("alb", error_message(exc))

    for database in found["rds"]:
        try:
            check_rds(aws, database, report, args.window, period)
        except AwsError as exc:
            report.skip("rds", error_message(exc))

    for tg in found["targetgroup"]:
        try:
            ecs._diagnose_target_group(aws, tg["arn"], report)
        except AwsError as exc:
            report.skip("alb", error_message(exc))

    # --- logs -------------------------------------------------------------- #
    if args.no_logs:
        report.skip("logs", "skipped (--no-logs)")
    elif found["logs"]:
        try:
            check_logs(aws, found["logs"], report, args.window)
        except AwsError as exc:
            report.skip("logs", error_message(exc))

    # --- what changed ------------------------------------------------------ #
    if args.no_changes:
        report.skip("changes", "skipped (--no-changes)")
    else:
        try:
            check_changes(aws, args.service, report, args.window, incident_time(report))
        except AwsError as exc:
            report.skip("changes", error_message(exc))

    # --- output ------------------------------------------------------------ #
    if args.json:
        print(report.to_json())
    else:
        report.render()

    if args.report:
        Path(args.report).write_text(report.to_markdown(), encoding="utf-8")
        print(out.green(f"incident summary written to {args.report}"), file=stream)

    return {CRIT: 2, WARN: 1}.get(report.status, 0)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print(out.yellow("\nInterrupted."))
        sys.exit(130)
