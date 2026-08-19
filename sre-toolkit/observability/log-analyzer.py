#!/usr/bin/env python3
"""
log-analyzer.py — find the signal in a service's logs.

Clusters error lines by shape (ids, timestamps, IPs and hashes normalised away) so a
million-line log collapses into the handful of distinct things actually going wrong,
with a histogram showing when they started.

    ./log-analyzer.py -r us-east-1 --service payments
    ./log-analyzer.py -r us-east-1 --group /aws/lambda/my-fn --window 6h
    ./log-analyzer.py -r us-east-1 --service payments --pattern 'timeout|refused' --tail
    ./log-analyzer.py -r us-east-1 --group /banking/prod/app --json

Read-only, but note that CloudWatch Logs Insights bills by bytes scanned — a wide
window over a busy group is not free.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "lib"))

from sretk import Aws, AwsError, error_message, logs, out, timewin  # noqa: E402


def parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="log-analyzer",
        description="Cluster and summarise error logs for a service.",
    )
    p.add_argument("--region", "-r", help="AWS region (falls back to AWS_REGION)")
    p.add_argument("--profile", "-p", help="AWS profile")
    p.add_argument("--service", "-s", help="service name; log groups are matched to it")
    p.add_argument("--group", "-g", action="append", metavar="NAME",
                   help="exact log group; repeatable, skips discovery")
    p.add_argument("--window", "-w", type=timewin.parse, default=timewin.parse("1h"),
                   metavar="30m|6h|2d", help="look-back window (default 1h)")
    p.add_argument("--pattern", help="regex to filter on instead of the default error terms")
    p.add_argument("--top", type=int, default=10, metavar="N",
                   help="distinct error shapes to show (default 10)")
    p.add_argument("--scan", type=int, default=300, metavar="N",
                   help="log lines to pull for clustering (default 300)")
    p.add_argument("--tail", action="store_true", help="also print the raw recent lines")
    p.add_argument("--bin", type=int, default=5, metavar="MINUTES",
                   help="histogram bucket size (default 5)")
    p.add_argument("--json", action="store_true", help="machine-readable output")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv if argv is not None else sys.argv[1:])
    if not args.service and not args.group:
        out.fail("give --service or --group")

    try:
        aws = Aws(args.region, args.profile)
        aws.check()
    except (ValueError, RuntimeError) as exc:
        out.fail(str(exc))
        return 2

    groups = args.group or logs.find_groups(aws, args.service)
    if not groups:
        message = f"No log groups matched {args.service!r} in {aws.region}."
        print("{}" if args.json else out.yellow(message))
        return 0

    stream = sys.stderr if args.json else sys.stdout
    print(out.dim(f"scanning {len(groups)} log group(s) over the last "
                  f"{timewin.label(args.window)}…"), file=stream)

    try:
        histogram = logs.error_histogram(aws, groups, args.window, args.bin)
        clusters = logs.top_errors(aws, groups, args.window, limit=args.scan)
        tail = logs.sample_lines(aws, groups, args.window, args.pattern, 20) if args.tail else []
    except AwsError as exc:
        out.fail(f"log query failed: {error_message(exc)}")
        return 2

    if args.json:
        print(json.dumps({
            "region": aws.region,
            "account": aws.account,
            "window": timewin.label(args.window),
            "groups": groups,
            "histogram": [{"at": at.isoformat(), "hits": hits} for at, hits in histogram],
            "clusters": [
                {"shape": c["shape"], "count": c["count"], "sample": c["sample"],
                 "first_seen": c["first"].isoformat() if c["first"] else None,
                 "last_seen": c["last"].isoformat() if c["last"] else None}
                for c in clusters[:args.top]
            ],
            "tail": tail,
        }, indent=2, default=str))
        return 0

    out.heading(f"logs · {args.service or groups[0]} · {aws.region}")
    for group in groups:
        out.kv("group", group)
    out.kv("window", timewin.label(args.window))

    total = sum(hits for _, hits in histogram)
    if not clusters and not total:
        print(out.green("\n  No error-shaped lines in this window."))
        return 0

    if histogram:
        out.section("When", len(histogram))
        values = [hits for _, hits in histogram]
        print(f"  {out.sparkline(values, 40)}  peak {max(values):.0f} per {args.bin}m")
        print(out.dim(f"  {histogram[0][0]:%H:%M} → {histogram[-1][0]:%H:%M} UTC · "
                      f"{total:.0f} matching lines"))
        busiest = max(histogram, key=lambda h: h[1])
        if busiest[1] > 0:
            print(f"  busiest bucket: {out.yellow(f'{busiest[0]:%H:%M} UTC')} "
                  f"({busiest[1]:.0f} lines)")

    out.section("Distinct error shapes", len(clusters))
    rows = []
    for cluster in clusters[:args.top]:
        seen = ""
        if cluster["first"] and cluster["last"]:
            # Include the date once a cluster spans more than one day, otherwise
            # "23:20–00:16" reads as though it ran backwards.
            span_days = cluster["first"].date() != cluster["last"].date()
            fmt = "%m-%d %H:%M" if span_days else "%H:%M"
            seen = f"{cluster['first']:{fmt}}–{cluster['last']:{fmt}}"
        rows.append([out.bold(str(cluster["count"])), seen, cluster["shape"][:110]])
    out.table(["count", "seen", "shape"], rows)

    if clusters:
        out.section("Most frequent, in full")
        print(out.dim(f"  {clusters[0]['sample'][:600]}"))

    if tail:
        out.section("Recent lines", len(tail))
        for line in tail:
            print(out.dim(f"  {line[:200]}"))
    print()
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
