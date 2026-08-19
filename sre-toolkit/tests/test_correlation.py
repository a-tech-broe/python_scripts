"""Correlation and ranking — the rules that decide what gets called the root cause."""

from __future__ import annotations

import json
import sys
import unittest
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "lib"))

import stubs  # noqa: E402
from sretk import CRIT, Finding, INFO, OK, Report, WARN, changes  # noqa: E402

SPIKE = stubs.ago(minutes=40)
EVENTS = stubs.change_events(SPIKE)


class TestCorrelate(unittest.TestCase):
    def setUp(self):
        self.suspects = changes.correlate(EVENTS, SPIKE)

    def test_nearest_disruptive_change_ranks_first(self):
        self.assertEqual(self.suspects[0]["name"], "UpdateFunctionCode")

    def test_changes_after_the_incident_are_never_suspects(self):
        # A fix applied during the incident must not be blamed for causing it.
        self.assertFalse([s for s in self.suspects if s["at"] > SPIKE])
        self.assertNotIn("UpdateService", [s["name"] for s in self.suspects])

    def test_changes_outside_the_tolerance_are_dropped(self):
        self.assertNotIn("PutParameter", [s["name"] for s in self.suspects])

    def test_failed_calls_are_not_treated_as_changes(self):
        # A throttled InvokeModel is a symptom, not a cause.
        self.assertNotIn("InvokeModel", [s["name"] for s in self.suspects])

    def test_closer_changes_outrank_more_distant_ones(self):
        scores = [s["score"] for s in self.suspects]
        self.assertEqual(scores, sorted(scores, reverse=True))

    def test_no_incident_time_means_no_suspects(self):
        self.assertEqual(changes.correlate(EVENTS, None), [])

    def test_describe_mentions_actor_and_resource(self):
        text = changes.describe(self.suspects[0])
        self.assertIn("UpdateFunctionCode", text)
        self.assertIn("deploy-bot", text)
        self.assertIn("payments-api", text)

    def test_describe_marks_failures(self):
        failed = [e for e in EVENTS if e["error"]][0]
        self.assertIn("failed: ThrottlingException", changes.describe(failed))


class TestRanking(unittest.TestCase):
    """A cause must outrank the symptoms it produced."""

    def setUp(self):
        self.report = Report("payments", "us-east-1", "1", "1h", "prod")
        self.report.add(Finding(CRIT, "lambda", "12% of invocations failing",
                                at=SPIKE, confidence=0.6))
        self.report.add(Finding(CRIT, "logs", "480 error lines", at=SPIKE, confidence=0.55))
        top = changes.correlate(EVENTS, SPIKE)[0]
        self.report.add(Finding(WARN, "changes",
                                f"{top['name']} ran 4 min before the incident",
                                at=top["at"], remediation="Roll back the deploy.",
                                confidence=min(0.95, 0.6 + top["score"] * 0.4)))

    def test_the_change_becomes_the_probable_cause(self):
        cause = self.report.probable_cause()
        self.assertEqual(cause.source, "changes")
        self.assertIn("UpdateFunctionCode", cause.title)

    def test_cause_carries_the_remediation(self):
        self.assertEqual(self.report.probable_cause().remediation, "Roll back the deploy.")

    def test_status_reflects_the_worst_finding(self):
        self.assertEqual(self.report.status, CRIT)

    def test_ranked_order_is_severity_then_confidence(self):
        ranked = self.report.ranked()
        self.assertEqual(ranked[0].severity, CRIT)
        crits = [f.confidence for f in ranked if f.severity == CRIT]
        self.assertEqual(crits, sorted(crits, reverse=True))

    def test_ties_break_toward_the_earliest_signal(self):
        report = Report("x", "us-east-1", "1", "1h")
        late = report.add(Finding(CRIT, "a", "later", at=stubs.ago(minutes=5),
                                  confidence=0.8))
        early = report.add(Finding(CRIT, "b", "earlier", at=stubs.ago(minutes=50),
                                   confidence=0.8))
        self.assertIs(report.probable_cause(), early)
        self.assertIsNot(report.probable_cause(), late)


class TestReportRendering(unittest.TestCase):
    def setUp(self):
        self.report = Report("payments", "us-east-1", "1", "1h", "prod")
        self.report.add(Finding(CRIT, "logs", "480 error lines", "mostly timeouts",
                                evidence=["12× connection refused"],
                                remediation="Fix the timeouts.", at=SPIKE, confidence=0.7))
        self.report.note_checked("logs")
        self.report.skip("changes", "skipped (--no-changes)")

    def test_markdown_has_the_expected_sections(self):
        md = self.report.to_markdown()
        for heading in ("# Incident summary", "## Probable cause", "## Findings",
                        "## Timeline", "## Evidence", "## What was checked"):
            self.assertIn(heading, md)

    def test_markdown_escapes_pipes_so_tables_survive(self):
        report = Report("x", "us-east-1", "1", "1h")
        report.add(Finding(CRIT, "logs", "pipe | in title", "a | b | c", confidence=0.5))
        rows = [ln for ln in report.to_markdown().splitlines()
                if ln.startswith("| ") and "pipe" in ln]
        self.assertEqual(len(rows), 1, "expected exactly one table row")
        # Five columns means the pipes inside the cells did not split the row.
        self.assertEqual(rows[0].count("|") - rows[0].count("\\|"), 5)
        self.assertIn("\\|", rows[0])

    def test_json_is_valid_and_complete(self):
        parsed = json.loads(self.report.to_json())
        self.assertEqual(parsed["status"], CRIT)
        self.assertEqual(parsed["skipped"]["changes"], "skipped (--no-changes)")
        self.assertEqual(len(parsed["findings"]), 1)
        self.assertEqual(parsed["timeline"][0]["source"], "logs")

    def test_empty_report_still_renders(self):
        empty = Report("quiet", "us-east-1", "1", "1h")
        self.assertIn("No problem signals", empty.to_markdown())
        self.assertIsNone(json.loads(empty.to_json())["probable_cause"])


class TestNoiseFiltering(unittest.TestCase):
    """CloudTrail is mostly noise; the signal has to survive it."""

    def test_read_only_calls_are_filtered(self):
        self.assertTrue(changes._READ_ONLY.match("DescribeInstances"))
        self.assertTrue(changes._READ_ONLY.match("ListBuckets"))
        self.assertFalse(changes._READ_ONLY.match("UpdateService"))

    def test_routine_data_plane_calls_are_noise(self):
        for name in ("Decrypt", "GenerateDataKey", "StartQuery", "AssumeRole"):
            self.assertIn(name, changes._NOISE_EVENTS)

    def test_deploy_style_calls_are_weighted_above_default(self):
        for name in ("UpdateFunctionCode", "UpdateService", "TerminateInstances"):
            self.assertGreater(changes._HIGH_SIGNAL[name], 0.5)


if __name__ == "__main__":
    unittest.main(verbosity=2)
