"""What changed recently — CloudTrail management events.

"What changed?" is the first question worth asking in an incident, and CloudTrail is
the only source that answers it across every service at once. Read-only calls to
Describe/List/Get are filtered out; only mutations survive.
"""

from __future__ import annotations

import re
import time
from datetime import datetime, timedelta
from typing import Any

from .aws import Aws, AwsError, utc_window

_READ_ONLY = re.compile(r"^(Describe|List|Get|Lookup|Head|Search|Query|Scan|BatchGet|"
                        r"Select|Preview|Filter|Test|Validate|Check|Estimate)")

#: Calls that mutate nothing anyone cares about during an incident. KMS data-plane
#: operations and Logs Insights queries otherwise bury the real changes — and
#: `StartQuery` is this toolkit's own log analyzer showing up in its own timeline.
_NOISE_EVENTS = {
    "Decrypt", "Encrypt", "GenerateDataKey", "GenerateDataKeyWithoutPlaintext",
    "GenerateDataKeyPair", "GenerateRandom", "ReEncrypt", "Sign", "Verify",
    "StartQuery", "StopQuery", "StartQueryExecution", "StopQueryExecution",
    "AssumeRole", "AssumeRoleWithSAML", "AssumeRoleWithWebIdentity",
    "RenewRole", "CreateLogStream", "PutLogEvents", "InvokeFunction", "Invoke",
    "UploadServerCertificate", "TagResource", "UntagResource",
    "InvokeModel", "InvokeModelWithResponseStream", "Converse", "ConverseStream",
    "PutMetricData", "PutObject", "GetObject", "DeleteObject",
}

#: Events that most often precede an outage, and how strongly they implicate a change.
_HIGH_SIGNAL = {
    "UpdateService": 0.85, "CreateService": 0.7, "DeleteService": 0.9,
    "RegisterTaskDefinition": 0.75, "UpdateFunctionCode": 0.85,
    "UpdateFunctionConfiguration": 0.8, "PublishVersion": 0.6,
    "UpdateStack": 0.8, "CreateStack": 0.6, "DeleteStack": 0.9,
    "ModifyDBInstance": 0.8, "RebootDBInstance": 0.85, "DeleteDBInstance": 0.95,
    "ModifyTargetGroup": 0.7, "DeregisterTargets": 0.8, "RegisterTargets": 0.5,
    "AuthorizeSecurityGroupIngress": 0.7, "RevokeSecurityGroupIngress": 0.85,
    "ModifyNetworkInterfaceAttribute": 0.6, "TerminateInstances": 0.9,
    "UpdateAutoScalingGroup": 0.75, "SetDesiredCapacity": 0.75,
    "PutScalingPolicy": 0.5, "UpdateNodegroupConfig": 0.8, "UpdateClusterConfig": 0.8,
    "PutRolePolicy": 0.7, "AttachRolePolicy": 0.7, "DetachRolePolicy": 0.85,
    "DeleteRolePolicy": 0.85, "PutBucketPolicy": 0.7, "DisableKey": 0.9,
    "ScheduleKeyDeletion": 0.9, "UpdateSecret": 0.6, "PutParameter": 0.6,
}


def _actor(event: dict[str, Any]) -> str:
    identity = event.get("UserIdentity") or {}
    if isinstance(identity, str):
        return identity
    for key in ("userName", "arn", "principalId", "type"):
        value = identity.get(key)
        if value:
            return str(value).rsplit("/", 1)[-1]
    return event.get("Username") or "unknown"


def recent(aws: Aws, window: timedelta, resource: str | None = None,
           limit: int = 200, deadline_seconds: float = 10.0,
           between: tuple[datetime, datetime] | None = None) -> list[dict[str, Any]]:
    """Mutating CloudTrail events in the window, newest first.

    `resource` narrows the lookup server-side by resource name, which is much faster
    but only matches what CloudTrail recorded as the resource — so callers should
    fall back to an unfiltered sweep when it comes back empty.

    LookupEvents is throttled to a couple of calls per second, so an unfiltered sweep
    over a wide window can take minutes. `deadline_seconds` caps that: better to
    return the most recent changes than to hang while someone is waiting on an
    answer. Pass `between` to look at an exact interval instead of the trailing
    window — far faster and far more relevant once the incident time is known.
    """
    start, end = between if between else utc_window(window)
    kwargs: dict[str, Any] = {"StartTime": start, "EndTime": end, "MaxResults": 50}
    if resource:
        kwargs["LookupAttributes"] = [{"AttributeKey": "ResourceName",
                                       "AttributeValue": resource}]

    events: list[dict[str, Any]] = []
    cutoff = time.monotonic() + deadline_seconds
    try:
        client = aws.client("cloudtrail")
        paginator = client.get_paginator("lookup_events")
        for page in paginator.paginate(**kwargs):
            if time.monotonic() > cutoff:
                break
            for event in page.get("Events", []):
                name = event.get("EventName", "")
                error = _error_of(event)
                # Routine calls are noise only when they succeed. A *failed* Decrypt,
                # AssumeRole or InvokeModel is exactly what you want to see mid-incident.
                if not error and (_READ_ONLY.match(name) or name in _NOISE_EVENTS):
                    continue
                events.append({
                    "at": event.get("EventTime"),
                    "name": name,
                    "source": (event.get("EventSource") or "").split(".")[0],
                    "actor": _actor(event),
                    "resources": [r.get("ResourceName", "") for r in event.get("Resources", [])
                                  if r.get("ResourceName")],
                    "error": error,
                    "weight": _HIGH_SIGNAL.get(name, 0.35),
                })
                if len(events) >= limit:
                    return events
    except AwsError:
        return events
    return events


def around(aws: Aws, when: datetime, before: timedelta = timedelta(hours=2),
           after: timedelta = timedelta(minutes=10), **kwargs) -> list[dict[str, Any]]:
    """Changes in a tight interval around a known incident time.

    Once the incident has a timestamp this is what to use: it asks CloudTrail a much
    smaller question, so it answers in seconds instead of minutes.
    """
    return recent(aws, before + after, between=(when - before, when + after), **kwargs)


def _error_of(event: dict[str, Any]) -> str:
    """CloudTrail records failed API calls too — a failed change is itself a signal."""
    raw = event.get("CloudTrailEvent")
    if not raw or not isinstance(raw, str):
        return ""
    match = re.search(r'"errorCode"\s*:\s*"([^"]+)"', raw)
    return match.group(1) if match else ""


def correlate(events: list[dict[str, Any]], when: datetime | None,
              tolerance: timedelta = timedelta(minutes=30)) -> list[dict[str, Any]]:
    """Changes that landed shortly before `when` — the prime suspects.

    Ordered by how close they were to the incident, weighted by how disruptive the
    API call usually is.
    """
    if not when:
        return []
    suspects = []
    for event in events:
        at = event.get("at")
        if not at or event.get("error"):
            continue  # a call that failed is a symptom, not a change that caused one
        gap = (when - at).total_seconds()
        if 0 <= gap <= tolerance.total_seconds():
            proximity = 1 - (gap / tolerance.total_seconds())
            suspects.append({**event, "gap_seconds": gap,
                             "score": event["weight"] * (0.5 + 0.5 * proximity)})
    return sorted(suspects, key=lambda e: -e["score"])


def describe(event: dict[str, Any]) -> str:
    who = event.get("actor", "unknown")
    what = event.get("name", "?")
    where = ", ".join(event.get("resources", [])[:2])
    text = f"{what} by {who}"
    if where:
        text += f" on {where}"
    if event.get("error"):
        text += f" (failed: {event['error']})"
    return text
