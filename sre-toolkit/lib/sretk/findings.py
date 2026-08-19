"""Findings, timelines, and incident reports.

Every diagnostic emits `Finding`s instead of printing directly. That is what lets
`incident.py` collect evidence from several sources, rank it, and turn it into a
probable cause plus a report.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterable

from . import out

CRIT, WARN, INFO, OK = out.CRIT, out.WARN, out.INFO, out.OK


@dataclass
class Finding:
    """One observation about a service."""

    severity: str                       # CRIT | WARN | INFO | OK
    source: str                         # ecs, alb, lambda, metrics, logs, changes…
    title: str
    detail: str = ""
    evidence: list[str] = field(default_factory=list)
    remediation: str = ""
    at: datetime | None = None          # when it happened, if pinpointable
    #: How strongly this points at a root cause (0–1). Ranking uses it as a
    #: tie-breaker within a severity, so a deploy 3 minutes before an error spike
    #: outranks a generic "errors are high".
    confidence: float = 0.4

    def to_dict(self) -> dict[str, Any]:
        return {
            "severity": self.severity,
            "source": self.source,
            "title": self.title,
            "detail": self.detail,
            "evidence": self.evidence,
            "remediation": self.remediation,
            "at": self.at.isoformat() if self.at else None,
            "confidence": round(self.confidence, 2),
        }


@dataclass
class Event:
    """A point in time worth putting on the incident timeline."""

    at: datetime
    source: str
    text: str
    severity: str = INFO


class Report:
    """Collected evidence for one service, over one window."""

    def __init__(self, service: str, region: str, account: str, window: str,
                 env: str | None = None):
        self.service = service
        self.region = region
        self.account = account
        self.window = window
        self.env = env
        self.findings: list[Finding] = []
        self.events: list[Event] = []
        self.checked: list[str] = []     # sources that ran, for "what was looked at"
        self.skipped: dict[str, str] = {}  # source -> why it produced nothing

    # -- collection -------------------------------------------------------- #

    def add(self, finding: Finding) -> Finding:
        self.findings.append(finding)
        if finding.at:
            self.events.append(Event(finding.at, finding.source, finding.title,
                                     finding.severity))
        return finding

    def event(self, at: datetime, source: str, text: str, severity: str = INFO) -> None:
        self.events.append(Event(at, source, text, severity))

    def note_checked(self, source: str) -> None:
        if source not in self.checked:
            self.checked.append(source)

    def skip(self, source: str, why: str) -> None:
        self.skipped[source] = why

    # -- analysis ---------------------------------------------------------- #

    @property
    def status(self) -> str:
        for level in (CRIT, WARN, OK):
            if any(f.severity == level for f in self.findings):
                return level
        return INFO

    def ranked(self) -> list[Finding]:
        return sorted(self.findings,
                      key=lambda f: (out.RANK.get(f.severity, 9), -f.confidence, f.source))

    def problems(self) -> list[Finding]:
        return [f for f in self.ranked() if f.severity in (CRIT, WARN)]

    def probable_cause(self) -> Finding | None:
        """Highest-confidence problem. Ties break toward the earliest signal,
        because the thing that happened first usually explains the rest."""
        problems = self.problems()
        if not problems:
            return None
        best = max(f.confidence for f in problems)
        leading = [f for f in problems if f.confidence >= best - 0.05]
        dated = [f for f in leading if f.at]
        if dated:
            return min(dated, key=lambda f: f.at)  # type: ignore[arg-type,return-value]
        return leading[0]

    def timeline(self) -> list[Event]:
        return sorted(self.events, key=lambda e: e.at)

    # -- rendering --------------------------------------------------------- #

    def render(self) -> None:
        out.heading(f"{self.service} · {self.env or 'all envs'} · {self.region}")
        out.kv("account", self.account)
        out.kv("window", self.window)
        out.kv("verdict", out.paint(self.status, self.status))
        out.kv("checked", ", ".join(self.checked) or "nothing")

        problems = self.problems()
        if not problems:
            out.section("No problems found")
            print(out.dim("  Every check that returned data looked healthy."))
        else:
            out.section("Findings", len(problems))
            for finding in problems:
                out.bullet(f"{out.bold(finding.title)}  {out.dim('[' + finding.source + ']')}",
                           finding.severity)
                if finding.detail:
                    print(f"      {finding.detail}")
                for line in finding.evidence:
                    print(out.dim(f"      · {line}"))

        events = self.timeline()
        if events:
            out.section("Timeline", len(events))
            # Show the date too once the timeline crosses midnight, otherwise an event
            # from yesterday reads as though it happened later today.
            spans_days = (events[0].at.astimezone(timezone.utc).date()
                          != events[-1].at.astimezone(timezone.utc).date())
            fmt = "%m-%d %H:%M:%S" if spans_days else "%H:%M:%S"
            for event in events:
                stamp = event.at.astimezone(timezone.utc).strftime(fmt)
                print(f"  {out.dim(stamp)}  {out.paint(event.severity, event.source.ljust(10))}"
                      f" {event.text}")

        cause = self.probable_cause()
        if cause:
            out.section("Probable cause")
            print(f"  {out.paint(cause.severity, cause.title)}")
            if cause.detail:
                print(f"  {out.dim(cause.detail)}")
            if cause.remediation:
                out.section("Recommended next step")
                print(f"  {cause.remediation}")

        others = [f for f in self.problems() if f is not cause and f.remediation]
        if others:
            out.section("Other remediations")
            for finding in others:
                out.bullet(finding.remediation, finding.severity)

        if self.skipped:
            out.section("Not checked")
            for source, why in self.skipped.items():
                print(out.dim(f"  {source}: {why}"))
        print()

    def to_dict(self) -> dict[str, Any]:
        cause = self.probable_cause()
        return {
            "service": self.service,
            "env": self.env,
            "region": self.region,
            "account": self.account,
            "window": self.window,
            "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "status": self.status,
            "checked": self.checked,
            "skipped": self.skipped,
            "probable_cause": cause.to_dict() if cause else None,
            "findings": [f.to_dict() for f in self.ranked()],
            "timeline": [
                {"at": e.at.isoformat(), "source": e.source,
                 "text": e.text, "severity": e.severity}
                for e in self.timeline()
            ],
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), indent=2, default=str)

    def to_markdown(self) -> str:
        cause = self.probable_cause()
        stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        lines = [
            f"# Incident summary — {self.service}",
            "",
            f"- **Status:** {self.status}",
            f"- **Service:** `{self.service}`" + (f" (`{self.env}`)" if self.env else ""),
            f"- **Account / region:** {self.account} / {self.region}",
            f"- **Window:** last {self.window}",
            f"- **Generated:** {stamp}",
            "",
        ]

        if cause:
            lines += ["## Probable cause", "", f"**{cause.title}**", ""]
            if cause.detail:
                lines += [cause.detail, ""]
            if cause.remediation:
                lines += ["**Recommended next step:** " + cause.remediation, ""]
        else:
            lines += ["## Probable cause", "",
                      "No problem signals in this window.", ""]

        problems = self.problems()
        if problems:
            lines += ["## Findings", "",
                      "| Severity | Source | Finding | Detail |",
                      "| --- | --- | --- | --- |"]
            for f in problems:
                # A pipe anywhere in a cell splits the row and wrecks the table.
                cells = [f.severity, f.source, f.title, f.detail or ""]
                lines.append("| " + " | ".join(c.replace("|", "\\|") for c in cells) + " |")
            lines.append("")

        events = self.timeline()
        if events:
            lines += ["## Timeline", "", "| Time (UTC) | Source | Event |",
                      "| --- | --- | --- |"]
            for e in events:
                lines.append(f"| {e.at.astimezone(timezone.utc):%Y-%m-%d %H:%M:%S} "
                             f"| {e.source} | {e.text.replace('|', chr(92) + '|')} |")
            lines.append("")

        evidence = [f for f in problems if f.evidence]
        if evidence:
            lines += ["## Evidence", ""]
            for f in evidence:
                lines.append(f"**{f.title}** ({f.source})")
                lines += [f"- {line}" for line in f.evidence]
                lines.append("")

        lines += ["## What was checked", "",
                  ", ".join(self.checked) or "nothing", ""]
        if self.skipped:
            lines += ["### Not checked", ""]
            lines += [f"- **{src}**: {why}" for src, why in self.skipped.items()]
            lines.append("")
        return "\n".join(lines)


def worst(severities: Iterable[str]) -> str:
    return min(severities, key=lambda s: out.RANK.get(s, 9), default=INFO)
