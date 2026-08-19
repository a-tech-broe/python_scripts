# python_scripts
just scripting...!

## [sre-toolkit](sre-toolkit/)

Incident-oriented tooling for an AWS + ECS + EKS + Terraform environment — read-only
diagnostics that correlate evidence into a probable cause, plus two guarded
resource-management scripts.

```bash
cd sre-toolkit
pip install -r requirements.txt

./aws/aws-whoami.sh -r us-east-1                 # am I in the right account?
./observability/golden-signals.py -r us-east-1   # what is unhealthy right now?
./incident/incident.py -s payments -e prod -r us-east-1 --report incident.md
```

See [sre-toolkit/README.md](sre-toolkit/README.md) for the full layout and status.
