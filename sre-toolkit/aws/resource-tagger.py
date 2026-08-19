#!/usr/bin/env python3
"""
aws_resource_tagger.py — bulk add / remove / inspect tags on AWS resources in one region.

Built on the Resource Groups Tagging API, so a single code path covers every taggable
service in the region (EC2, S3, RDS, Lambda, DynamoDB, ELB, ECS, logs, …).

    # see what is out there and how it is tagged
    python3 aws_resource_tagger.py list --region us-east-1 --all
    python3 aws_resource_tagger.py list --region us-east-1 --type ec2:instance --json

    # add tags
    python3 aws_resource_tagger.py add --region us-east-1 --all \
        --tag Env=dev --tag Owner=jenom
    python3 aws_resource_tagger.py add --region us-east-1 --file resources.txt --tag Env=prod

    # remove tags
    python3 aws_resource_tagger.py remove --region us-east-1 --has-key Temp --key Temp

    # pick resources interactively
    python3 aws_resource_tagger.py add --region us-east-1 --all --select --tag Env=dev

Resources are chosen by ARN, bare id, Name tag, glob, existing tags, or resource type.
Every write shows a plan first and asks for confirmation (skip with --yes, preview with
--dry-run). Resources already in the desired state are reported and skipped.
"""

from __future__ import annotations

import argparse
import curses
import fnmatch
import json
import os
import re
import sys
from dataclasses import dataclass, field
from typing import Any, Iterator

try:
    import boto3
    from botocore.config import Config
    from botocore.exceptions import BotoCoreError, ClientError, NoCredentialsError
except ImportError:  # pragma: no cover
    sys.exit("boto3 is required:  pip install boto3")


BATCH = 20  # tag_resources / untag_resources accept at most 20 ARNs per call

# Splits an ARN tail into (type, separator, id) — the separator is whichever of
# `/` or `:` comes first, and everything after it is the id.
_TAIL_SPLIT = re.compile(r"^([^/:]*)([/:])?(.*)$", re.DOTALL)

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
# Model
# --------------------------------------------------------------------------- #


@dataclass
class Res:
    """One taggable resource, as returned by the tagging API."""

    arn: str
    tags: dict[str, str] = field(default_factory=dict)
    discovered: bool = True  # False => ARN supplied by the user, current tags unknown

    def _split(self) -> tuple[str, str | None, str]:
        """(service, resource type or None, id) — ARN tails come as `type/id`,
        `type:id`, or a bare `id`, and ids may themselves contain / and :
        (log group paths, secret names)."""
        parts = self.arn.split(":", 5)
        if len(parts) < 6:
            return (parts[2] if len(parts) > 2 else "unknown"), None, parts[-1]
        service, tail = parts[2], parts[5]
        head, sep, rest = _TAIL_SPLIT.match(tail).groups()  # type: ignore[union-attr]
        return service, (head if sep else None), (rest if sep else head)

    @property
    def rid(self) -> str:
        """Bare resource id, with the ARN's type prefix stripped."""
        return self._split()[2] or self.arn

    @property
    def group(self) -> str:
        """`service:type` label used for grouping, e.g. ec2:instance, s3."""
        service, kind, _ = self._split()
        return f"{service}:{kind}" if kind else service

    @property
    def name(self) -> str:
        return self.tags.get("Name", "")

    def matches(self, token: str) -> bool:
        """Match a user-supplied token (ARN, id, Name tag) exactly or as a glob."""
        t = token.lower()
        fields = (self.arn.lower(), self.rid.lower(), self.name.lower())
        return any(f == t or fnmatch.fnmatch(f, t) for f in fields if f)


@dataclass
class Change:
    """What one resource needs, for the mode being run."""

    res: Res
    adds: dict[str, str] = field(default_factory=dict)      # key -> new value
    overwrites: dict[str, tuple[str, str]] = field(default_factory=dict)  # key -> (old, new)
    removes: dict[str, str] = field(default_factory=dict)   # key -> value being dropped

    @property
    def empty(self) -> bool:
        return not (self.adds or self.overwrites or self.removes)


# --------------------------------------------------------------------------- #
# AWS helpers
# --------------------------------------------------------------------------- #


def tagging_client(session: "boto3.Session", region: str):
    return session.client(
        "resourcegroupstaggingapi",
        config=Config(region_name=region, retries={"max_attempts": 6, "mode": "standard"}),
    )


def get_resources(client, type_filters: list[str], tag_filters: list[dict]) -> Iterator[Res]:
    kwargs: dict[str, Any] = {"ResourcesPerPage": 100}
    if type_filters:
        kwargs["ResourceTypeFilters"] = type_filters
    if tag_filters:
        kwargs["TagFilters"] = tag_filters
    for page in client.get_paginator("get_resources").paginate(**kwargs):
        for item in page.get("ResourceTagMappingList", []):
            yield Res(
                item["ResourceARN"],
                {t["Key"]: t["Value"] for t in item.get("Tags", [])},
            )


def batched(items: list[Any], size: int = BATCH) -> Iterator[list[Any]]:
    for i in range(0, len(items), size):
        yield items[i : i + size]


# --------------------------------------------------------------------------- #
# Parsing / validation
# --------------------------------------------------------------------------- #


def parse_tag(raw: str) -> tuple[str, str]:
    if "=" not in raw:
        raise argparse.ArgumentTypeError(f"tag must be Key=Value, got {raw!r}")
    key, _, value = raw.partition("=")
    key, value = key.strip(), value.strip()
    if not key:
        raise argparse.ArgumentTypeError(f"tag key is empty in {raw!r}")
    if key.lower().startswith("aws:"):
        raise argparse.ArgumentTypeError(f"{key!r} uses the reserved 'aws:' prefix")
    if len(key) > 128:
        raise argparse.ArgumentTypeError(f"tag key {key!r} exceeds 128 characters")
    if len(value) > 256:
        raise argparse.ArgumentTypeError(f"value for {key!r} exceeds 256 characters")
    return key, value


def parse_tag_filter(raw: str) -> dict:
    """`Key` or `Key=Value` or `Key=V1,V2` -> a tagging-API TagFilter."""
    key, _, values = raw.partition("=")
    key = key.strip()
    if not key:
        raise argparse.ArgumentTypeError(f"filter key is empty in {raw!r}")
    vals = [v.strip() for v in values.split(",") if v.strip()] if values else []
    return {"Key": key, "Values": vals} if vals else {"Key": key}


def read_resource_file(path: str) -> list[str]:
    tokens: list[str] = []
    with open(path, "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.split("#", 1)[0].strip()
            if line:
                tokens.extend(t for t in line.replace(",", " ").split() if t)
    return tokens


# --------------------------------------------------------------------------- #
# Selection
# --------------------------------------------------------------------------- #


def select_resources(client, args: argparse.Namespace) -> tuple[list[Res], list[str]]:
    """Return (matched resources, tokens that matched nothing)."""
    pool = sorted(
        get_resources(client, args.type or [], args.has_tag or []),
        key=lambda r: (r.group, r.name or r.rid),
    )

    tokens: list[str] = list(args.resource or [])
    if args.file:
        tokens += read_resource_file(args.file)

    if tokens:
        chosen: list[Res] = []
        unmatched: list[str] = []
        seen: set[str] = set()
        by_arn = {r.arn: r for r in pool}
        for token in tokens:
            hits = [r for r in pool if r.matches(token)]
            if not hits and token.startswith("arn:"):
                # Explicit ARN the tagging API did not report — act on it anyway.
                hits = [by_arn.get(token) or Res(token, discovered=False)]
            if not hits:
                unmatched.append(token)
                continue
            for r in hits:
                if r.arn not in seen:
                    seen.add(r.arn)
                    chosen.append(r)
    else:
        chosen, unmatched = pool, []

    for pattern in args.exclude or []:
        chosen = [r for r in chosen if not r.matches(pattern)]
    return chosen, unmatched


# --------------------------------------------------------------------------- #
# Interactive picker
# --------------------------------------------------------------------------- #

HELP = "space toggle · a group · A all · n none · / filter · enter confirm · q quit"


class Picker:
    """Curses checkbox list over resources, grouped by service:type."""

    def __init__(self, resources: list[Res], region: str, account: str, action: str):
        self.resources = resources
        self.region = region
        self.account = account
        self.action = action
        self.checked: set[int] = set(range(len(resources)))  # pre-selected by the query
        self.filter = ""
        self.cursor = 0
        self.top = 0
        self.rows: list[tuple[str, Any]] = []
        self._build_rows()

    def _build_rows(self) -> None:
        keep = None
        if self.rows and 0 <= self.cursor < len(self.rows) and self.rows[self.cursor][0] == "item":
            keep = self.rows[self.cursor][1]
        self.rows, current, f = [], None, self.filter.lower()
        for idx, r in enumerate(self.resources):
            if f and f not in f"{r.arn} {r.name} {' '.join(r.tags)}".lower():
                continue
            if r.group != current:
                current = r.group
                self.rows.append(("header", current))
            self.rows.append(("item", idx))
        if keep is not None:
            self.cursor = next((i for i, (k, v) in enumerate(self.rows)
                                if k == "item" and v == keep), 0)
        self.cursor = max(0, min(self.cursor, max(0, len(self.rows) - 1)))

    def _group_of(self, row: int) -> str | None:
        for i in range(row, -1, -1):
            if self.rows[i][0] == "header":
                return self.rows[i][1]
        return None

    def _toggle_group(self, group: str) -> None:
        idxs = {v for k, v in self.rows if k == "item" and self.resources[v].group == group}
        self.checked = (self.checked - idxs) if idxs <= self.checked else (self.checked | idxs)

    def _draw(self, scr) -> None:
        scr.erase()
        h, w = scr.getmaxyx()
        body = max(1, h - 4)
        if self.cursor < self.top:
            self.top = self.cursor
        elif self.cursor >= self.top + body:
            self.top = self.cursor - body + 1

        title = f" Tag {self.action} · account {self.account} · region {self.region} "
        scr.addnstr(0, 0, title.ljust(w), w, curses.A_REVERSE | curses.A_BOLD)

        for i in range(body):
            row = self.top + i
            if row >= len(self.rows):
                break
            kind, val = self.rows[row]
            attr = curses.A_REVERSE if row == self.cursor else curses.A_NORMAL
            if kind == "header":
                n = sum(1 for k, v in self.rows
                        if k == "item" and self.resources[v].group == val)
                scr.addnstr(i + 1, 0, f"  {val}  ({n})".ljust(w), w,
                            attr | curses.A_BOLD | curses.color_pair(3))
                continue
            r = self.resources[val]
            mark = "[x]" if val in self.checked else "[ ]"
            label = f"   {mark} {r.rid}" + (f" ({r.name})" if r.name else "")
            tags = "  " + (", ".join(f"{k}={v}" for k, v in sorted(r.tags.items()))
                           or "no tags")
            scr.addnstr(i + 1, 0, (label + tags).ljust(w), w, attr)
            if row != self.cursor and len(label) < w:
                scr.addnstr(i + 1, len(label), tags[: w - len(label)], w - len(label),
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
        try:
            return scr.getstr(h - 1, len(label), 60).decode("utf-8", "replace")
        finally:
            curses.noecho()
            curses.curs_set(0)

    def _loop(self, scr) -> list[Res] | None:
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
            ch = scr.getch()
            if ch in (curses.KEY_DOWN, ord("j")):
                self.cursor = min(len(self.rows) - 1, self.cursor + 1)
            elif ch in (curses.KEY_UP, ord("k")):
                self.cursor = max(0, self.cursor - 1)
            elif ch in (curses.KEY_NPAGE, ord("f")):
                self.cursor = min(len(self.rows) - 1, self.cursor + 10)
            elif ch in (curses.KEY_PPAGE, ord("b")):
                self.cursor = max(0, self.cursor - 10)
            elif ch in (curses.KEY_HOME, ord("g")):
                self.cursor = 0
            elif ch in (curses.KEY_END, ord("G")):
                self.cursor = len(self.rows) - 1
            elif ch == ord(" ") and self.rows:
                kind, val = self.rows[self.cursor]
                if kind == "item":
                    self.checked ^= {val}
                    self.cursor = min(len(self.rows) - 1, self.cursor + 1)
                else:
                    self._toggle_group(val)
            elif ch == ord("a") and self.rows:
                group = self._group_of(self.cursor)
                if group:
                    self._toggle_group(group)
            elif ch == ord("A"):
                self.checked = {v for k, v in self.rows if k == "item"}
            elif ch == ord("n"):
                self.checked -= {v for k, v in self.rows if k == "item"}
            elif ch == ord("/"):
                self.filter = self._prompt(scr, "filter: ").strip()
                self._build_rows()
                self.top = 0
            elif ch == 27:
                if self.filter:
                    self.filter = ""
                    self._build_rows()
                else:
                    return None
            elif ch in (ord("q"), 3):
                return None
            elif ch in (curses.KEY_ENTER, 10, 13):
                return [self.resources[i] for i in sorted(self.checked)]

    def run(self) -> list[Res] | None:
        return curses.wrapper(self._loop)


def pick_plain(resources: list[Res]) -> list[Res] | None:
    current = None
    for i, r in enumerate(resources, 1):
        if r.group != current:
            current = r.group
            print(f"\n{bold(current)}")
        tags = ", ".join(f"{k}={v}" for k, v in sorted(r.tags.items())) or "no tags"
        print(f"  {i:>4}. {r.rid}" + (f" ({r.name})" if r.name else "") + dim(f"  {tags}"))
    raw = input("\nNumbers to act on (e.g. 1,4,7-9 · 'all' · blank to cancel): ").strip()
    if not raw:
        return None
    if raw.lower() == "all":
        return list(resources)
    chosen: set[int] = set()
    for part in raw.replace(",", " ").split():
        if "-" in part:
            a, _, b = part.partition("-")
            if a.isdigit() and b.isdigit():
                chosen.update(range(int(a), int(b) + 1))
        elif part.isdigit():
            chosen.add(int(part))
    return [resources[i - 1] for i in sorted(chosen) if 1 <= i <= len(resources)]


# --------------------------------------------------------------------------- #
# Plan building / rendering
# --------------------------------------------------------------------------- #


def build_changes(resources: list[Res], args: argparse.Namespace) -> list[Change]:
    changes = []
    for r in resources:
        ch = Change(r)
        if args.command == "add":
            for key, value in args.tags:
                if r.discovered and r.tags.get(key) == value:
                    continue  # already correct
                if key in r.tags and r.discovered:
                    if args.no_overwrite:
                        continue
                    ch.overwrites[key] = (r.tags[key], value)
                else:
                    ch.adds[key] = value
        else:  # remove
            for key in args.keys:
                if not r.discovered or key in r.tags:
                    ch.removes[key] = r.tags.get(key, "?")
        changes.append(ch)
    return changes


def show_plan(changes: list[Change], args: argparse.Namespace, account: str) -> list[Change]:
    todo = [c for c in changes if not c.empty]
    skipped = len(changes) - len(todo)

    print()
    print(bold("=" * 76))
    verb = "ADD TAGS" if args.command == "add" else "REMOVE TAGS"
    print(bold(f"  {verb} PLAN — account {account} — region {args.region}"))
    print(bold("=" * 76))

    current = None
    for ch in todo:
        if ch.res.group != current:
            current = ch.res.group
            print(f"\n{bold(current)}")
        label = f"  {ch.res.rid}" + (f" ({ch.res.name})" if ch.res.name else "")
        print(label + ("" if ch.res.discovered else yellow("  [not reported by the tagging API]")))
        for key, value in sorted(ch.adds.items()):
            print(f"      {green('+')} {key}={value}")
        for key, (old, new) in sorted(ch.overwrites.items()):
            print(f"      {yellow('~')} {key}: {old} -> {new}")
        for key, value in sorted(ch.removes.items()):
            print(f"      {red('-')} {key}" + (f"={value}" if value != "?" else ""))

    print()
    print(bold("-" * 76))
    n_add = sum(len(c.adds) for c in todo)
    n_over = sum(len(c.overwrites) for c in todo)
    n_del = sum(len(c.removes) for c in todo)
    bits = []
    if n_add:
        bits.append(f"{n_add} tag(s) added")
    if n_over:
        bits.append(f"{n_over} value(s) overwritten")
    if n_del:
        bits.append(f"{n_del} tag(s) removed")
    print(f"  {bold(str(len(todo)))} resources to change: {', '.join(bits) or 'nothing'}")
    if skipped:
        print(dim(f"  {skipped} resource(s) already in the desired state — skipped"))
    print(bold("-" * 76))
    return todo


def confirm(args: argparse.Namespace, count: int) -> bool:
    if args.dry_run:
        print(yellow("\n[--dry-run] nothing was written.\n"))
        return False
    if args.yes:
        return True
    word = "tag" if args.command == "add" else "untag"
    try:
        answer = input(f'\nType "{bold("yes")}" to {word} {count} resources in '
                       f'{cyan(args.region)} (anything else aborts): ')
    except (EOFError, KeyboardInterrupt):
        return False
    return answer.strip().lower() == "yes"


# --------------------------------------------------------------------------- #
# Apply
# --------------------------------------------------------------------------- #


def apply_changes(client, changes: list[Change], args: argparse.Namespace) -> tuple[int, list[tuple[str, str]]]:
    """Group resources by identical tag payload, then write in batches of 20."""
    groups: dict[tuple, list[str]] = {}
    short_of = {ch.res.arn: ch.res.rid for ch in changes}
    for ch in changes:
        if args.command == "add":
            payload = {**ch.adds, **{k: v for k, (_, v) in ch.overwrites.items()}}
            key = tuple(sorted(payload.items()))
        else:
            key = tuple(sorted(ch.removes))
        groups.setdefault(key, []).append(ch.res.arn)

    done, failures = 0, []
    for key, arns in groups.items():
        if args.command == "add":
            payload = dict(key)
            label = ", ".join(f"{k}={v}" for k, v in payload.items())
            print(f"\n{bold('+ ' + label)}  {dim(f'({len(arns)} resources)')}")
        else:
            label = ", ".join(key)
            print(f"\n{bold('- ' + label)}  {dim(f'({len(arns)} resources)')}")

        for chunk in batched(arns):
            try:
                if args.command == "add":
                    resp = client.tag_resources(ResourceARNList=chunk, Tags=dict(key))
                else:
                    resp = client.untag_resources(ResourceARNList=chunk, TagKeys=list(key))
            except (ClientError, BotoCoreError) as exc:
                msg = str(exc)
                if isinstance(exc, ClientError):
                    msg = exc.response.get("Error", {}).get("Message", msg)
                for arn in chunk:
                    print(f"   {short_of.get(arn, arn)} … {red('FAILED')} {dim(msg[:110])}")
                    failures.append((arn, msg[:150]))
                continue

            failed = resp.get("FailedResourcesMap", {}) or {}
            for arn in chunk:
                short = short_of.get(arn, arn)
                if arn in failed:
                    err = failed[arn].get("ErrorMessage", failed[arn].get("ErrorCode", "failed"))
                    print(f"   {short} … {red('FAILED')} {dim(err[:110])}")
                    failures.append((arn, err[:150]))
                else:
                    print(f"   {short} … {green('ok')}")
                    done += 1
    return done, failures


# --------------------------------------------------------------------------- #
# list mode
# --------------------------------------------------------------------------- #


def show_list(resources: list[Res], as_json: bool) -> None:
    if as_json:
        print(json.dumps(
            [{"arn": r.arn, "id": r.rid, "type": r.group, "tags": r.tags} for r in resources],
            indent=2, sort_keys=True,
        ))
        return

    current, untagged = None, 0
    for r in resources:
        if r.group != current:
            current = r.group
            n = sum(1 for x in resources if x.group == current)
            print(f"\n{bold(current)} {dim(f'({n})')}")
        print(f"  {r.rid}" + (f" ({r.name})" if r.name else ""))
        if r.tags:
            for key, value in sorted(r.tags.items()):
                print(dim(f"      {key} = {value}"))
        elif not r.discovered:
            print(yellow("      (not reported by the tagging API — tags unknown)"))
        else:
            untagged += 1
            print(dim("      (no tags)"))

    keys: dict[str, int] = {}
    for r in resources:
        for key in r.tags:
            keys[key] = keys.get(key, 0) + 1
    print(f"\n{bold(str(len(resources)))} resources · {untagged} untagged")
    if keys:
        top = sorted(keys.items(), key=lambda kv: (-kv[1], kv[0]))
        print("Tag keys in use: " + ", ".join(f"{k} ({n})" for k, n in top))


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def add_selection_flags(p: argparse.ArgumentParser) -> None:
    g = p.add_argument_group("resource selection")
    g.add_argument("--resource", "-R", action="append", metavar="ARN|ID|NAME|GLOB",
                   help="resource to act on; repeatable, globs allowed")
    g.add_argument("--file", "-f", metavar="PATH",
                   help="file of resources, one per line (# comments allowed)")
    g.add_argument("--type", "-t", action="append", metavar="TYPE",
                   help="resource type filter, e.g. ec2:instance, s3, lambda; repeatable")
    g.add_argument("--has-tag", action="append", type=parse_tag_filter, metavar="KEY[=V1,V2]",
                   help="only resources carrying this tag; repeatable")
    g.add_argument("--exclude", "-x", action="append", metavar="GLOB",
                   help="drop resources matching this ARN/id/Name glob; repeatable")
    g.add_argument("--all", action="store_true",
                   help="act on every taggable resource matching the filters")
    g.add_argument("--select", "-s", action="store_true",
                   help="review and tick the matched resources before continuing")
    g.add_argument("--no-ui", action="store_true", help="plain numeric selector instead of curses")


def parse_args(argv: list[str]) -> argparse.Namespace:
    # Accepted on either side of the subcommand: SUPPRESS keeps an absent sub-command
    # flag from clobbering the value already parsed at the top level.
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--region", "-r", default=argparse.SUPPRESS,
                        help="AWS region (falls back to AWS_REGION, else prompts)")
    common.add_argument("--profile", "-p", default=argparse.SUPPRESS,
                        help="AWS profile from ~/.aws/credentials")

    p = argparse.ArgumentParser(
        prog="aws_resource_tagger",
        description="Add, remove, or inspect tags on AWS resources in one region.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="Writes always show a plan first; confirm with 'yes' or pass --yes.",
        parents=[common],
    )
    sub = p.add_subparsers(dest="command", required=True)

    lst = sub.add_parser("list", help="show resources and their current tags", parents=[common])
    lst.add_argument("--json", action="store_true", help="machine-readable output")
    add_selection_flags(lst)

    add = sub.add_parser("add", help="add or update tags", parents=[common])
    add.add_argument("--tag", "-T", action="append", required=True, type=parse_tag,
                     dest="tags", metavar="KEY=VALUE", help="tag to set; repeatable")
    add.add_argument("--no-overwrite", action="store_true",
                     help="leave keys that already exist untouched")
    add.add_argument("--dry-run", action="store_true", help="show the plan, write nothing")
    add.add_argument("--yes", "-y", action="store_true", help="skip the confirmation prompt")
    add_selection_flags(add)

    rm = sub.add_parser("remove", help="remove tags by key", parents=[common])
    rm.add_argument("--key", "-K", action="append", required=True, dest="keys",
                    metavar="KEY", help="tag key to remove; repeatable")
    rm.add_argument("--dry-run", action="store_true", help="show the plan, write nothing")
    rm.add_argument("--yes", "-y", action="store_true", help="skip the confirmation prompt")
    add_selection_flags(rm)

    args = p.parse_args(argv)
    for name, default in (("tags", []), ("keys", []), ("json", False), ("region", None),
                          ("profile", None), ("dry_run", False), ("yes", False),
                          ("no_overwrite", False)):
        if not hasattr(args, name):
            setattr(args, name, default)
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv if argv is not None else sys.argv[1:])

    if not any([args.resource, args.file, args.type, args.has_tag, args.all]):
        print(red("Pick some resources: --resource/--file/--type/--has-tag, or --all."))
        return 2

    region = args.region or os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION")
    if not region:
        try:
            region = input("Region: ").strip()
        except (EOFError, KeyboardInterrupt):
            return 1
    if not region:
        print(red("A region is required."))
        return 2
    args.region = region

    try:
        session = boto3.Session(profile_name=args.profile, region_name=region)
        ident = session.client("sts", region_name=region).get_caller_identity()
    except (NoCredentialsError, ClientError, BotoCoreError) as exc:
        print(red(f"Could not authenticate to AWS: {exc}"))
        return 2
    account = ident["Account"]

    # In --json mode chatter goes to stderr so stdout stays pipeable into jq.
    stream = sys.stderr if args.json else sys.stdout

    def info(text: str = "") -> None:
        print(text, file=stream)

    info(f"\n{bold('Account')} {account}  {dim(ident['Arn'])}")
    info(f"{bold('Region')}  {cyan(region)}\n")

    client = tagging_client(session, region)
    try:
        resources, unmatched = select_resources(client, args)
    except (ClientError, BotoCoreError) as exc:
        print(red(f"Could not read resources: {exc}"))
        return 2
    except OSError as exc:
        print(red(f"Could not read --file: {exc}"))
        return 2

    if unmatched:
        info(yellow(f"{len(unmatched)} input(s) matched nothing in {region}:"))
        for token in unmatched[:10]:
            info(dim(f"   {token}"))
        if len(unmatched) > 10:
            info(dim(f"   … and {len(unmatched) - 10} more"))
        info()

    if not resources:
        info(yellow("No matching resources."))
        if args.json:
            print("[]")
        return 0

    if args.command == "list":
        show_list(resources, args.json)
        return 0

    print(f"Matched {bold(str(len(resources)))} resources.")

    if args.select:
        use_ui = not args.no_ui and sys.stdin.isatty() and sys.stdout.isatty()
        if use_ui:
            input(dim("Press Enter to open the selector… "))
            try:
                resources = Picker(resources, region, account, args.command).run() or []
            except curses.error as exc:
                print(yellow(f"Curses UI unavailable ({exc}); falling back to text mode."))
                resources = pick_plain(resources) or []
        else:
            resources = pick_plain(resources) or []
        if not resources:
            print(yellow("\nNothing selected — no changes made."))
            return 0

    todo = show_plan(build_changes(resources, args), args, account)
    if not todo:
        print(green("\nEverything is already in the desired state — nothing to do."))
        return 0

    if not confirm(args, len(todo)):
        if not args.dry_run:
            print(yellow("\nAborted — no changes made."))
        return 0 if args.dry_run else 1

    done, failures = apply_changes(client, todo, args)
    print()
    print(bold(f"Done: {done} resources updated, {len(failures)} failed."))
    if failures:
        print(yellow("\nFailures:"))
        for arn, msg in failures:
            print(f"   {arn}: {dim(msg)}")
        return 1
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print(yellow("\nInterrupted — no further changes made."))
        sys.exit(130)
