"""Fake AWS clients, so the diagnostics can be tested without an account.

The point is to exercise the paths live AWS cannot reach from a laptop: a broken ECS
service, a task killed for memory, a deploy that landed four minutes before an error
spike. Nothing here touches the network.
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "lib"))

NOW = datetime.now(timezone.utc)


def ago(**kwargs) -> datetime:
    """`ago(minutes=20)` — a timestamp that far in the past."""
    return NOW - timedelta(**kwargs)


class FakeAws:
    """Stands in for `sretk.Aws`, returning whatever the test hands it."""

    region = "us-east-1"
    account = "111111111111"

    def __init__(self, clients: dict[str, Any] | None = None,
                 pages: dict[str, list] | None = None):
        self._clients = clients or {}
        self._pages = pages or {}

    def client(self, service: str):
        if service not in self._clients:
            raise AssertionError(f"test asked for an unstubbed {service!r} client")
        return self._clients[service]

    def paginate(self, service: str, op: str, key: str, **kwargs):
        return iter(self._pages.get(op, []))


class FakeEcs:
    """ECS service that is comprehensively on fire."""

    def __init__(self, service: dict | None = None, tasks: list | None = None):
        self._service = service if service is not None else broken_service()
        self._tasks = tasks if tasks is not None else oom_tasks()

    def describe_services(self, cluster, services, include=None):
        return {"services": [self._service] if self._service else []}

    def describe_tasks(self, cluster, tasks):
        return {"tasks": self._tasks}


class FakeElb:
    def __init__(self, targets: list | None = None):
        self._targets = targets if targets is not None else unhealthy_targets()

    def describe_target_health(self, TargetGroupArn):
        return {"TargetHealthDescriptions": self._targets}


class FakeCloudWatch:
    """Returns the same series for every query, which is enough for threshold logic."""

    def __init__(self, values: list[float] | None = None):
        self._values = values if values is not None else [10.0, 12.0, 11.0]

    def get_metric_data(self, **kwargs):
        return {"MetricDataResults": [
            {"Id": q["Id"], "Values": list(self._values)}
            for q in kwargs["MetricDataQueries"]
        ]}


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #


def broken_service() -> dict:
    return {
        "serviceName": "payments",
        "desiredCount": 4, "runningCount": 0, "pendingCount": 2,
        "deployments": [{
            "status": "PRIMARY",
            "rolloutState": "FAILED",
            "rolloutStateReason": "ECS deployment circuit breaker: task failed to start.",
            "failedTasks": 6,
            "createdAt": ago(minutes=25),
            "taskDefinition": "arn:aws:ecs:us-east-1:1:task-definition/payments:42",
        }],
        "deploymentConfiguration": {
            "deploymentCircuitBreaker": {"enable": True, "rollback": False}},
        "events": [
            {"createdAt": ago(minutes=20),
             "message": "(service payments) is unable to consistently start tasks successfully."},
            {"createdAt": ago(minutes=22),
             "message": "(service payments) (port 8080) is unhealthy in target-group "
                        "tg-payments due to (reason Health checks failed)"},
            {"createdAt": ago(hours=3),
             "message": "(service payments) has reached a steady state."},
        ],
        "loadBalancers": [{
            "targetGroupArn":
                "arn:aws:elasticloadbalancing:us-east-1:1:targetgroup/tg-payments/9d0f1a2b3c",
        }],
    }


def healthy_service() -> dict:
    return {
        "serviceName": "payments",
        "desiredCount": 3, "runningCount": 3, "pendingCount": 0,
        "deployments": [{
            "status": "PRIMARY", "rolloutState": "COMPLETED",
            "rolloutStateReason": "ECS deployment ecs-svc/123 completed.",
            "failedTasks": 0, "createdAt": ago(hours=6),
            "taskDefinition": "arn:aws:ecs:us-east-1:1:task-definition/payments:41",
        }],
        "deploymentConfiguration": {
            "deploymentCircuitBreaker": {"enable": True, "rollback": True}},
        "events": [{"createdAt": ago(hours=5),
                    "message": "(service payments) has reached a steady state."}],
        "loadBalancers": [],
    }


def oom_tasks() -> list[dict]:
    return [
        {"stoppedReason": "OutOfMemoryError: Container killed due to memory usage",
         "stopCode": "EssentialContainerExited", "stoppedAt": ago(minutes=18),
         "containers": [{"name": "app", "exitCode": 137, "reason": "OutOfMemoryError"}]},
        {"stoppedReason": "OutOfMemoryError: Container killed due to memory usage",
         "stopCode": "EssentialContainerExited", "stoppedAt": ago(minutes=15),
         "containers": [{"name": "app", "exitCode": 137, "reason": "OutOfMemoryError"}]},
    ]


def unhealthy_targets() -> list[dict]:
    return [
        {"TargetHealth": {"State": "unhealthy", "Reason": "Target.FailedHealthChecks",
                          "Description": "Health checks failed with these codes: [503]"}},
        {"TargetHealth": {"State": "unhealthy", "Reason": "Target.Timeout",
                          "Description": "Request timed out"}},
    ]


def healthy_targets() -> list[dict]:
    return [{"TargetHealth": {"State": "healthy"}},
            {"TargetHealth": {"State": "healthy"}}]


def change_events(spike: datetime) -> list[dict]:
    """A deploy just before the spike, a config change long before, a fix just after."""
    return [
        {"at": spike - timedelta(minutes=4), "name": "UpdateFunctionCode", "source": "lambda",
         "actor": "deploy-bot", "resources": ["payments-api"], "error": "", "weight": 0.85},
        {"at": spike - timedelta(minutes=90), "name": "PutParameter", "source": "ssm",
         "actor": "alice", "resources": ["/payments/config"], "error": "", "weight": 0.6},
        {"at": spike - timedelta(minutes=12), "name": "AuthorizeSecurityGroupIngress",
         "source": "ec2", "actor": "bob", "resources": ["sg-123"], "error": "", "weight": 0.7},
        {"at": spike + timedelta(minutes=5), "name": "UpdateService", "source": "ecs",
         "actor": "oncall", "resources": ["payments"], "error": "", "weight": 0.85},
        {"at": spike - timedelta(minutes=2), "name": "InvokeModel", "source": "bedrock",
         "actor": "summarizer", "resources": [], "error": "ThrottlingException",
         "weight": 0.35},
    ]
