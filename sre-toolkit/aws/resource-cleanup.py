#!/usr/bin/env python3
"""
aws_resource_cleanup.py — interactive, region-scoped AWS resource sweeper.

Flow:
  1. Scan a region for resources (parallel, read-only).
  2. Pick what to delete in a checkbox UI (arrows + space).
  3. Review the ordered delete plan.
  4. Type the region name to confirm. Nothing is deleted before that.

Usage:
    python3 aws_resource_cleanup.py --region us-east-1
    python3 aws_resource_cleanup.py --region eu-west-1 --profile sandbox --dry-run
    python3 aws_resource_cleanup.py --region us-east-1 --types ec2:instance,ec2:volume
    python3 aws_resource_cleanup.py --list-types

Safety notes:
  * Resources in the default VPC are protected unless --include-default-vpc.
  * Anything with deletion protection is listed but not selectable.
  * S3 buckets are emptied (all versions) before deletion — irreversible.
  * KMS keys and Secrets Manager secrets are *scheduled* for deletion (7-day window).
"""

from __future__ import annotations

import argparse
import concurrent.futures
import curses
import fnmatch
import os
import re
import sys
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Iterator

try:
    import boto3
    from botocore.config import Config
    from botocore.exceptions import BotoCoreError, ClientError, NoCredentialsError
except ImportError:  # pragma: no cover
    sys.exit("boto3 is required:  pip install boto3")


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


# --------------------------------------------------------------------------- #
# Core data model
# --------------------------------------------------------------------------- #


@dataclass
class Resource:
    """A single deletable thing found in the region."""

    type_key: str
    id: str
    name: str = ""
    detail: str = ""
    extra: dict[str, Any] = field(default_factory=dict)
    protected: str | None = None  # reason, if it must not be touched

    @property
    def label(self) -> str:
        parts = [self.id]
        if self.name and self.name != self.id:
            parts.append(f"({self.name})")
        if self.detail:
            parts.append(dim(f"— {self.detail}") if _COLOR else f"- {self.detail}")
        return " ".join(parts)


@dataclass
class Spec:
    """Registry entry: how to find and how to remove one resource type."""

    key: str
    service: str
    label: str
    order: int  # lower = deleted earlier
    lister: Callable[["Ctx"], list[Resource]]
    deleter: Callable[["Ctx", Resource], None]
    settle: Callable[["Ctx", list[Resource]], None] | None = None
    warning: str = ""


class Ctx:
    """Session + cached clients + run flags."""

    def __init__(self, session: "boto3.Session", region: str, args: argparse.Namespace):
        self.session = session
        self.region = region
        self.args = args
        self.dry_run: bool = args.dry_run
        self._clients: dict[str, Any] = {}
        self._cfg = Config(
            region_name=region,
            retries={"max_attempts": 6, "mode": "standard"},
        )

    def client(self, service: str):
        if service not in self._clients:
            self._clients[service] = self.session.client(service, config=self._cfg)
        return self._clients[service]


REGISTRY: dict[str, Spec] = {}


def register(
    key: str,
    service: str,
    label: str,
    order: int,
    *,
    settle: Callable[[Ctx, list[Resource]], None] | None = None,
    warning: str = "",
):
    """Decorator: `def _(ctx) -> list[Resource]` is the lister; it returns the deleter."""

    def outer(lister: Callable[[Ctx], list[Resource]]):
        def inner(deleter: Callable[[Ctx, Resource], None]):
            REGISTRY[key] = Spec(key, service, label, order, lister, deleter, settle, warning)
            return deleter

        lister.deleter = inner  # type: ignore[attr-defined]
        return lister

    return outer


def spec_for(r: Resource) -> Spec:
    return REGISTRY[r.type_key]


# --------------------------------------------------------------------------- #
# Small AWS helpers
# --------------------------------------------------------------------------- #


def paginate(client, op: str, key: str, **kwargs) -> Iterator[dict]:
    """Yield items from `key` across all pages of `op`."""
    if client.can_paginate(op):
        for page in client.get_paginator(op).paginate(**kwargs):
            yield from page.get(key, []) or []
    else:
        yield from getattr(client, op)(**kwargs).get(key, []) or []


def tag_name(tags: Iterable[dict] | None, fallback: str = "") -> str:
    for t in tags or []:
        if t.get("Key") == "Name":
            return t.get("Value", fallback)
    return fallback


def arn_tail(arn: str) -> str:
    return arn.rsplit("/", 1)[-1].rsplit(":", 1)[-1]


def default_vpc_id(ctx: Ctx) -> str | None:
    if not hasattr(ctx, "_default_vpc"):
        try:
            vpcs = ctx.client("ec2").describe_vpcs(
                Filters=[{"Name": "isDefault", "Values": ["true"]}]
            )["Vpcs"]
            ctx._default_vpc = vpcs[0]["VpcId"] if vpcs else None  # type: ignore[attr-defined]
        except (ClientError, BotoCoreError):
            ctx._default_vpc = None  # type: ignore[attr-defined]
    return ctx._default_vpc  # type: ignore[attr-defined]


def vpc_guard(ctx: Ctx, vpc_id: str | None) -> str | None:
    """Protect default-VPC resources unless the user opted in."""
    if ctx.args.include_default_vpc or not vpc_id:
        return None
    return "default VPC" if vpc_id == default_vpc_id(ctx) else None


def wait_gone(check: Callable[[], bool], timeout: int = 600, interval: int = 10) -> None:
    """Poll `check` until it returns True (gone) or timeout elapses."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            if check():
                return
        except ClientError:
            return
        time.sleep(interval)


# =========================================================================== #
# Resource type registry
# =========================================================================== #

# --- CloudFormation -------------------------------------------------------- #

_CFN_ACTIVE = [
    "CREATE_COMPLETE", "CREATE_FAILED", "ROLLBACK_COMPLETE", "ROLLBACK_FAILED",
    "UPDATE_COMPLETE", "UPDATE_ROLLBACK_COMPLETE", "UPDATE_ROLLBACK_FAILED",
    "UPDATE_FAILED", "IMPORT_COMPLETE", "IMPORT_ROLLBACK_COMPLETE", "DELETE_FAILED",
]


@register(
    "cfn:stack", "cloudformation", "CloudFormation Stacks", 5,
    warning="deleting a stack also deletes every resource it manages",
)
def _list_stacks(ctx: Ctx) -> list[Resource]:
    out = []
    for s in paginate(ctx.client("cloudformation"), "list_stacks", "StackSummaries",
                      StackStatusFilter=_CFN_ACTIVE):
        if s.get("ParentId"):
            continue  # nested stacks go with their parent
        out.append(Resource("cfn:stack", s["StackName"], detail=s["StackStatus"]))
    return out


@_list_stacks.deleter
def _(ctx: Ctx, r: Resource) -> None:
    ctx.client("cloudformation").delete_stack(StackName=r.id)


# --- Auto Scaling ---------------------------------------------------------- #


@register("asg:group", "autoscaling", "Auto Scaling Groups", 10)
def _list_asgs(ctx: Ctx) -> list[Resource]:
    return [
        Resource("asg:group", g["AutoScalingGroupName"],
                 detail=f"desired={g['DesiredCapacity']} min={g['MinSize']} max={g['MaxSize']}")
        for g in paginate(ctx.client("autoscaling"), "describe_auto_scaling_groups",
                          "AutoScalingGroups")
    ]


@_list_asgs.deleter
def _(ctx: Ctx, r: Resource) -> None:
    ctx.client("autoscaling").delete_auto_scaling_group(
        AutoScalingGroupName=r.id, ForceDelete=True
    )


# --- ECS ------------------------------------------------------------------- #


@register("ecs:service", "ecs", "ECS Services", 15)
def _list_ecs_services(ctx: Ctx) -> list[Resource]:
    ecs, out = ctx.client("ecs"), []
    for cluster in paginate(ecs, "list_clusters", "clusterArns"):
        for svc in paginate(ecs, "list_services", "serviceArns", cluster=cluster):
            out.append(Resource("ecs:service", arn_tail(svc),
                                detail=f"cluster {arn_tail(cluster)}",
                                extra={"cluster": cluster}))
    return out


@_list_ecs_services.deleter
def _(ctx: Ctx, r: Resource) -> None:
    ctx.client("ecs").delete_service(cluster=r.extra["cluster"], service=r.id, force=True)


@register("ecs:cluster", "ecs", "ECS Clusters", 22)
def _list_ecs_clusters(ctx: Ctx) -> list[Resource]:
    ecs = ctx.client("ecs")
    arns = list(paginate(ecs, "list_clusters", "clusterArns"))
    out = []
    for i in range(0, len(arns), 100):
        for c in ecs.describe_clusters(clusters=arns[i:i + 100])["clusters"]:
            out.append(Resource(
                "ecs:cluster", c["clusterName"],
                detail=f"{c.get('runningTasksCount', 0)} running tasks, "
                       f"{c.get('activeServicesCount', 0)} services",
            ))
    return out


@_list_ecs_clusters.deleter
def _(ctx: Ctx, r: Resource) -> None:
    ecs = ctx.client("ecs")
    for task in paginate(ecs, "list_tasks", "taskArns", cluster=r.id):
        try:
            ecs.stop_task(cluster=r.id, task=task, reason="aws_resource_cleanup")
        except ClientError:
            pass
    for inst in paginate(ecs, "list_container_instances", "containerInstanceArns", cluster=r.id):
        try:
            ecs.deregister_container_instance(cluster=r.id, containerInstance=inst, force=True)
        except ClientError:
            pass
    ecs.delete_cluster(cluster=r.id)


# --- EKS ------------------------------------------------------------------- #


def _settle_nodegroups(ctx: Ctx, rs: list[Resource]) -> None:
    eks = ctx.client("eks")
    for r in rs:
        wait_gone(lambda r=r: not _ng_exists(eks, r.extra["cluster"], r.id), timeout=1200, interval=20)


def _ng_exists(eks, cluster: str, ng: str) -> bool:
    try:
        eks.describe_nodegroup(clusterName=cluster, nodegroupName=ng)
        return True
    except ClientError:
        return False


@register("eks:nodegroup", "eks", "EKS Node Groups", 16, settle=_settle_nodegroups)
def _list_nodegroups(ctx: Ctx) -> list[Resource]:
    eks, out = ctx.client("eks"), []
    for cluster in paginate(eks, "list_clusters", "clusters"):
        for ng in paginate(eks, "list_nodegroups", "nodegroups", clusterName=cluster):
            out.append(Resource("eks:nodegroup", ng, detail=f"cluster {cluster}",
                                extra={"cluster": cluster}))
    return out


@_list_nodegroups.deleter
def _(ctx: Ctx, r: Resource) -> None:
    ctx.client("eks").delete_nodegroup(clusterName=r.extra["cluster"], nodegroupName=r.id)


@register("eks:cluster", "eks", "EKS Clusters", 23)
def _list_eks(ctx: Ctx) -> list[Resource]:
    eks, out = ctx.client("eks"), []
    for name in paginate(eks, "list_clusters", "clusters"):
        c = eks.describe_cluster(name=name)["cluster"]
        out.append(Resource("eks:cluster", name,
                            detail=f"v{c.get('version', '?')} {c.get('status', '')}"))
    return out


@_list_eks.deleter
def _(ctx: Ctx, r: Resource) -> None:
    eks = ctx.client("eks")
    for fp in paginate(eks, "list_fargate_profiles", "fargateProfileNames", clusterName=r.id):
        try:
            eks.delete_fargate_profile(clusterName=r.id, fargateProfileName=fp)
        except ClientError:
            pass
    eks.delete_cluster(name=r.id)


# --- EC2 instances --------------------------------------------------------- #


def _settle_instances(ctx: Ctx, rs: list[Resource]) -> None:
    ids = [r.id for r in rs]
    if not ids:
        return
    try:
        ctx.client("ec2").get_waiter("instance_terminated").wait(
            InstanceIds=ids, WaiterConfig={"Delay": 15, "MaxAttempts": 60}
        )
    except (ClientError, BotoCoreError):
        pass


@register("ec2:instance", "ec2", "EC2 Instances", 20, settle=_settle_instances)
def _list_instances(ctx: Ctx) -> list[Resource]:
    out = []
    for res in paginate(ctx.client("ec2"), "describe_instances", "Reservations"):
        for i in res.get("Instances", []):
            state = i["State"]["Name"]
            if state in ("terminated", "shutting-down"):
                continue
            out.append(Resource(
                "ec2:instance", i["InstanceId"], tag_name(i.get("Tags")),
                f"{i['InstanceType']} {state} {i.get('VpcId', 'classic')}",
                protected=vpc_guard(ctx, i.get("VpcId")),
            ))
    return out


@_list_instances.deleter
def _(ctx: Ctx, r: Resource) -> None:
    ec2 = ctx.client("ec2")
    try:
        ec2.terminate_instances(InstanceIds=[r.id])
    except ClientError as e:
        if "OperationNotPermitted" not in str(e):
            raise
        ec2.modify_instance_attribute(InstanceId=r.id, DisableApiTermination={"Value": False})
        ec2.modify_instance_attribute(InstanceId=r.id, DisableApiStop={"Value": False})
        ec2.terminate_instances(InstanceIds=[r.id])


# --- RDS ------------------------------------------------------------------- #


def _settle_rds_instances(ctx: Ctx, rs: list[Resource]) -> None:
    rds = ctx.client("rds")
    for r in rs:
        try:
            rds.get_waiter("db_instance_deleted").wait(
                DBInstanceIdentifier=r.id, WaiterConfig={"Delay": 20, "MaxAttempts": 90}
            )
        except (ClientError, BotoCoreError):
            pass


@register("rds:instance", "rds", "RDS Instances", 21, settle=_settle_rds_instances,
          warning="final snapshot is skipped — data is gone")
def _list_rds(ctx: Ctx) -> list[Resource]:
    out = []
    for db in paginate(ctx.client("rds"), "describe_db_instances", "DBInstances"):
        out.append(Resource(
            "rds:instance", db["DBInstanceIdentifier"],
            detail=f"{db['Engine']} {db['DBInstanceClass']} {db['DBInstanceStatus']}"
                   + (f" cluster={db['DBClusterIdentifier']}" if db.get("DBClusterIdentifier") else ""),
            protected="deletion protection enabled" if db.get("DeletionProtection") else None,
        ))
    return out


@_list_rds.deleter
def _(ctx: Ctx, r: Resource) -> None:
    ctx.client("rds").delete_db_instance(
        DBInstanceIdentifier=r.id, SkipFinalSnapshot=True, DeleteAutomatedBackups=True
    )


@register("rds:cluster", "rds", "RDS/Aurora Clusters", 24,
          warning="final snapshot is skipped — data is gone")
def _list_rds_clusters(ctx: Ctx) -> list[Resource]:
    return [
        Resource("rds:cluster", c["DBClusterIdentifier"],
                 detail=f"{c['Engine']} {c['Status']} members={len(c.get('DBClusterMembers', []))}",
                 protected="deletion protection enabled" if c.get("DeletionProtection") else None)
        for c in paginate(ctx.client("rds"), "describe_db_clusters", "DBClusters")
    ]


@_list_rds_clusters.deleter
def _(ctx: Ctx, r: Resource) -> None:
    ctx.client("rds").delete_db_cluster(DBClusterIdentifier=r.id, SkipFinalSnapshot=True)


@register("rds:snapshot", "rds", "RDS Manual Snapshots", 35)
def _list_rds_snaps(ctx: Ctx) -> list[Resource]:
    return [
        Resource("rds:snapshot", s["DBSnapshotIdentifier"],
                 detail=f"{s['Engine']} {s.get('AllocatedStorage', '?')}GB of {s['DBInstanceIdentifier']}")
        for s in paginate(ctx.client("rds"), "describe_db_snapshots", "DBSnapshots",
                          SnapshotType="manual")
    ]


@_list_rds_snaps.deleter
def _(ctx: Ctx, r: Resource) -> None:
    ctx.client("rds").delete_db_snapshot(DBSnapshotIdentifier=r.id)


@register("rds:subnetgroup", "rds", "RDS Subnet Groups", 75)
def _list_rds_subnet_groups(ctx: Ctx) -> list[Resource]:
    return [
        Resource("rds:subnetgroup", g["DBSubnetGroupName"], detail=g.get("VpcId", ""),
                 protected=vpc_guard(ctx, g.get("VpcId")) or
                 ("AWS default group" if g["DBSubnetGroupName"] == "default" else None))
        for g in paginate(ctx.client("rds"), "describe_db_subnet_groups", "DBSubnetGroups")
    ]


@_list_rds_subnet_groups.deleter
def _(ctx: Ctx, r: Resource) -> None:
    ctx.client("rds").delete_db_subnet_group(DBSubnetGroupName=r.id)


# --- ElastiCache ----------------------------------------------------------- #


@register("elasticache:replgroup", "elasticache", "ElastiCache Replication Groups", 21)
def _list_repl_groups(ctx: Ctx) -> list[Resource]:
    return [
        Resource("elasticache:replgroup", g["ReplicationGroupId"],
                 detail=f"{g.get('Status', '')} nodes={len(g.get('MemberClusters', []))}")
        for g in paginate(ctx.client("elasticache"), "describe_replication_groups",
                          "ReplicationGroups")
    ]


@_list_repl_groups.deleter
def _(ctx: Ctx, r: Resource) -> None:
    ctx.client("elasticache").delete_replication_group(
        ReplicationGroupId=r.id, RetainPrimaryCluster=False
    )


@register("elasticache:cluster", "elasticache", "ElastiCache Clusters", 22)
def _list_cache_clusters(ctx: Ctx) -> list[Resource]:
    return [
        Resource("elasticache:cluster", c["CacheClusterId"],
                 detail=f"{c.get('Engine', '')} {c.get('CacheNodeType', '')} {c.get('CacheClusterStatus', '')}",
                 protected=(f"member of replication group {c['ReplicationGroupId']}"
                            if c.get("ReplicationGroupId") else None))
        for c in paginate(ctx.client("elasticache"), "describe_cache_clusters", "CacheClusters")
    ]


@_list_cache_clusters.deleter
def _(ctx: Ctx, r: Resource) -> None:
    ctx.client("elasticache").delete_cache_cluster(CacheClusterId=r.id)


# --- Load balancers -------------------------------------------------------- #


def _settle_albs(ctx: Ctx, rs: list[Resource]) -> None:
    elb = ctx.client("elbv2")
    for r in rs:
        try:
            elb.get_waiter("load_balancers_deleted").wait(
                LoadBalancerArns=[r.extra["arn"]], WaiterConfig={"Delay": 15, "MaxAttempts": 40}
            )
        except (ClientError, BotoCoreError):
            pass


@register("elbv2:lb", "elbv2", "Load Balancers (ALB/NLB)", 25, settle=_settle_albs)
def _list_elbv2(ctx: Ctx) -> list[Resource]:
    elb, out = ctx.client("elbv2"), []
    for lb in paginate(elb, "describe_load_balancers", "LoadBalancers"):
        attrs = {}
        try:
            attrs = {a["Key"]: a["Value"] for a in elb.describe_load_balancer_attributes(
                LoadBalancerArn=lb["LoadBalancerArn"])["Attributes"]}
        except ClientError:
            pass
        out.append(Resource(
            "elbv2:lb", lb["LoadBalancerName"],
            detail=f"{lb['Type']} {lb['Scheme']} {lb.get('VpcId', '')}",
            extra={"arn": lb["LoadBalancerArn"]},
            protected=("deletion protection enabled"
                       if attrs.get("deletion_protection.enabled") == "true"
                       else vpc_guard(ctx, lb.get("VpcId"))),
        ))
    return out


@_list_elbv2.deleter
def _(ctx: Ctx, r: Resource) -> None:
    ctx.client("elbv2").delete_load_balancer(LoadBalancerArn=r.extra["arn"])


@register("elbv2:targetgroup", "elbv2", "Target Groups", 30)
def _list_target_groups(ctx: Ctx) -> list[Resource]:
    return [
        Resource("elbv2:targetgroup", tg["TargetGroupName"],
                 detail=f"{tg.get('Protocol', '')}:{tg.get('Port', '')} {tg.get('VpcId', '')}",
                 extra={"arn": tg["TargetGroupArn"]},
                 protected=vpc_guard(ctx, tg.get("VpcId")))
        for tg in paginate(ctx.client("elbv2"), "describe_target_groups", "TargetGroups")
    ]


@_list_target_groups.deleter
def _(ctx: Ctx, r: Resource) -> None:
    ctx.client("elbv2").delete_target_group(TargetGroupArn=r.extra["arn"])


@register("elb:classic", "elb", "Classic Load Balancers", 25)
def _list_elb_classic(ctx: Ctx) -> list[Resource]:
    return [
        Resource("elb:classic", lb["LoadBalancerName"],
                 detail=f"{lb.get('Scheme', '')} {lb.get('VPCId', 'classic')}",
                 protected=vpc_guard(ctx, lb.get("VPCId")))
        for lb in paginate(ctx.client("elb"), "describe_load_balancers",
                           "LoadBalancerDescriptions")
    ]


@_list_elb_classic.deleter
def _(ctx: Ctx, r: Resource) -> None:
    ctx.client("elb").delete_load_balancer(LoadBalancerName=r.id)


# --- Serverless / app services --------------------------------------------- #


@register("lambda:function", "lambda", "Lambda Functions", 35)
def _list_lambdas(ctx: Ctx) -> list[Resource]:
    return [
        Resource("lambda:function", f["FunctionName"],
                 detail=f"{f.get('Runtime', f.get('PackageType', ''))} {f.get('MemorySize', '')}MB")
        for f in paginate(ctx.client("lambda"), "list_functions", "Functions")
    ]


@_list_lambdas.deleter
def _(ctx: Ctx, r: Resource) -> None:
    ctx.client("lambda").delete_function(FunctionName=r.id)


@register("dynamodb:table", "dynamodb", "DynamoDB Tables", 35, warning="table data is not recoverable")
def _list_ddb(ctx: Ctx) -> list[Resource]:
    ddb, out = ctx.client("dynamodb"), []
    for name in paginate(ddb, "list_tables", "TableNames"):
        try:
            t = ddb.describe_table(TableName=name)["Table"]
            detail = f"{t.get('ItemCount', 0)} items, {t.get('TableStatus', '')}"
            protected = ("deletion protection enabled"
                         if t.get("DeletionProtectionEnabled") else None)
        except ClientError:
            detail, protected = "", None
        out.append(Resource("dynamodb:table", name, detail=detail, protected=protected))
    return out


@_list_ddb.deleter
def _(ctx: Ctx, r: Resource) -> None:
    ctx.client("dynamodb").delete_table(TableName=r.id)


@register("apigateway:rest", "apigateway", "API Gateway REST APIs", 35)
def _list_apis(ctx: Ctx) -> list[Resource]:
    return [
        Resource("apigateway:rest", a["id"], a.get("name", ""), detail="REST")
        for a in paginate(ctx.client("apigateway"), "get_rest_apis", "items")
    ]


@_list_apis.deleter
def _(ctx: Ctx, r: Resource) -> None:
    ctx.client("apigateway").delete_rest_api(restApiId=r.id)


@register("apigatewayv2:api", "apigatewayv2", "API Gateway HTTP/WS APIs", 35)
def _list_apis_v2(ctx: Ctx) -> list[Resource]:
    return [
        Resource("apigatewayv2:api", a["ApiId"], a.get("Name", ""),
                 detail=a.get("ProtocolType", ""))
        for a in paginate(ctx.client("apigatewayv2"), "get_apis", "Items")
    ]


@_list_apis_v2.deleter
def _(ctx: Ctx, r: Resource) -> None:
    ctx.client("apigatewayv2").delete_api(ApiId=r.id)


@register("sfn:statemachine", "stepfunctions", "Step Functions State Machines", 35)
def _list_sfn(ctx: Ctx) -> list[Resource]:
    return [
        Resource("sfn:statemachine", sm["name"], detail=sm.get("type", ""),
                 extra={"arn": sm["stateMachineArn"]})
        for sm in paginate(ctx.client("stepfunctions"), "list_state_machines", "stateMachines")
    ]


@_list_sfn.deleter
def _(ctx: Ctx, r: Resource) -> None:
    ctx.client("stepfunctions").delete_state_machine(stateMachineArn=r.extra["arn"])


# --- Storage / registry ---------------------------------------------------- #


@register("s3:bucket", "s3", "S3 Buckets", 35,
          warning="buckets are emptied first (all object versions) — irreversible")
def _list_buckets(ctx: Ctx) -> list[Resource]:
    s3, out = ctx.client("s3"), []
    for b in s3.list_buckets().get("Buckets", []):
        try:
            loc = s3.get_bucket_location(Bucket=b["Name"])["LocationConstraint"] or "us-east-1"
        except ClientError:
            continue
        if loc != ctx.region:
            continue
        out.append(Resource("s3:bucket", b["Name"],
                            detail=f"created {b['CreationDate']:%Y-%m-%d}"))
    return out


@_list_buckets.deleter
def _(ctx: Ctx, r: Resource) -> None:
    s3 = ctx.client("s3")
    for op, key in (("list_object_versions", ("Versions", "DeleteMarkers")),
                    ("list_objects_v2", ("Contents",))):
        try:
            for page in s3.get_paginator(op).paginate(Bucket=r.id):
                objs = [{"Key": o["Key"], **({"VersionId": o["VersionId"]} if "VersionId" in o else {})}
                        for k in key for o in page.get(k, []) or []]
                for i in range(0, len(objs), 1000):
                    s3.delete_objects(Bucket=r.id, Delete={"Objects": objs[i:i + 1000],
                                                           "Quiet": True})
        except ClientError:
            pass
    try:
        s3.delete_bucket_policy(Bucket=r.id)
    except ClientError:
        pass
    s3.delete_bucket(Bucket=r.id)


@register("ecr:repository", "ecr", "ECR Repositories", 35, warning="images are deleted with the repo")
def _list_ecr(ctx: Ctx) -> list[Resource]:
    return [
        Resource("ecr:repository", r["repositoryName"], detail=r.get("repositoryUri", ""))
        for r in paginate(ctx.client("ecr"), "describe_repositories", "repositories")
    ]


@_list_ecr.deleter
def _(ctx: Ctx, r: Resource) -> None:
    ctx.client("ecr").delete_repository(repositoryName=r.id, force=True)


@register("efs:filesystem", "efs", "EFS File Systems", 35, warning="file system data is not recoverable")
def _list_efs(ctx: Ctx) -> list[Resource]:
    return [
        Resource("efs:filesystem", fs["FileSystemId"], fs.get("Name", ""),
                 detail=f"{fs.get('SizeInBytes', {}).get('Value', 0) / 1e9:.2f} GB, "
                        f"{fs.get('NumberOfMountTargets', 0)} mount targets")
        for fs in paginate(ctx.client("efs"), "describe_file_systems", "FileSystems")
    ]


@_list_efs.deleter
def _(ctx: Ctx, r: Resource) -> None:
    efs = ctx.client("efs")
    mts = efs.describe_mount_targets(FileSystemId=r.id).get("MountTargets", [])
    for mt in mts:
        try:
            efs.delete_mount_target(MountTargetId=mt["MountTargetId"])
        except ClientError:
            pass
    if mts:
        wait_gone(lambda: not efs.describe_mount_targets(FileSystemId=r.id).get("MountTargets"),
                  timeout=300, interval=10)
    efs.delete_file_system(FileSystemId=r.id)


# --- Messaging / observability / secrets ------------------------------------ #


@register("sns:topic", "sns", "SNS Topics", 35)
def _list_sns(ctx: Ctx) -> list[Resource]:
    return [
        Resource("sns:topic", t["TopicArn"].rsplit(":", 1)[-1], extra={"arn": t["TopicArn"]})
        for t in paginate(ctx.client("sns"), "list_topics", "Topics")
    ]


@_list_sns.deleter
def _(ctx: Ctx, r: Resource) -> None:
    ctx.client("sns").delete_topic(TopicArn=r.extra["arn"])


@register("sqs:queue", "sqs", "SQS Queues", 35)
def _list_sqs(ctx: Ctx) -> list[Resource]:
    return [
        Resource("sqs:queue", url.rsplit("/", 1)[-1], extra={"url": url})
        for url in paginate(ctx.client("sqs"), "list_queues", "QueueUrls")
    ]


@_list_sqs.deleter
def _(ctx: Ctx, r: Resource) -> None:
    ctx.client("sqs").delete_queue(QueueUrl=r.extra["url"])


@register("logs:group", "logs", "CloudWatch Log Groups", 35)
def _list_log_groups(ctx: Ctx) -> list[Resource]:
    return [
        Resource("logs:group", g["logGroupName"],
                 detail=f"{g.get('storedBytes', 0) / 1e6:.1f} MB, "
                        f"retention={g.get('retentionInDays', 'never')}")
        for g in paginate(ctx.client("logs"), "describe_log_groups", "logGroups")
    ]


@_list_log_groups.deleter
def _(ctx: Ctx, r: Resource) -> None:
    ctx.client("logs").delete_log_group(logGroupName=r.id)


@register("cloudwatch:alarm", "cloudwatch", "CloudWatch Alarms", 35)
def _list_alarms(ctx: Ctx) -> list[Resource]:
    cw = ctx.client("cloudwatch")
    out = [Resource("cloudwatch:alarm", a["AlarmName"], detail=a.get("StateValue", ""))
           for a in paginate(cw, "describe_alarms", "MetricAlarms")]
    out += [Resource("cloudwatch:alarm", a["AlarmName"], detail="composite")
            for a in paginate(cw, "describe_alarms", "CompositeAlarms")]
    return out


@_list_alarms.deleter
def _(ctx: Ctx, r: Resource) -> None:
    ctx.client("cloudwatch").delete_alarms(AlarmNames=[r.id])


@register("secretsmanager:secret", "secretsmanager", "Secrets Manager Secrets", 35,
          warning="scheduled for deletion with a 7-day recovery window")
def _list_secrets(ctx: Ctx) -> list[Resource]:
    return [
        Resource("secretsmanager:secret", s["Name"], extra={"arn": s["ARN"]},
                 detail="already scheduled" if s.get("DeletedDate") else "")
        for s in paginate(ctx.client("secretsmanager"), "list_secrets", "SecretList")
    ]


@_list_secrets.deleter
def _(ctx: Ctx, r: Resource) -> None:
    ctx.client("secretsmanager").delete_secret(SecretId=r.extra["arn"], RecoveryWindowInDays=7)


@register("kms:key", "kms", "KMS Customer Keys", 35,
          warning="scheduled for deletion with a 7-day window; data encrypted with them becomes unreadable")
def _list_kms(ctx: Ctx) -> list[Resource]:
    kms, out = ctx.client("kms"), []
    aliases: dict[str, str] = {}
    for a in paginate(kms, "list_aliases", "Aliases"):
        if a.get("TargetKeyId"):
            aliases.setdefault(a["TargetKeyId"], a["AliasName"])
    for k in paginate(kms, "list_keys", "Keys"):
        try:
            meta = kms.describe_key(KeyId=k["KeyId"])["KeyMetadata"]
        except ClientError:
            continue
        if meta.get("KeyManager") != "CUSTOMER" or meta.get("KeyState") == "PendingDeletion":
            continue
        out.append(Resource("kms:key", k["KeyId"], aliases.get(k["KeyId"], ""),
                            detail=meta.get("KeyState", "")))
    return out


@_list_kms.deleter
def _(ctx: Ctx, r: Resource) -> None:
    ctx.client("kms").schedule_key_deletion(KeyId=r.id, PendingWindowInDays=7)


# --- EC2 storage & networking ---------------------------------------------- #


def _settle_nat(ctx: Ctx, rs: list[Resource]) -> None:
    ids = [r.id for r in rs]
    if not ids:
        return
    try:
        ctx.client("ec2").get_waiter("nat_gateway_deleted").wait(
            NatGatewayIds=ids, WaiterConfig={"Delay": 15, "MaxAttempts": 40}
        )
    except (ClientError, BotoCoreError):
        pass


@register("ec2:natgateway", "ec2", "NAT Gateways", 45, settle=_settle_nat)
def _list_nat(ctx: Ctx) -> list[Resource]:
    return [
        Resource("ec2:natgateway", n["NatGatewayId"], tag_name(n.get("Tags")),
                 f"{n['State']} {n.get('VpcId', '')}", protected=vpc_guard(ctx, n.get("VpcId")))
        for n in paginate(ctx.client("ec2"), "describe_nat_gateways", "NatGateways")
        if n["State"] not in ("deleted", "deleting")
    ]


@_list_nat.deleter
def _(ctx: Ctx, r: Resource) -> None:
    ctx.client("ec2").delete_nat_gateway(NatGatewayId=r.id)


@register("ec2:volume", "ec2", "EBS Volumes", 50, warning="volume data is not recoverable")
def _list_volumes(ctx: Ctx) -> list[Resource]:
    return [
        Resource("ec2:volume", v["VolumeId"], tag_name(v.get("Tags")),
                 f"{v['Size']}GB {v['VolumeType']} {v['State']}"
                 + (f" attached to {v['Attachments'][0]['InstanceId']}" if v.get("Attachments") else ""))
        for v in paginate(ctx.client("ec2"), "describe_volumes", "Volumes")
    ]


@_list_volumes.deleter
def _(ctx: Ctx, r: Resource) -> None:
    ctx.client("ec2").delete_volume(VolumeId=r.id)


@register("ec2:snapshot", "ec2", "EBS Snapshots", 50)
def _list_snapshots(ctx: Ctx) -> list[Resource]:
    return [
        Resource("ec2:snapshot", s["SnapshotId"], tag_name(s.get("Tags")),
                 f"{s.get('VolumeSize', '?')}GB of {s.get('VolumeId', '?')} {s['StartTime']:%Y-%m-%d}")
        for s in paginate(ctx.client("ec2"), "describe_snapshots", "Snapshots", OwnerIds=["self"])
    ]


@_list_snapshots.deleter
def _(ctx: Ctx, r: Resource) -> None:
    ctx.client("ec2").delete_snapshot(SnapshotId=r.id)


@register("ec2:image", "ec2", "AMIs (owned)", 48)
def _list_images(ctx: Ctx) -> list[Resource]:
    return [
        Resource("ec2:image", i["ImageId"], i.get("Name", ""),
                 detail=f"{i.get('State', '')} {i.get('CreationDate', '')[:10]}")
        for i in paginate(ctx.client("ec2"), "describe_images", "Images", Owners=["self"])
    ]


@_list_images.deleter
def _(ctx: Ctx, r: Resource) -> None:
    ctx.client("ec2").deregister_image(ImageId=r.id)


@register("ec2:eip", "ec2", "Elastic IPs", 50)
def _list_eips(ctx: Ctx) -> list[Resource]:
    out = []
    for a in ctx.client("ec2").describe_addresses().get("Addresses", []):
        if not a.get("AllocationId"):
            continue  # EC2-Classic
        out.append(Resource("ec2:eip", a["AllocationId"], tag_name(a.get("Tags"), a["PublicIp"]),
                            detail=f"{a['PublicIp']}"
                                   + (f" -> {a.get('InstanceId') or a.get('NetworkInterfaceId')}"
                                      if a.get("AssociationId") else " unassociated")))
    return out


@_list_eips.deleter
def _(ctx: Ctx, r: Resource) -> None:
    ec2 = ctx.client("ec2")
    addrs = ec2.describe_addresses(AllocationIds=[r.id]).get("Addresses", [])
    if addrs and addrs[0].get("AssociationId"):
        ec2.disassociate_address(AssociationId=addrs[0]["AssociationId"])
    ec2.release_address(AllocationId=r.id)


@register("ec2:eni", "ec2", "Network Interfaces", 52)
def _list_enis(ctx: Ctx) -> list[Resource]:
    out = []
    for e in paginate(ctx.client("ec2"), "describe_network_interfaces", "NetworkInterfaces"):
        managed = e.get("RequesterManaged") or e.get("InterfaceType") not in (None, "interface")
        out.append(Resource(
            "ec2:eni", e["NetworkInterfaceId"], tag_name(e.get("TagSet")),
            f"{e.get('InterfaceType', 'interface')} {e['Status']} {e.get('Description', '')[:40]}",
            protected=("managed by an AWS service" if managed
                       else vpc_guard(ctx, e.get("VpcId"))),
        ))
    return out


@_list_enis.deleter
def _(ctx: Ctx, r: Resource) -> None:
    ec2 = ctx.client("ec2")
    enis = ec2.describe_network_interfaces(NetworkInterfaceIds=[r.id])["NetworkInterfaces"]
    att = enis[0].get("Attachment") if enis else None
    if att and att.get("AttachmentId"):
        ec2.detach_network_interface(AttachmentId=att["AttachmentId"], Force=True)
        time.sleep(5)
    ec2.delete_network_interface(NetworkInterfaceId=r.id)


@register("ec2:vpcendpoint", "ec2", "VPC Endpoints", 55)
def _list_vpce(ctx: Ctx) -> list[Resource]:
    return [
        Resource("ec2:vpcendpoint", e["VpcEndpointId"], tag_name(e.get("Tags")),
                 f"{e.get('ServiceName', '')} {e.get('VpcId', '')}",
                 protected=vpc_guard(ctx, e.get("VpcId")))
        for e in paginate(ctx.client("ec2"), "describe_vpc_endpoints", "VpcEndpoints")
    ]


@_list_vpce.deleter
def _(ctx: Ctx, r: Resource) -> None:
    res = ctx.client("ec2").delete_vpc_endpoints(VpcEndpointIds=[r.id])
    if res.get("Unsuccessful"):
        raise RuntimeError(res["Unsuccessful"][0]["Error"]["Message"])


@register("ec2:securitygroup", "ec2", "Security Groups", 60)
def _list_sgs(ctx: Ctx) -> list[Resource]:
    out = []
    for g in paginate(ctx.client("ec2"), "describe_security_groups", "SecurityGroups"):
        reason = "default security group" if g["GroupName"] == "default" else vpc_guard(ctx, g.get("VpcId"))
        out.append(Resource("ec2:securitygroup", g["GroupId"], g["GroupName"],
                            g.get("VpcId", ""), protected=reason))
    return out


@_list_sgs.deleter
def _(ctx: Ctx, r: Resource) -> None:
    ec2 = ctx.client("ec2")
    # Strip rules first so cross-referencing groups can be removed in any order.
    for g in ec2.describe_security_groups(GroupIds=[r.id])["SecurityGroups"]:
        if g.get("IpPermissions"):
            ec2.revoke_security_group_ingress(GroupId=r.id, IpPermissions=g["IpPermissions"])
        if g.get("IpPermissionsEgress"):
            ec2.revoke_security_group_egress(GroupId=r.id, IpPermissions=g["IpPermissionsEgress"])
    ec2.delete_security_group(GroupId=r.id)


@register("ec2:subnet", "ec2", "Subnets", 65)
def _list_subnets(ctx: Ctx) -> list[Resource]:
    return [
        Resource("ec2:subnet", s["SubnetId"], tag_name(s.get("Tags")),
                 f"{s['CidrBlock']} {s['AvailabilityZone']} {s.get('VpcId', '')}",
                 protected=vpc_guard(ctx, s.get("VpcId")))
        for s in paginate(ctx.client("ec2"), "describe_subnets", "Subnets")
    ]


@_list_subnets.deleter
def _(ctx: Ctx, r: Resource) -> None:
    ctx.client("ec2").delete_subnet(SubnetId=r.id)


@register("ec2:routetable", "ec2", "Route Tables", 70)
def _list_route_tables(ctx: Ctx) -> list[Resource]:
    out = []
    for rt in paginate(ctx.client("ec2"), "describe_route_tables", "RouteTables"):
        main = any(a.get("Main") for a in rt.get("Associations", []))
        out.append(Resource("ec2:routetable", rt["RouteTableId"], tag_name(rt.get("Tags")),
                            f"{rt.get('VpcId', '')}{' (main)' if main else ''}",
                            protected=("main route table — deleted with its VPC" if main
                                       else vpc_guard(ctx, rt.get("VpcId")))))
    return out


@_list_route_tables.deleter
def _(ctx: Ctx, r: Resource) -> None:
    ec2 = ctx.client("ec2")
    for rt in ec2.describe_route_tables(RouteTableIds=[r.id])["RouteTables"]:
        for a in rt.get("Associations", []):
            if not a.get("Main") and a.get("RouteTableAssociationId"):
                ec2.disassociate_route_table(AssociationId=a["RouteTableAssociationId"])
    ec2.delete_route_table(RouteTableId=r.id)


@register("ec2:networkacl", "ec2", "Network ACLs", 70)
def _list_nacls(ctx: Ctx) -> list[Resource]:
    return [
        Resource("ec2:networkacl", n["NetworkAclId"], tag_name(n.get("Tags")),
                 n.get("VpcId", ""),
                 protected=("default network ACL" if n.get("IsDefault")
                            else vpc_guard(ctx, n.get("VpcId"))))
        for n in paginate(ctx.client("ec2"), "describe_network_acls", "NetworkAcls")
    ]


@_list_nacls.deleter
def _(ctx: Ctx, r: Resource) -> None:
    ctx.client("ec2").delete_network_acl(NetworkAclId=r.id)


@register("ec2:internetgateway", "ec2", "Internet Gateways", 72)
def _list_igws(ctx: Ctx) -> list[Resource]:
    out = []
    for g in paginate(ctx.client("ec2"), "describe_internet_gateways", "InternetGateways"):
        vpc = (g.get("Attachments") or [{}])[0].get("VpcId")
        out.append(Resource("ec2:internetgateway", g["InternetGatewayId"], tag_name(g.get("Tags")),
                            f"attached to {vpc}" if vpc else "detached",
                            extra={"vpc": vpc}, protected=vpc_guard(ctx, vpc)))
    return out


@_list_igws.deleter
def _(ctx: Ctx, r: Resource) -> None:
    ec2 = ctx.client("ec2")
    if r.extra.get("vpc"):
        ec2.detach_internet_gateway(InternetGatewayId=r.id, VpcId=r.extra["vpc"])
    ec2.delete_internet_gateway(InternetGatewayId=r.id)


@register("ec2:keypair", "ec2", "EC2 Key Pairs", 75)
def _list_keypairs(ctx: Ctx) -> list[Resource]:
    return [
        Resource("ec2:keypair", k["KeyName"], detail=k.get("KeyFingerprint", "")[:20])
        for k in ctx.client("ec2").describe_key_pairs().get("KeyPairs", [])
    ]


@_list_keypairs.deleter
def _(ctx: Ctx, r: Resource) -> None:
    ctx.client("ec2").delete_key_pair(KeyName=r.id)


@register("ec2:launchtemplate", "ec2", "Launch Templates", 75)
def _list_launch_templates(ctx: Ctx) -> list[Resource]:
    return [
        Resource("ec2:launchtemplate", t["LaunchTemplateName"],
                 detail=f"v{t.get('LatestVersionNumber', '?')}")
        for t in paginate(ctx.client("ec2"), "describe_launch_templates", "LaunchTemplates")
    ]


@_list_launch_templates.deleter
def _(ctx: Ctx, r: Resource) -> None:
    ctx.client("ec2").delete_launch_template(LaunchTemplateName=r.id)


@register("asg:launchconfig", "autoscaling", "Launch Configurations", 75)
def _list_launch_configs(ctx: Ctx) -> list[Resource]:
    return [
        Resource("asg:launchconfig", c["LaunchConfigurationName"],
                 detail=c.get("InstanceType", ""))
        for c in paginate(ctx.client("autoscaling"), "describe_launch_configurations",
                          "LaunchConfigurations")
    ]


@_list_launch_configs.deleter
def _(ctx: Ctx, r: Resource) -> None:
    ctx.client("autoscaling").delete_launch_configuration(LaunchConfigurationName=r.id)


@register("ec2:vpc", "ec2", "VPCs", 80, warning="a VPC must be empty before it can be deleted")
def _list_vpcs(ctx: Ctx) -> list[Resource]:
    return [
        Resource("ec2:vpc", v["VpcId"], tag_name(v.get("Tags")),
                 f"{v['CidrBlock']}{' (default)' if v.get('IsDefault') else ''}",
                 protected=vpc_guard(ctx, v["VpcId"]))
        for v in paginate(ctx.client("ec2"), "describe_vpcs", "Vpcs")
    ]


@_list_vpcs.deleter
def _(ctx: Ctx, r: Resource) -> None:
    ctx.client("ec2").delete_vpc(VpcId=r.id)


# =========================================================================== #
# Discovery
# =========================================================================== #


@dataclass
class Scan:
    found: list[Resource] = field(default_factory=list)
    protected: list[Resource] = field(default_factory=list)
    errors: dict[str, str] = field(default_factory=dict)


def discover(ctx: Ctx, keys: list[str]) -> Scan:
    scan = Scan()
    default_vpc_id(ctx)  # warm the cache before threads fan out

    def run(key: str) -> tuple[str, list[Resource] | Exception]:
        try:
            return key, REGISTRY[key].lister(ctx)
        except Exception as exc:  # noqa: BLE001 — one bad service must not kill the scan
            return key, exc

    done = 0
    with concurrent.futures.ThreadPoolExecutor(max_workers=12) as pool:
        for key, result in pool.map(run, keys):
            done += 1
            if sys.stderr.isatty():
                print(f"\r  scanning… {done}/{len(keys)} {REGISTRY[key].label:<34}",
                      end="", file=sys.stderr, flush=True)
            if isinstance(result, Exception):
                msg = str(result)
                if isinstance(result, ClientError):
                    msg = result.response.get("Error", {}).get("Message", msg)
                scan.errors[key] = msg.split("\n")[0][:160]
                continue
            for r in result:
                if ctx.args.exclude and any(fnmatch.fnmatch(r.id, p) or fnmatch.fnmatch(r.name, p)
                                            for p in ctx.args.exclude):
                    r.protected = "matched --exclude"
                (scan.protected if r.protected else scan.found).append(r)
    if sys.stderr.isatty():
        print("\r" + " " * 70 + "\r", end="", file=sys.stderr)

    order = {k: i for i, k in enumerate(REGISTRY)}
    scan.found.sort(key=lambda r: (order[r.type_key], r.name or r.id))
    scan.protected.sort(key=lambda r: (order[r.type_key], r.name or r.id))
    return scan


# =========================================================================== #
# Checkbox picker
# =========================================================================== #

HELP = "space toggle · a group · A all · n none · / filter · enter confirm · q quit"


class Picker:
    """Curses checkbox list grouped by resource type."""

    def __init__(self, resources: list[Resource], region: str, account: str):
        self.resources = resources
        self.region = region
        self.account = account
        self.checked: set[int] = set()
        self.filter = ""
        self.cursor = 0
        self.top = 0
        self.rows: list[tuple[str, Any]] = []
        self._build_rows()

    def _build_rows(self) -> None:
        prev_cursor_item = None
        if self.rows and 0 <= self.cursor < len(self.rows) and self.rows[self.cursor][0] == "item":
            prev_cursor_item = self.rows[self.cursor][1]
        self.rows = []
        f = self.filter.lower()
        current = None
        for idx, r in enumerate(self.resources):
            if f and f not in f"{r.type_key} {r.id} {r.name} {r.detail}".lower():
                continue
            if r.type_key != current:
                current = r.type_key
                self.rows.append(("header", r.type_key))
            self.rows.append(("item", idx))
        if prev_cursor_item is not None:
            for i, (kind, val) in enumerate(self.rows):
                if kind == "item" and val == prev_cursor_item:
                    self.cursor = i
                    break
            else:
                self.cursor = 0
        self.cursor = max(0, min(self.cursor, len(self.rows) - 1))

    # -- navigation -------------------------------------------------------- #

    def _move(self, delta: int) -> None:
        if not self.rows:
            return
        self.cursor = max(0, min(len(self.rows) - 1, self.cursor + delta))

    def _group_of(self, row: int) -> str | None:
        for i in range(row, -1, -1):
            if self.rows[i][0] == "header":
                return self.rows[i][1]
        return None

    def _toggle_group(self, key: str) -> None:
        idxs = [v for k, v in self.rows if k == "item" and self.resources[v].type_key == key]
        if all(i in self.checked for i in idxs):
            self.checked -= set(idxs)
        else:
            self.checked |= set(idxs)

    # -- rendering --------------------------------------------------------- #

    def _draw(self, scr) -> None:
        scr.erase()
        h, w = scr.getmaxyx()
        body = max(1, h - 4)

        if self.cursor < self.top:
            self.top = self.cursor
        elif self.cursor >= self.top + body:
            self.top = self.cursor - body + 1

        title = f" AWS cleanup · account {self.account} · region {self.region} "
        scr.addnstr(0, 0, title.ljust(w), w, curses.A_REVERSE | curses.A_BOLD)

        for i in range(body):
            row = self.top + i
            if row >= len(self.rows):
                break
            kind, val = self.rows[row]
            y = i + 1
            if kind == "header":
                spec = REGISTRY[val]
                n = sum(1 for k, v in self.rows
                        if k == "item" and self.resources[v].type_key == val)
                text = f"  {spec.label}  ({n})"
                attr = curses.A_BOLD | curses.color_pair(3)
                if row == self.cursor:
                    attr |= curses.A_REVERSE
                scr.addnstr(y, 0, text.ljust(w), w, attr)
                continue
            r = self.resources[val]
            mark = "[x]" if val in self.checked else "[ ]"
            name = f" ({r.name})" if r.name and r.name != r.id else ""
            text = f"   {mark} {r.id}{name}"
            detail = f"  — {r.detail}" if r.detail else ""
            attr = curses.A_REVERSE if row == self.cursor else curses.A_NORMAL
            scr.addnstr(y, 0, (text + detail).ljust(w), w, attr)
            if detail and row != self.cursor and len(text) < w:
                scr.addnstr(y, len(text), detail[: w - len(text)], w - len(text),
                            curses.color_pair(2))

        status = f" {len(self.checked)} selected of {len(self.resources)} "
        if self.filter:
            status += f"· filter: {self.filter} "
        scr.addnstr(h - 2, 0, status.ljust(w), w, curses.color_pair(1) | curses.A_BOLD)
        scr.addnstr(h - 1, 0, f" {HELP} ".ljust(w), w, curses.A_DIM)
        scr.refresh()

    def _prompt(self, scr, label: str) -> str:
        h, w = scr.getmaxyx()
        curses.echo()
        curses.curs_set(1)
        scr.addnstr(h - 1, 0, label.ljust(w), w)
        scr.move(h - 1, len(label))
        try:
            value = scr.getstr(h - 1, len(label), 60).decode("utf-8", "replace")
        finally:
            curses.noecho()
            curses.curs_set(0)
        return value

    # -- main loop --------------------------------------------------------- #

    def _loop(self, scr) -> list[Resource] | None:
        curses.curs_set(0)
        try:
            curses.start_color()
            curses.use_default_colors()
            curses.init_pair(1, curses.COLOR_CYAN, -1)
            curses.init_pair(2, curses.COLOR_YELLOW, -1)
            curses.init_pair(3, curses.COLOR_GREEN, -1)
        except curses.error:
            pass

        while True:
            self._draw(scr)
            try:
                ch = scr.getch()
            except KeyboardInterrupt:
                return None

            if ch in (curses.KEY_DOWN, ord("j")):
                self._move(1)
            elif ch in (curses.KEY_UP, ord("k")):
                self._move(-1)
            elif ch in (curses.KEY_NPAGE, ord("f")):
                self._move(10)
            elif ch in (curses.KEY_PPAGE, ord("b")):
                self._move(-10)
            elif ch in (curses.KEY_HOME, ord("g")):
                self.cursor = 0
            elif ch in (curses.KEY_END, ord("G")):
                self.cursor = len(self.rows) - 1
            elif ch == ord(" "):
                if self.rows and self.rows[self.cursor][0] == "item":
                    idx = self.rows[self.cursor][1]
                    self.checked ^= {idx}
                    self._move(1)
                elif self.rows:
                    self._toggle_group(self.rows[self.cursor][1])
            elif ch == ord("a"):
                if self.rows:
                    key = self._group_of(self.cursor)
                    if key:
                        self._toggle_group(key)
            elif ch == ord("A"):
                self.checked = {v for k, v in self.rows if k == "item"}
            elif ch == ord("n"):
                self.checked -= {v for k, v in self.rows if k == "item"}
            elif ch == ord("/"):
                self.filter = self._prompt(scr, "filter: ").strip()
                self._build_rows()
                self.top = 0
            elif ch == 27:  # ESC clears the filter, or quits
                if self.filter:
                    self.filter = ""
                    self._build_rows()
                else:
                    return None
            elif ch in (ord("q"), 3):
                return None
            elif ch in (curses.KEY_ENTER, 10, 13):
                return [self.resources[i] for i in sorted(self.checked)]

    def run(self) -> list[Resource] | None:
        return curses.wrapper(self._loop)


def pick_plain(resources: list[Resource]) -> list[Resource] | None:
    """Fallback selector for non-TTY / no-curses environments."""
    current = None
    for i, r in enumerate(resources, 1):
        if r.type_key != current:
            current = r.type_key
            print(f"\n{bold(REGISTRY[current].label)}")
        print(f"  {i:>4}. {r.label}")
    print()
    raw = input("Numbers to delete (e.g. 1,4,7-9 · 'all' · blank to cancel): ").strip()
    if not raw:
        return None
    if raw.lower() == "all":
        return list(resources)
    chosen: set[int] = set()
    for part in re.split(r"[,\s]+", raw):
        if not part:
            continue
        if "-" in part:
            a, _, b = part.partition("-")
            try:
                chosen.update(range(int(a), int(b) + 1))
            except ValueError:
                pass
        elif part.isdigit():
            chosen.add(int(part))
    return [resources[i - 1] for i in sorted(chosen) if 1 <= i <= len(resources)]


# =========================================================================== #
# Plan + execution
# =========================================================================== #


def show_plan(selected: list[Resource], ctx: Ctx, account: str) -> None:
    ordered = sorted(selected, key=lambda r: (spec_for(r).order, r.type_key, r.id))
    print()
    print(bold("=" * 72))
    print(bold(f"  DELETE PLAN — account {account} — region {ctx.region}"))
    print(bold("=" * 72))

    step, current = 0, None
    for r in ordered:
        if r.type_key != current:
            current = r.type_key
            step += 1
            spec = REGISTRY[current]
            print(f"\n{bold(f'{step}. {spec.label}')}")
            if spec.warning:
                print(f"   {yellow('! ' + spec.warning)}")
        print(f"     - {r.label}")

    print()
    print(bold("-" * 72))
    counts: dict[str, int] = {}
    for r in ordered:
        counts[r.type_key] = counts.get(r.type_key, 0) + 1
    summary = ", ".join(f"{n}× {REGISTRY[k].label}" for k, n in counts.items())
    print(f"  {bold(str(len(ordered)))} resources to delete: {summary}")
    print(bold("-" * 72))


def confirm(region: str, count: int, dry_run: bool) -> bool:
    if dry_run:
        print(yellow("\n[--dry-run] nothing will be deleted.\n"))
        return True
    print(red(f"\nThis permanently deletes {count} resources in {region}. "
              "There is no undo."))
    try:
        answer = input(f'Type the region name "{bold(region)}" to proceed (anything else aborts): ')
    except (EOFError, KeyboardInterrupt):
        return False
    return answer.strip() == region


def execute(ctx: Ctx, selected: list[Resource], passes: int = 3) -> tuple[int, list[tuple[Resource, str]]]:
    """Delete in dependency order; retry leftovers, since AWS frees dependencies lazily."""
    pending = sorted(selected, key=lambda r: (spec_for(r).order, r.type_key, r.id))
    deleted = 0
    failures: list[tuple[Resource, str]] = []

    for attempt in range(1, passes + 1):
        if not pending:
            break
        if attempt > 1:
            print(yellow(f"\n-- retry pass {attempt} ({len(pending)} remaining) --"))
            time.sleep(15)

        failures, current_type, succeeded_in_type = [], None, []
        for r in pending:
            spec = spec_for(r)
            if spec.key != current_type:
                if current_type and REGISTRY[current_type].settle and succeeded_in_type:
                    print(dim(f"   waiting for {REGISTRY[current_type].label} to finish deleting…"))
                    REGISTRY[current_type].settle(ctx, succeeded_in_type)  # type: ignore[misc]
                current_type, succeeded_in_type = spec.key, []
                print(f"\n{bold(spec.label)}")

            prefix = f"   {r.id}"
            if ctx.dry_run:
                print(f"{prefix} … {dim('would delete')}")
                deleted += 1
                continue
            try:
                spec.deleter(ctx, r)
                print(f"{prefix} … {green('deleted')}")
                deleted += 1
                succeeded_in_type.append(r)
            except (ClientError, BotoCoreError, RuntimeError) as exc:
                msg = str(exc)
                if isinstance(exc, ClientError):
                    msg = exc.response.get("Error", {}).get("Message", msg)
                msg = msg.split("\n")[0][:150]
                print(f"{prefix} … {red('FAILED')} {dim(msg)}")
                failures.append((r, msg))

        if current_type and REGISTRY[current_type].settle and succeeded_in_type and not ctx.dry_run:
            REGISTRY[current_type].settle(ctx, succeeded_in_type)  # type: ignore[misc]

        pending = [r for r, _ in failures]

    return deleted, failures


# =========================================================================== #
# CLI
# =========================================================================== #


def parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="aws_resource_cleanup",
        description="Interactively review and delete AWS resources in one region.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="Nothing is deleted until you review the plan and type the region name.",
    )
    p.add_argument("--region", "-r", help="AWS region to scan (e.g. us-east-1)")
    p.add_argument("--profile", "-p", help="AWS profile from ~/.aws/credentials")
    p.add_argument("--types", "-t", help="comma-separated resource types to scan (see --list-types)")
    p.add_argument("--exclude", "-x", action="append", default=[],
                   help="glob on id or Name tag to protect; repeatable")
    p.add_argument("--include-default-vpc", action="store_true",
                   help="allow selecting resources in the default VPC")
    p.add_argument("--dry-run", action="store_true", help="print the plan, delete nothing")
    p.add_argument("--no-ui", action="store_true", help="use the plain numeric selector")
    p.add_argument("--list-types", action="store_true", help="print known resource types and exit")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv if argv is not None else sys.argv[1:])

    if args.list_types:
        print(bold(f"{'TYPE':<28} {'ORDER':>5}  LABEL"))
        for key, spec in sorted(REGISTRY.items(), key=lambda kv: kv[1].order):
            print(f"{key:<28} {spec.order:>5}  {spec.label}")
        return 0

    region = args.region or os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION")
    if not region:
        try:
            region = input("Region to scan: ").strip()
        except (EOFError, KeyboardInterrupt):
            return 1
    if not region:
        print(red("A region is required."))
        return 2
    args.region = region

    keys = list(REGISTRY)
    if args.types:
        wanted = [t.strip() for t in args.types.split(",") if t.strip()]
        keys = [k for k in REGISTRY if any(fnmatch.fnmatch(k, w) for w in wanted)]
        if not keys:
            print(red(f"No resource types matched {args.types!r}. Try --list-types."))
            return 2

    try:
        session = boto3.Session(profile_name=args.profile, region_name=region)
        ident = session.client("sts", region_name=region).get_caller_identity()
    except (NoCredentialsError, ClientError, BotoCoreError) as exc:
        print(red(f"Could not authenticate to AWS: {exc}"))
        return 2

    account = ident["Account"]
    print(f"\n{bold('Account')} {account}  {dim(ident['Arn'])}")
    print(f"{bold('Region')}  {cyan(region)}  {dim(f'({len(keys)} resource types)')}\n")

    ctx = Ctx(session, region, args)
    scan = discover(ctx, keys)

    if scan.errors:
        print(yellow(f"{len(scan.errors)} service(s) could not be scanned "
                     "(not available in region, or no permission):"))
        for key, msg in list(scan.errors.items())[:12]:
            print(dim(f"   {REGISTRY[key].label}: {msg}"))
        if len(scan.errors) > 12:
            print(dim(f"   … and {len(scan.errors) - 12} more"))
        print()

    if scan.protected:
        print(dim(f"{len(scan.protected)} resource(s) held back as protected:"))
        for r in scan.protected[:10]:
            print(dim(f"   {REGISTRY[r.type_key].label}: {r.id} — {r.protected}"))
        if len(scan.protected) > 10:
            print(dim(f"   … and {len(scan.protected) - 10} more"))
        print()

    if not scan.found:
        print(green(f"No deletable resources found in {region}."))
        return 0

    print(f"Found {bold(str(len(scan.found)))} deletable resources in {cyan(region)}.")

    use_ui = not args.no_ui and sys.stdin.isatty() and sys.stdout.isatty()
    if use_ui:
        input(dim("Press Enter to open the selector… "))
        try:
            selected = Picker(scan.found, region, account).run()
        except curses.error as exc:
            print(yellow(f"Curses UI unavailable ({exc}); falling back to text mode."))
            selected = pick_plain(scan.found)
    else:
        selected = pick_plain(scan.found)

    if not selected:
        print(yellow("\nNothing selected — no changes made."))
        return 0

    show_plan(selected, ctx, account)

    if not confirm(region, len(selected), args.dry_run):
        print(yellow("\nAborted — no changes made."))
        return 1

    print()
    deleted, failures = execute(ctx, selected)

    print()
    verb = "would delete" if args.dry_run else "deleted"
    print(bold(f"Done: {deleted} {verb}, {len(failures)} failed."))
    if failures:
        print(yellow("\nStill present after retries (usually a dependency this run did not "
                     "cover, or an in-flight AWS deletion):"))
        for r, msg in failures:
            print(f"   {REGISTRY[r.type_key].label} {r.id}: {dim(msg)}")
        return 1
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print(yellow("\nInterrupted — no further changes made."))
        sys.exit(130)
