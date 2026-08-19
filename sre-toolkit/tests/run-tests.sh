#!/usr/bin/env bash
#
# run-tests.sh — offline test suite for the toolkit.
#
# No AWS calls, no credentials, no network. Runs in about a second, so there is no
# excuse for not running it before a commit.
#
#   ./tests/run-tests.sh              # quiet
#   ./tests/run-tests.sh -v           # every test name
#   ./tests/run-tests.sh test_ecs     # one module
#
# Also syntax-checks every script in the toolkit, which catches the class of mistake
# that only shows up when you are already in an incident.

set -euo pipefail

cd "$(dirname "$0")/.."
export PYTHONPATH="$PWD/lib:${PYTHONPATH:-}"

VERBOSITY=1
MODULE=""
while [[ $# -gt 0 ]]; do
    case "$1" in
        -v|--verbose) VERBOSITY=2; shift ;;
        -h|--help)    sed -n '2,14p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *)            MODULE="$1"; shift ;;
    esac
done

if [[ -t 1 && -z "${NO_COLOR:-}" ]]; then
    BOLD=$'\033[1m'; DIM=$'\033[2m'; RED=$'\033[31m'; GREEN=$'\033[32m'; RESET=$'\033[0m'
else
    BOLD=""; DIM=""; RED=""; GREEN=""; RESET=""
fi

fail() { echo "${RED}✗ $1${RESET}" >&2; exit 1; }

echo "${BOLD}syntax${RESET}"
python3 -m py_compile lib/sretk/*.py aws/*.py observability/*.py incident/*.py tests/*.py \
    || fail "python syntax errors"
echo "  ${GREEN}✓${RESET} python  ${DIM}$(ls lib/sretk/*.py aws/*.py observability/*.py incident/*.py | wc -l | tr -d ' ') files${RESET}"

for script in aws/*.sh tests/*.sh; do
    bash -n "$script" || fail "bash syntax error in $script"
done
echo "  ${GREEN}✓${RESET} bash    ${DIM}$(ls aws/*.sh tests/*.sh | wc -l | tr -d ' ') files${RESET}"

python3 -c "import sretk" || fail "sretk does not import"
echo "  ${GREEN}✓${RESET} imports ${DIM}sretk${RESET}"

echo
echo "${BOLD}unit tests${RESET} ${DIM}(no AWS calls)${RESET}"
if [[ -n "$MODULE" ]]; then
    python3 -m unittest "tests.${MODULE%.py}" -v
else
    python3 -m unittest discover -s tests -t . -p 'test_*.py' \
        $([[ $VERBOSITY -eq 2 ]] && echo "-v")
fi

# Every CLI must at least be able to print its own help without touching AWS.
echo
echo "${BOLD}cli help${RESET}"
for script in aws/ecs-diagnose.py aws/resource-cleanup.py aws/resource-tagger.py \
              observability/golden-signals.py observability/log-analyzer.py \
              incident/incident.py; do
    python3 "$script" --help >/dev/null 2>&1 || fail "$script --help failed"
    printf "  ${GREEN}✓${RESET} %s\n" "$script"
done
for script in aws/aws-whoami.sh aws/aws-find.sh; do
    bash "$script" --help >/dev/null 2>&1 || fail "$script --help failed"
    printf "  ${GREEN}✓${RESET} %s\n" "$script"
done

find . -name __pycache__ -type d -exec rm -rf {} + 2>/dev/null || true
echo
echo "${GREEN}${BOLD}all green${RESET}"
