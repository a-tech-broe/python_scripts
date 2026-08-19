#!/usr/bin/env bash
#
# aws-whoami.sh — who am I, where am I, and can I do anything here?
#
# The first thing to run in an incident: confirms which account and region your
# shell is actually pointed at before you start changing things in the wrong one.
#
#   ./aws-whoami.sh
#   ./aws-whoami.sh --region eu-west-1 --profile prod
#
# Read-only.

set -euo pipefail

REGION=""
PROFILE=""

usage() {
    sed -n '2,12p' "$0" | sed 's/^# \{0,1\}//'
    exit "${1:-0}"
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        -r|--region)  REGION="${2:-}"; shift 2 ;;
        -p|--profile) PROFILE="${2:-}"; shift 2 ;;
        -h|--help)    usage 0 ;;
        *) echo "unknown argument: $1" >&2; usage 2 ;;
    esac
done

command -v aws >/dev/null || { echo "aws CLI not found" >&2; exit 2; }
command -v jq  >/dev/null || { echo "jq not found" >&2; exit 2; }

AWS=(aws)
[[ -n "$PROFILE" ]] && AWS+=(--profile "$PROFILE")
[[ -n "$REGION"  ]] && AWS+=(--region  "$REGION")

if [[ -t 1 && -z "${NO_COLOR:-}" ]]; then
    BOLD=$'\033[1m'; DIM=$'\033[2m'; RED=$'\033[31m'; GREEN=$'\033[32m'
    YELLOW=$'\033[33m'; CYAN=$'\033[36m'; RESET=$'\033[0m'
else
    BOLD=""; DIM=""; RED=""; GREEN=""; YELLOW=""; CYAN=""; RESET=""
fi

kv() { printf "  %s%-18s%s %s\n" "$DIM" "$1" "$RESET" "$2"; }

if ! IDENTITY=$("${AWS[@]}" sts get-caller-identity --output json 2>&1); then
    echo "${RED}Not authenticated:${RESET} ${IDENTITY}" >&2
    echo "${DIM}Try: aws sso login --profile <name>, or check AWS_PROFILE${RESET}" >&2
    exit 1
fi

ACCOUNT=$(jq -r .Account <<<"$IDENTITY")
ARN=$(jq -r .Arn <<<"$IDENTITY")
USERID=$(jq -r .UserId <<<"$IDENTITY")
EFFECTIVE_REGION=$("${AWS[@]}" configure get region 2>/dev/null || true)
[[ -n "$REGION" ]] && EFFECTIVE_REGION="$REGION"
[[ -z "$EFFECTIVE_REGION" ]] && EFFECTIVE_REGION="${AWS_REGION:-${AWS_DEFAULT_REGION:-unset}}"

ALIAS=$("${AWS[@]}" iam list-account-aliases --query 'AccountAliases[0]' --output text 2>/dev/null || echo "None")
[[ "$ALIAS" == "None" || -z "$ALIAS" ]] && ALIAS="${DIM}(none set)${RESET}"

echo
echo "${BOLD}AWS context${RESET}"
kv "account"   "${BOLD}${ACCOUNT}${RESET}  ${ALIAS}"
kv "region"    "${CYAN}${EFFECTIVE_REGION}${RESET}"
kv "identity"  "$ARN"
kv "user id"   "${DIM}${USERID}${RESET}"
kv "profile"   "${PROFILE:-${AWS_PROFILE:-default}}"

# Principal type shapes what you should expect to be able to do.
case "$ARN" in
    *:assumed-role/*) PRINCIPAL="assumed role  ${DIM}(session credentials)${RESET}" ;;
    *:user/*)         PRINCIPAL="IAM user      ${YELLOW}(long-lived keys)${RESET}" ;;
    *:root)           PRINCIPAL="${RED}ROOT ACCOUNT — stop and use a role instead${RESET}" ;;
    *)                PRINCIPAL="$ARN" ;;
esac
kv "principal" "$PRINCIPAL"

# Credential expiry matters mid-incident: a session that dies at the wrong moment
# costs more time than checking for it now.
if EXPIRY=$("${AWS[@]}" configure export-credentials --format process 2>/dev/null \
            | jq -r '.Expiration // empty' 2>/dev/null) && [[ -n "$EXPIRY" ]]; then
    if NOW_EPOCH=$(date -u +%s) && EXP_EPOCH=$(date -u -j -f "%Y-%m-%dT%H:%M:%S" "${EXPIRY%%+*}" +%s 2>/dev/null \
                   || date -u -d "$EXPIRY" +%s 2>/dev/null); then
        MINS=$(( (EXP_EPOCH - NOW_EPOCH) / 60 ))
        if   (( MINS < 0 ));  then kv "credentials" "${RED}expired${RESET}"
        elif (( MINS < 15 )); then kv "credentials" "${RED}expire in ${MINS}m${RESET}"
        elif (( MINS < 60 )); then kv "credentials" "${YELLOW}expire in ${MINS}m${RESET}"
        else                       kv "credentials" "${GREEN}valid for $((MINS / 60))h $((MINS % 60))m${RESET}"
        fi
    fi
fi

echo
echo "${BOLD}Access probes${RESET} ${DIM}(read-only calls, in this region)${RESET}"
probe() {
    local label="$1"; shift
    if output=$("$@" 2>&1); then
        printf "  %s✓%s %-22s %s\n" "$GREEN" "$RESET" "$label" "${DIM}ok${RESET}"
    else
        local reason="denied"
        grep -qi "not authorized\|AccessDenied" <<<"$output" || reason="failed"
        printf "  %s✗%s %-22s %s\n" "$RED" "$RESET" "$label" "${DIM}${reason}${RESET}"
    fi
}

probe "ec2:DescribeInstances" "${AWS[@]}" ec2 describe-instances --max-items 1 --output json
probe "ecs:ListClusters"      "${AWS[@]}" ecs list-clusters --output json
probe "eks:ListClusters"      "${AWS[@]}" eks list-clusters --output json
probe "logs:DescribeLogGroups" "${AWS[@]}" logs describe-log-groups --limit 1 --output json
probe "cloudwatch:ListMetrics" "${AWS[@]}" cloudwatch list-metrics --output json
probe "cloudtrail:LookupEvents" "${AWS[@]}" cloudtrail lookup-events --max-items 1 --output json
echo
