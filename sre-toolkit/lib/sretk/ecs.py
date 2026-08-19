"""ECS / Fargate service diagnosis.

Shared by `aws/ecs-diagnose.py` and `incident/incident.py` so the two can never
disagree about what a broken service looks like.
"""

from __future__ import annotations

import re
from datetime import timedelta
from typing import Any

from . import metrics
from .aws import Aws, AwsError, arn_tail, error_message
from .findings import CRIT, INFO, OK, WARN, Finding, Report

#: Service-event text that has a known meaning, mapped to (severity, what to do).
EVENT_PATTERNS: list[tuple[str, str, str, str]] = [
    (r"unable to place a task because no container instance met",
     CRIT, "No capacity to place tasks",
     "Cluster has no instance meeting CPU/memory/port/attribute requirements — "
     "scale the capacity provider or reduce the task's reservations."),
    (r"unable to place a task.*resources could not be found",
     CRIT, "Task placement failed: resources missing",
     "Check the subnets, security groups and capacity provider referenced by the service."),
    (r"failed container health checks",
     CRIT, "Containers failing their health check",
     "Compare the container healthCheck command against what the app actually serves; "
     "check startPeriod is long enough for boot."),
    (r"\(service .*\) is unable to consistently start tasks successfully",
     CRIT, "Tasks crash-looping on start",
     "Read the stopped-task reasons and the container logs — the image, entrypoint or "
     "config is failing before the app can serve traffic."),
    (r"unhealthy in .*target-group|deregistered .*because it is unhealthy",
     WARN, "Targets deregistered as unhealthy",
     "Check the target group health check path, port and timeout against the app."),
    (r"was unable to assume the role|ECS was unable to assume",
     CRIT, "Task/execution role cannot be assumed",
     "Fix the trust policy on the task execution role (ecs-tasks.amazonaws.com)."),
    (r"CannotPullContainerError|unable to pull|image not found",
     CRIT, "Image pull failure",
     "Check the image tag exists and the execution role can read the ECR repo."),
    (r"has begun draining connections",
     INFO, "Draining connections", ""),
    (r"has reached a steady state",
     OK, "Service reached steady state", ""),
    (r"registered .*targets? in .*target-group",
     INFO, "Targets registered", ""),
]

#: Why a task stopped, and what that usually means.
STOP_PATTERNS: list[tuple[str, str, str, str]] = [
    (r"OutOfMemory|OOMKilled",
     CRIT, "Container killed for exceeding its memory limit",
     "Raise the task/container memory, or fix the leak — this repeats until one of those happens."),
    (r"CannotPullContainerError",
     CRIT, "Image could not be pulled",
     "Verify the image tag and that the execution role has ecr:GetAuthorizationToken + pull rights."),
    (r"ResourceInitializationError.*secrets|AccessDenied.*secretsmanager|ssm",
     CRIT, "Secrets/parameters could not be read at start",
     "Grant the execution role access to the referenced secret or SSM parameter."),
    (r"ResourceInitializationError.*network|failed to configure ENI|CannotCreateNetworkInterface",
     CRIT, "Networking could not be set up for the task",
     "Check subnet free IPs, the security groups, and whether the subnet has a route out."),
    (r"Essential container in task exited",
     CRIT, "Essential container exited",
     "Read the container's exit code and logs — the process itself is dying."),
    (r"Task failed ELB health checks",
     CRIT, "Task failed load balancer health checks",
     "Align the target group health check with a path the app answers 200 on."),
    (r"Scaling activity initiated|ServiceSchedulerInitiated|UserInitiated",
     INFO, "Task stopped deliberately", ""),
    (r"SpotInterruption",
     WARN, "Spot capacity reclaimed",
     "Expected on Spot — add on-demand base capacity if this is hurting availability."),
]


def _match(text: str, table: list[tuple[str, str, str, str]]) -> tuple[str, str, str] | None:
    for pattern, severity, title, remedy in table:
        if re.search(pattern, text, re.I):
            return severity, title, remedy
    return None


def find_services(aws: Aws, needle: str | None = None) -> list[dict[str, str]]:
    """Every ECS service in the region, optionally narrowed by substring."""
    found = []
    try:
        for cluster_arn in aws.paginate("ecs", "list_clusters", "clusterArns"):
            cluster = arn_tail(cluster_arn)
            for service_arn in aws.paginate("ecs", "list_services", "serviceArns",
                                            cluster=cluster_arn):
                name = arn_tail(service_arn)
                if needle and needle.lower() not in name.lower() \
                        and needle.lower() not in cluster.lower():
                    continue
                found.append({"cluster": cluster, "cluster_arn": cluster_arn,
                              "service": name, "service_arn": service_arn})
    except AwsError:
        return found
    return found


def describe(aws: Aws, cluster: str, service: str) -> dict[str, Any] | None:
    try:
        resp = aws.client("ecs").describe_services(cluster=cluster, services=[service],
                                                   include=["TAGS"])
    except AwsError:
        return None
    services = resp.get("services") or []
    return services[0] if services else None


def diagnose(aws: Aws, cluster: str, service: str, report: Report,
             window: timedelta, max_events: int = 20) -> dict[str, Any] | None:
    """Inspect one ECS service and add findings to `report`."""
    report.note_checked("ecs")
    detail = describe(aws, cluster, service)
    if not detail:
        report.skip("ecs", f"service {service} not found in cluster {cluster}")
        return None

    desired = detail.get("desiredCount", 0)
    running = detail.get("runningCount", 0)
    pending = detail.get("pendingCount", 0)

    # --- capacity ---------------------------------------------------------- #
    if desired and running == 0:
        report.add(Finding(
            CRIT, "ecs", "No tasks running",
            f"desired {desired}, running 0, pending {pending}",
            remediation="Service is fully down — check stopped-task reasons below first.",
            confidence=0.8,
        ))
    elif running < desired:
        report.add(Finding(
            WARN, "ecs", "Fewer tasks running than desired",
            f"desired {desired}, running {running}, pending {pending}",
            remediation="Capacity or placement problem; see the service events.",
            confidence=0.6,
        ))
    else:
        report.add(Finding(OK, "ecs", "Task count at desired",
                           f"desired {desired}, running {running}"))

    # --- deployment state -------------------------------------------------- #
    for deployment in detail.get("deployments", []):
        if deployment.get("status") != "PRIMARY":
            continue
        state = deployment.get("rolloutState")
        reason = deployment.get("rolloutStateReason", "")
        failed = deployment.get("failedTasks", 0)
        created = deployment.get("createdAt")
        if state == "FAILED":
            report.add(Finding(
                CRIT, "ecs", "Deployment rollout failed", reason,
                evidence=[f"{failed} failed task(s)"],
                remediation="Roll back to the previous task definition, then debug the new one.",
                at=created, confidence=0.9,
            ))
        elif state == "IN_PROGRESS":
            report.add(Finding(
                WARN, "ecs", "Deployment still in progress", reason,
                at=created,
                remediation="If it has been in progress for a while, the new tasks are not "
                            "passing health checks.",
                confidence=0.7,
            ))
        elif created:
            report.event(created, "ecs",
                         f"deployment {arn_tail(deployment.get('taskDefinition', ''))} "
                         f"({state or 'active'})")
        if failed:
            report.add(Finding(
                WARN, "ecs", f"{failed} task(s) failed during this deployment",
                remediation="Inspect stopped tasks for the failure reason.",
                confidence=0.65, at=created,
            ))

    circuit = (detail.get("deploymentConfiguration") or {}).get("deploymentCircuitBreaker") or {}
    if circuit and not circuit.get("enable"):
        report.add(Finding(
            INFO, "ecs", "Deployment circuit breaker is off",
            remediation="Enable it so a bad rollout stops itself instead of draining capacity.",
            confidence=0.2,
        ))

    # --- service events ---------------------------------------------------- #
    seen: set[str] = set()
    for event in (detail.get("events") or [])[:max_events]:
        message = event.get("message", "")
        when = event.get("createdAt")
        hit = _match(message, EVENT_PATTERNS)
        if not hit:
            continue
        severity, title, remedy = hit
        if severity in (CRIT, WARN) and title not in seen:
            seen.add(title)
            report.add(Finding(severity, "ecs", title, message.strip(),
                               remediation=remedy, at=when,
                               confidence=0.75 if severity == CRIT else 0.55))
        elif when:
            report.event(when, "ecs", message.strip()[:120], severity)

    # --- stopped tasks ----------------------------------------------------- #
    _diagnose_stopped_tasks(aws, cluster, service, report)

    # --- load balancer targets --------------------------------------------- #
    for lb in detail.get("loadBalancers") or []:
        _diagnose_target_group(aws, lb.get("targetGroupArn"), report)

    # --- utilisation ------------------------------------------------------- #
    series = metrics.fetch(aws, [
        metrics.Query("cpu", "AWS/ECS", "CPUUtilization", "Average",
                      [("ClusterName", cluster), ("ServiceName", service)]),
        metrics.Query("mem", "AWS/ECS", "MemoryUtilization", "Average",
                      [("ClusterName", cluster), ("ServiceName", service)]),
    ], window, period=300)
    for alias, label in (("cpu", "CPU"), ("mem", "memory")):
        top = metrics.peak(series, alias)
        if top is None:
            continue
        if top >= 90:
            report.add(Finding(
                WARN, "ecs", f"{label} peaked at {top:.0f}%",
                remediation=f"Raise the task {label.lower()} reservation or scale out.",
                confidence=0.5,
            ))
        else:
            report.add(Finding(OK, "ecs", f"{label} peak {top:.0f}%"))

    return detail


def _diagnose_stopped_tasks(aws: Aws, cluster: str, service: str, report: Report,
                            limit: int = 10) -> None:
    """Stopped tasks carry the most direct explanation of why a service is unhealthy."""
    try:
        arns = list(aws.paginate("ecs", "list_tasks", "taskArns", cluster=cluster,
                                 serviceName=service, desiredStatus="STOPPED"))[:limit]
        if not arns:
            return
        tasks = aws.client("ecs").describe_tasks(cluster=cluster, tasks=arns).get("tasks", [])
    except AwsError as exc:
        report.skip("ecs:stopped-tasks", error_message(exc))
        return

    grouped: dict[str, dict[str, Any]] = {}
    for task in tasks:
        reason = (task.get("stoppedReason") or "").strip()
        code = task.get("stopCode", "")
        containers = [
            f"{c.get('name')} exit={c.get('exitCode', '?')}"
            + (f" ({c['reason']})" if c.get("reason") else "")
            for c in task.get("containers", [])
            if c.get("exitCode") not in (0, None) or c.get("reason")
        ]
        key = f"{code}: {reason}"
        entry = grouped.setdefault(key, {"count": 0, "reason": reason, "code": code,
                                         "containers": set(), "at": task.get("stoppedAt")})
        entry["count"] += 1
        entry["containers"].update(containers)
        if task.get("stoppedAt") and entry["at"]:
            entry["at"] = max(entry["at"], task["stoppedAt"])

    for key, entry in sorted(grouped.items(), key=lambda kv: -kv[1]["count"]):
        text = f"{entry['code']} {entry['reason']}"
        hit = _match(text, STOP_PATTERNS)
        severity, title, remedy = hit or (
            WARN, f"Tasks stopped: {entry['code'] or 'unknown reason'}", "")
        if severity == INFO:
            if entry["at"]:
                report.event(entry["at"], "ecs", f"{entry['count']}× {title}", INFO)
            continue
        report.add(Finding(
            severity, "ecs", f"{title} ({entry['count']}× recently)",
            entry["reason"][:200],
            evidence=sorted(entry["containers"])[:5],
            remediation=remedy, at=entry["at"],
            confidence=0.85 if severity == CRIT else 0.6,
        ))


def _diagnose_target_group(aws: Aws, target_group_arn: str | None, report: Report) -> None:
    if not target_group_arn:
        return
    report.note_checked("alb")
    try:
        health = aws.client("elbv2").describe_target_health(
            TargetGroupArn=target_group_arn).get("TargetHealthDescriptions", [])
    except AwsError as exc:
        report.skip("alb", error_message(exc))
        return

    # targetgroup/<name>/<id> — arn_tail would hand back the random id, not the name.
    name = target_group_arn.split("targetgroup/", 1)[-1].split("/", 1)[0]
    if not health:
        report.add(Finding(
            CRIT, "alb", f"Target group {name} has no targets",
            remediation="Nothing is registered to serve traffic — check the service's "
                        "load balancer wiring.",
            confidence=0.8,
        ))
        return

    unhealthy = [t for t in health if (t.get("TargetHealth") or {}).get("State") != "healthy"]
    if not unhealthy:
        report.add(Finding(OK, "alb", f"All {len(health)} targets healthy in {name}"))
        return

    reasons = sorted({
        f"{(t.get('TargetHealth') or {}).get('Reason', '?')}: "
        f"{(t.get('TargetHealth') or {}).get('Description', '')}".strip()
        for t in unhealthy
    })
    severity = CRIT if len(unhealthy) == len(health) else WARN
    report.add(Finding(
        severity, "alb",
        f"{len(unhealthy)} of {len(health)} targets unhealthy in {name}",
        evidence=reasons[:5],
        remediation="Check the health check path/port and whether the app is listening "
                    "on the container port.",
        confidence=0.75,
    ))
