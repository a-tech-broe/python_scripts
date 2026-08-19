#!/usr/bin/env python3
"""
ecs-diagnose.py — why is this ECS/Fargate service unhealthy?

Reads task counts, deployment rollout state, service events, stopped-task reasons,
target group health and utilisation, then says what is wrong and what to do about it.

    ./ecs-diagnose.py --region us-east-1                  # list services, diagnose all
    ./ecs-diagnose.py -r us-east-1 --service payments
    ./ecs-diagnose.py -r us-east-1 -c prod-cluster -s payments --window 6h --json

Read-only.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "lib"))

from sretk import Aws, AwsError, Report, ecs, error_message, out, timewin  # noqa: E402


def parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="ecs-diagnose",
        description="Diagnose ECS/Fargate services in a region.",
        epilog="Exit codes: 0 healthy · 1 warnings · 2 criticals.",
    )
    p.add_argument("--region", "-r", help="AWS region (falls back to AWS_REGION)")
    p.add_argument("--profile", "-p", help="AWS profile")
    p.add_argument("--cluster", "-c", help="cluster name (default: search all)")
    p.add_argument("--service", "-s", help="service name or substring (default: all)")
    p.add_argument("--window", "-w", type=timewin.parse, default=timewin.parse("1h"),
                   metavar="30m|6h|2d", help="look-back window for metrics (default 1h)")
    p.add_argument("--events", type=int, default=20, metavar="N",
                   help="service events to scan (default 20)")
    p.add_argument("--json", action="store_true", help="machine-readable output")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv if argv is not None else sys.argv[1:])

    try:
        aws = Aws(args.region, args.profile)
        aws.check()
    except (ValueError, RuntimeError) as exc:
        out.fail(str(exc))
        return 2

    try:
        services = ecs.find_services(aws, args.service)
    except AwsError as exc:
        out.fail(f"could not list ECS services: {error_message(exc)}")
        return 2

    if args.cluster:
        services = [s for s in services if s["cluster"] == args.cluster]

    if not services:
        message = "No ECS services found"
        if args.service or args.cluster:
            message += f" matching {args.service or args.cluster!r}"
        if args.json:
            print("[]")
        else:
            print(out.yellow(f"{message} in {aws.region}."))
        return 0

    reports = []
    for target in services:
        report = Report(f"{target['cluster']}/{target['service']}", aws.region,
                        aws.account, timewin.label(args.window))
        try:
            ecs.diagnose(aws, target["cluster"], target["service"], report, args.window,
                         max_events=args.events)
        except AwsError as exc:
            report.skip("ecs", error_message(exc))
        reports.append(report)

    if args.json:
        import json
        print(json.dumps([r.to_dict() for r in reports], indent=2, default=str))
    else:
        for report in reports:
            report.render()

    statuses = [r.status for r in reports]
    if any(s == out.CRIT for s in statuses):
        return 2
    if any(s == out.WARN for s in statuses):
        return 1
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
