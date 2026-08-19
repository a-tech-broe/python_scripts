# aws/

Account- and resource-level tools. The first three are read-only; the last two write, and
both show a plan and ask before doing so.

---

## `aws-whoami.sh` — am I in the right account?

```bash
./aws-whoami.sh
./aws-whoami.sh --region eu-west-1 --profile prod
```

Account, alias, region, identity ARN, principal type, and **credential expiry** — a
session that dies mid-incident costs more time than checking for it now. Then it probes
the six read-only permissions the rest of the toolkit depends on (EC2, ECS, EKS, logs,
CloudWatch, CloudTrail) so a missing IAM grant surfaces immediately rather than as a
confusing failure three commands later.

Flags root usage in red — if you are the root account during an incident, stop.

---

## `aws-find.sh` — what *is* "payments"?

```bash
./aws-find.sh payments
./aws-find.sh 'prod-*' --region eu-west-1
./aws-find.sh --tag Environment=prod --tags
./aws-find.sh banking --type rds --type lambda
```

One sweep of the Resource Groups Tagging API covers every taggable service, matching your
term against the ARN, the bare id, and every tag key/value. A plain word is treated as a
substring; globs work as written. Results are grouped by `service:type`.

| Flag | Purpose |
| --- | --- |
| `--tag KEY[=VAL]` | Filter by tag, server-side |
| `--type TYPE` | Filter by resource type, e.g. `rds`, `lambda` |
| `--tags` | Show every tag on each match |

---

## `ecs-diagnose.py` — why is this service unhealthy?

```bash
./ecs-diagnose.py -r us-east-1                       # every service in the region
./ecs-diagnose.py -r us-east-1 --service payments
./ecs-diagnose.py -r us-east-1 -c prod -s payments -w 6h --json
```

Reads task counts, deployment rollout state, the deployment circuit breaker, service
events, **stopped-task reasons**, target group health, and CPU/memory — then names the
problem and what to do about it. Stopped tasks are where the real answer usually is:
`OutOfMemoryError`, `CannotPullContainerError`, secrets the execution role cannot read,
ENI setup failures, and health-check failures are each recognised and paired with a
remediation.

**Exit codes:** `0` healthy · `1` warnings · `2` criticals.

The diagnosis lives in `lib/sretk/ecs.py` and is shared with `incident.py`.

---

## `resource-cleanup.py` — review-and-delete sweep

```bash
./resource-cleanup.py --region us-east-1
./resource-cleanup.py --region eu-west-1 --profile sandbox --dry-run
./resource-cleanup.py --region us-east-1 --types 'ec2:*,s3:bucket'
./resource-cleanup.py --list-types            # 46 supported resource types
```

Scan a region → tick checkboxes → review the ordered delete plan → type the region name
to confirm → delete.

**Selector keys:** `↑/↓` or `j/k` move · `space` toggle · `a` group · `A` all · `n` none ·
`/` filter · `enter` confirm · `q` quit.

| Guard | Behaviour |
| --- | --- |
| Default VPC | Its resources are protected and unselectable (`--include-default-vpc` to unlock) |
| Deletion protection | RDS, ALBs and DynamoDB tables that have it are held back |
| AWS-managed | Default security groups/NACLs, main route tables, service-owned ENIs held back |
| `--exclude GLOB` | Protects resources by id or `Name` tag |
| Confirmation | Plan printed first; deletion needs the exact region name typed back |
| `--dry-run` | Full plan, zero API writes |

Deletion runs in dependency order (CloudFormation → ASG → compute → load balancers → data
services → NAT → volumes/ENIs → security groups → subnets → route tables → VPC), waits on
slow terminations, and retries leftovers for up to 3 passes.

---

## `resource-tagger.py` — bulk tag operations

```bash
./resource-tagger.py list   -r us-east-1 --all
./resource-tagger.py add    -r us-east-1 --all --tag Env=dev --tag Owner=jenom
./resource-tagger.py remove -r us-east-1 --has-tag Temp --key Temp
./resource-tagger.py add    -r us-east-1 -t lambda --tag Env=dev --select
```

Built on the Resource Groups Tagging API, so one code path covers every taggable service.
`--region`/`--profile` work on either side of the subcommand.

**Selecting:** `--resource/-R` (ARN, id, `Name` tag, or glob), `--file`, `--type`,
`--has-tag`, `--exclude`, `--all`, `--select` for a checkbox review.

**Safety:** every write prints a plan (`+` added, `~` overwritten `old -> new`, `-`
removed) and asks you to type `yes`. `--dry-run` writes nothing, `--yes` skips the prompt,
`--no-overwrite` fills in only missing keys, and resources already in the desired state
are skipped rather than rewritten. Reserved `aws:` keys are rejected before any call.

Writes are grouped by identical payload and batched 20 at a time; per-resource failures
are reported individually without aborting the run.
