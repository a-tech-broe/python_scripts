# observability/

---

## `golden-signals.py` — latency, traffic, errors, saturation

```bash
./golden-signals.py -r us-east-1                       # last hour, everything
./golden-signals.py -r us-east-1 -w 24h -k lambda -k alb
./golden-signals.py -r us-east-1 -n 'prod-*' --only-problems
./golden-signals.py -r us-east-1 --watch 60            # live refresh
./golden-signals.py -r us-east-1 --json | jq '.targets[] | select(.status=="CRIT")'
./golden-signals.py --explain                          # metric + threshold per column
```

The four signals for every ALB, NLB, Lambda, API Gateway, RDS, DynamoDB table, SQS queue,
ECS service and EC2 instance in a region, in one batched CloudWatch sweep, graded
`OK`/`WARN`/`CRIT` with inline trend sparklines.

| Flag | Purpose |
| --- | --- |
| `--window`, `-w` | `30m`, `6h`, `2d` (5m–14d, default `1h`) |
| `--kind`, `-k` / `--name`, `-n` | Limit by service kind or name glob |
| `--only-problems`, `-P` | Only WARN/CRIT rows |
| `--threshold`, `-T` | Override one: `lambda.errors=1:5` |
| `--thresholds PATH` | JSON file of overrides |
| `--fail-on` | `warn`, `crit` (default), `never` |
| `--watch N` | Refresh every N seconds |
| `--explain` | Print the metric behind every column |

**Exit codes:** `0` clear · `1` warnings · `2` criticals — wire it into cron:

```bash
0 * * * * golden-signals.py -r us-east-1 -w 1h -P || mail -s "golden signals" me@example.com
```

A `—` column means the service does not publish that signal with a per-target dimension
set (DynamoDB latency only exists per `Operation`), so it is left blank rather than filled
with a proxy that looks authoritative. A `—` **status** means CloudWatch returned no
datapoints — an idle resource, not a healthy one, which is why zero traffic never
reads `OK`.

Thresholds are `[warn, crit]` pairs per kind and signal:

```json
{
  "lambda": { "latency": [15000, 30000], "errors": [0.5, 2] },
  "rds":    { "saturation": [60, 80] }
}
```

---

## `log-analyzer.py` — find the signal in the logs

```bash
./log-analyzer.py -r us-east-1 --service payments
./log-analyzer.py -r us-east-1 --group /aws/lambda/my-fn --window 6h
./log-analyzer.py -r us-east-1 -s payments --pattern 'timeout|refused' --tail
./log-analyzer.py -r us-east-1 -g /banking/prod/app --json
```

Pulls error-shaped lines from CloudWatch Logs Insights and clusters them by **shape** —
UUIDs, timestamps, IPs, hex, tokens and numbers are normalised away — so a million-line
log collapses into the handful of distinct things actually going wrong, each with a count,
a first/last seen, and a sample. A histogram sparkline shows when they started.

| Flag | Purpose |
| --- | --- |
| `--service`, `-s` | Log groups are discovered by matching this name |
| `--group`, `-g` | Exact log group; repeatable, skips discovery |
| `--window`, `-w` | Look-back window (default `1h`) |
| `--pattern` | Regex to filter on instead of the default error terms |
| `--top N` / `--scan N` | Shapes to show / lines to pull for clustering |
| `--tail` | Also print raw recent lines |
| `--bin MINUTES` | Histogram bucket size (default 5) |

The default filter is word-based (`error`, `exception`, `fatal`, `timeout`, `refused`,
`denied`, `oom`, …). An earlier version also matched a bare `5\d{2}` for HTTP 5xx, which
cheerfully classified Postgres `distance=65536 kB` checkpoint lines as errors — if you
want status codes, pass an explicit `--pattern`.

**Cost:** Logs Insights bills by bytes scanned. A wide window over a busy group is not
free.
