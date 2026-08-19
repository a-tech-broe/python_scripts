# incident/

## `incident.py` — alert to probable cause, in one command

```bash
./incident.py -r us-east-1 --service payments --env prod
./incident.py -r us-east-1 -s payments -w 6h --report incident.md
./incident.py -r us-east-1 -s payments --json | jq .probable_cause
./incident.py -r us-east-1 -s payments --no-logs --no-changes
```

Give it a service *name*. It does not need to know whether that name is an ECS service,
a Lambda function, a load balancer, or all three.

### What it does

1. **Resolve** — sweeps the region in parallel for anything whose name matches: ECS
   services, Lambda functions, ALBs, target groups, RDS instances, log groups, and
   CloudWatch alarms. `--env prod` narrows further.
2. **Collect symptoms** — each resource type has a checker that reads its real signals
   (task counts, rollout state, stopped-task reasons, target health, error rates,
   throttles, duration against timeout, CPU, storage headroom, disk latency).
3. **Date the incident** — the earliest hard signal: an alarm transitioning to ALARM, an
   error spike in the logs, a failed deployment.
4. **Ask what changed** — CloudTrail mutations in the window, correlated against that
   time. A disruptive API call minutes before the incident scores highest.
5. **Rank and report** — findings sorted by severity then confidence; the earliest
   high-confidence one becomes the probable cause, with its remediation.

### Why the answer is usually right

Confidence is scored so that a **cause** outranks a **symptom**:

| Finding | Confidence |
| --- | --- |
| `UpdateFunctionCode` ran 4 min before the incident | 0.92 |
| Deployment rollout failed | 0.90 |
| Container killed for exceeding its memory limit | 0.85 |
| 12% of invocations failing | 0.60 |
| 480 error lines in the logs | 0.55 |
| CPU peaked at 91% | 0.50 |

Correlation rules that keep it honest:

- Changes **after** the incident started are never suspects — a fix is not a cause.
- Changes more than 30 minutes before are dropped; proximity is weighted inside that.
- Read-only API calls and KMS/Insights noise are filtered out of CloudTrail entirely,
  so a real deploy is not buried under a thousand `Decrypt` calls.
- Ties break toward the **earliest** signal, because what happened first usually explains
  what happened after.

### Flags

| Flag | Purpose |
| --- | --- |
| `--service`, `-s` | Service name or substring (required) |
| `--env`, `-e` | Environment filter, e.g. `prod` |
| `--region`, `-r` / `--profile`, `-p` | Where to look |
| `--window`, `-w` | Look-back window: `30m`, `6h`, `2d` (default `1h`) |
| `--report PATH` | Write a markdown incident summary |
| `--json` | Machine-readable output (chatter goes to stderr) |
| `--no-logs` | Skip Logs Insights — it bills by bytes scanned |
| `--no-changes` | Skip the CloudTrail lookup |

**Exit codes:** `0` clear · `1` warnings · `2` criticals.

### The generated summary

`--report incident.md` writes a summary with the probable cause, a findings table, the
timeline, the supporting evidence, and what was and was not checked — ready to paste into
a ticket or a postmortem while the incident is still warm.

### Cost note

Logs Insights charges for bytes scanned. `incident.py` queries at most 20 log groups over
your window; on a busy service a `-w 2d` run is not free. `--no-logs` skips it entirely.
