#!/usr/bin/env bash
#
# aws-find.sh — find anything in a region by name, id, ARN, or tag value.
#
# "Someone said the payments thing is broken" — this tells you what that actually is
# and where it lives, across every taggable service at once.
#
#   ./aws-find.sh payments
#   ./aws-find.sh 'prod-*' --region eu-west-1
#   ./aws-find.sh --tag Environment=prod
#   ./aws-find.sh banking --type rds --type lambda
#
# Read-only. Uses the Resource Groups Tagging API, so it covers every taggable service
# in one sweep instead of asking each service in turn.

set -euo pipefail

TERM_MATCH=""
REGION=""
PROFILE=""
TAG_FILTERS=()
TYPE_FILTERS=()
SHOW_TAGS=0

usage() {
    sed -n '2,16p' "$0" | sed 's/^# \{0,1\}//'
    exit "${1:-0}"
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        -r|--region)  REGION="${2:-}"; shift 2 ;;
        -p|--profile) PROFILE="${2:-}"; shift 2 ;;
        -t|--type)    TYPE_FILTERS+=("${2:-}"); shift 2 ;;
        --tag)        TAG_FILTERS+=("${2:-}"); shift 2 ;;
        --tags)       SHOW_TAGS=1; shift ;;
        -h|--help)    usage 0 ;;
        -*) echo "unknown flag: $1" >&2; usage 2 ;;
        *)  TERM_MATCH="$1"; shift ;;
    esac
done

if [[ -z "$TERM_MATCH" && ${#TAG_FILTERS[@]} -eq 0 && ${#TYPE_FILTERS[@]} -eq 0 ]]; then
    echo "give a search term, --tag, or --type" >&2
    usage 2
fi

command -v aws >/dev/null || { echo "aws CLI not found" >&2; exit 2; }
command -v jq  >/dev/null || { echo "jq not found" >&2; exit 2; }

AWS=(aws)
[[ -n "$PROFILE" ]] && AWS+=(--profile "$PROFILE")
[[ -n "$REGION"  ]] && AWS+=(--region  "$REGION")

if [[ -t 1 && -z "${NO_COLOR:-}" ]]; then
    BOLD=$'\033[1m'; DIM=$'\033[2m'; CYAN=$'\033[36m'; RESET=$'\033[0m'
else
    BOLD=""; DIM=""; CYAN=""; RESET=""
fi

GET_ARGS=(resourcegroupstaggingapi get-resources --resources-per-page 100 --output json)
for tag in "${TAG_FILTERS[@]}"; do
    key="${tag%%=*}"; values="${tag#*=}"
    if [[ "$tag" == *=* && -n "$values" ]]; then
        GET_ARGS+=(--tag-filters "Key=${key},Values=${values//,/ }")
    else
        GET_ARGS+=(--tag-filters "Key=${key}")
    fi
done
for type in "${TYPE_FILTERS[@]}"; do
    GET_ARGS+=(--resource-type-filters "$type")
done

RESOURCES=$("${AWS[@]}" "${GET_ARGS[@]}" | jq -c '.ResourceTagMappingList[]?')

# Match on the ARN, the bare id, or any tag key/value. Globs are honoured; a plain
# word is treated as a substring so `aws-find.sh payments` does the obvious thing.
PATTERN="$TERM_MATCH"
[[ -n "$PATTERN" && "$PATTERN" != *[\*\?]* ]] && PATTERN="*${PATTERN}*"

MATCHED=$(jq -c -n --arg pat "${PATTERN,,}" '
    [inputs
     | . as $r
     | ($r.ResourceARN) as $arn
     | ($arn | ascii_downcase) as $lower
     | ($r.Tags // []) as $tags
     | select(
         $pat == "" or
         ($lower | test($pat | gsub("\\*"; ".*") | gsub("\\?"; ".") | "^" + . + "$")) or
         ($tags | map((.Key + "=" + .Value) | ascii_downcase)
                | any(test($pat | gsub("\\*"; ".*") | gsub("\\?"; ".") | "^" + . + "$")))
       )
     | {arn: $arn, tags: $tags}]
' <<<"$RESOURCES")

COUNT=$(jq 'length' <<<"$MATCHED")
if [[ "$COUNT" -eq 0 ]]; then
    echo "${DIM}No resources matched.${RESET}" >&2
    exit 1
fi

echo
printf "%s%s matching resource(s)%s\n" "$BOLD" "$COUNT" "$RESET"

jq -r --arg showtags "$SHOW_TAGS" '
    # service:type from the ARN, splitting on whichever of / or : comes first
    def kind:
        (. / ":") as $p
        | if ($p | length) < 6 then $p[2]
          else ($p[2]) as $svc
             | ($p[5:] | join(":")) as $tail
             | ($tail | capture("^(?<head>[^/:]*)(?<sep>[/:])?") ) as $m
             | if ($m.sep // "") == "" then $svc else $svc + ":" + $m.head end
          end;
    def id:
        (. / ":") as $p
        | if ($p | length) < 6 then .
          else ($p[5:] | join(":")) as $tail
             | ($tail | capture("^(?<head>[^/:]*)(?<sep>[/:])?(?<rest>.*)$")) as $m
             | if ($m.sep // "") == "" then $m.head else $m.rest end
          end;
    group_by(.arn | kind)[]
    | (.[0].arn | kind) as $k
    | "\n[1m" + $k + "[0m [2m(" + (length | tostring) + ")[0m",
      (.[] |
        "  " + (.arn | id)
        + (( .tags // [] | map(select(.Key == "Name")) | .[0].Value // "") as $n
           | if $n == "" or $n == (.arn | id) then "" else "  (" + $n + ")" end)
        + (if $showtags == "1"
           then "\n      [2m" + ((.tags // []) | map(.Key + "=" + .Value) | join(", ")) + "[0m"
           else "" end))
' <<<"$MATCHED"
echo
