# sre-toolkit

Incident-oriented tooling for an AWS + ECS + EKS + Terraform environment. The point is
not to wrap Linux commands — it is to answer the question you actually have at 3am:

```text
ALERT
  ↓
./incident/incident.py --service payments --env prod
  ↓
ECS · ALB · Lambda · RDS · CloudWatch alarms · logs · CloudTrail
  ↓
correlated evidence
  ↓
probable cause
  ↓
recommended remediation
  ↓
incident summary
```

Everything here is **read-only** except the two resource-management scripts in `aws/`,
which show a plan and require confirmation before touching anything.

## Install

```bash
pip install -r requirements.txt
```

`boto3` is the only dependency. Credentials come from the usual chain
(`~/.aws/credentials`, `AWS_PROFILE`, env vars, SSO, instance role). The bash scripts
additionally use `aws` and `jq`.

## Layout

```text
sre-toolkit/
├── lib/sretk/          shared: AWS session, output, metrics, logs, changes, ECS, findings
├── aws/                account and resource level
│   ├── aws-whoami.sh       identity, region, credential expiry, access probes
│   ├── aws-find.sh         find anything by name / id / ARN / tag
│   ├── ecs-diagnose.py     why is this ECS service unhealthy
│   ├── resource-cleanup.py review-and-delete sweep for a region
│   └── resource-tagger.py  bulk add / remove / inspect tags
├── observability/
│   ├── golden-signals.py   latency · traffic · errors · saturation, region-wide
│   └── log-analyzer.py     cluster error logs into distinct shapes
├── incident/
│   └── incident.py         the correlator — start here during an incident
└── tests/                  offline suite — ./tests/run-tests.sh, no AWS needed
```

See each directory's `README.md` for per-script detail.

## The idea behind `lib/sretk`

Diagnostics do not print. They emit **findings** — a severity, what was observed, the
evidence, a remediation, and *when* it happened. That is what makes correlation possible:
`incident.py` collects findings from six sources, sorts them by severity and confidence,
picks the earliest high-confidence one as the probable cause, and renders the rest as a
timeline.

```python
report.add(Finding(
    CRIT, "ecs", "Deployment rollout failed",
    "circuit breaker: task failed to start",
    remediation="Roll back to the previous task definition, then debug the new one.",
    at=deployment_created_at, confidence=0.9,
))
```

Confidence is what lets a *change* outrank a *symptom*. "12% of invocations are failing"
is a symptom; "someone ran `UpdateFunctionCode` four minutes before that started" is a
cause, so it is scored higher and surfaces as the answer.

## Quick start

```bash
cd sre-toolkit

./aws/aws-whoami.sh -r us-east-1                 # am I in the right account?
./aws/aws-find.sh payments -r us-east-1          # what is "payments", concretely?
./observability/golden-signals.py -r us-east-1   # what is unhealthy region-wide?
./incident/incident.py -s payments -e prod -r us-east-1 --report incident.md
```

## Testing

```bash
./tests/run-tests.sh              # ~1 second, no AWS calls, no credentials
./tests/run-tests.sh -v           # every test name
./tests/run-tests.sh test_ecs     # one module
```

The suite syntax-checks every script, runs 67 unit tests, and confirms every CLI can
print its own help. It covers the paths a live account cannot reach from a laptop — a
broken ECS service, an OOM-killed task, a deploy four minutes before an error spike —
plus the pure functions the diagnostics stand on.

Almost every case in it is a bug that was real at some point: `5\d{2}` matching the
"536" inside `distance=65536 kB`, `\bexception\b` failing to match
`NullPointerException`, a fix applied *during* an incident being blamed for causing it.
Run it before you commit.

For a live smoke test against an account, see the walkthrough in the section above and
start with `./aws/aws-whoami.sh`.

## Status

Built and verified against a live account:

| Area | Script | Notes |
| --- | --- | --- |
| lib | `sretk` | findings, correlation, metrics, logs, changes, ECS |
| aws | `aws-whoami.sh` | ✅ live |
| aws | `aws-find.sh` | ✅ live (56 resources matched in test) |
| aws | `ecs-diagnose.py` | logic verified against stubbed ECS responses — no ECS cluster in the test account |
| aws | `resource-cleanup.py` | ✅ live (read + dry-run paths) |
| aws | `resource-tagger.py` | ✅ live (read + dry-run paths) |
| observability | `golden-signals.py` | ✅ live |
| observability | `log-analyzer.py` | ✅ live |
| incident | `incident.py` | ✅ live end to end; correlation engine unit-verified |

Not built yet, in the order they are most useful:

- `bash/` — `host-health.sh`, `disk-check.sh`, `network-check.sh`, `http-check.sh`,
  `dns-check.sh`, `tls-check.sh`, `process-check.sh`, `docker-cleanup.sh`
- `kubernetes/` — `eks-diagnose.sh`, `pod-debug.sh`, `node-debug.sh`, `resource-check.sh`
- `terraform/` — `tf-diagnose.sh`, `tf-plan.sh`, `state-check.sh`
- `aws/` — `ecs-exec.sh`, `aws-inventory.py`, `aws-cost-check.py`
- `observability/` — `prom-query.py`, `alert-diagnose.py`
- `incident/` — `deployment-diff.py`, `incident-report.py`

### Two deliberate deviations from the original sketch

- **`ecs-diagnose.py`, not `.sh`.** `incident.py` needs the same ECS logic, and writing
  it twice — once in bash, once in Python — guarantees the two eventually disagree about
  what a broken service looks like. It lives in `lib/sretk/ecs.py` and both use it.
- **No Prometheus or Splunk yet.** Neither is reachable from here, and an integration
  that cannot be run is a liability during an incident. CloudWatch Logs Insights fills
  the log role today; `lib/sretk/logs.py` exposes exactly three calls
  (`find_groups`, `error_histogram`, `top_errors`) so a Splunk or Loki backend can be
  dropped in behind them without touching any caller.
