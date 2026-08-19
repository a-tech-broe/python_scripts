"""ECS diagnosis — the logic live AWS cannot exercise without a broken cluster."""

from __future__ import annotations

import sys
import unittest
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "lib"))

import stubs  # noqa: E402
from sretk import CRIT, OK, Report, WARN, ecs  # noqa: E402

WINDOW = timedelta(hours=1)


def diagnose(service=None, tasks=None, targets=None, cpu=None) -> Report:
    aws = stubs.FakeAws(
        clients={
            "ecs": stubs.FakeEcs(service, tasks),
            "elbv2": stubs.FakeElb(targets),
            "cloudwatch": stubs.FakeCloudWatch(cpu),
        },
        pages={"list_tasks": ["arn:task/1", "arn:task/2"]},
    )
    report = Report("payments", "us-east-1", "1", "1h", "prod")
    ecs.diagnose(aws, "prod-cluster", "payments", report, WINDOW)
    return report


def titles(report: Report) -> str:
    return " | ".join(f.title for f in report.findings)


class TestBrokenService(unittest.TestCase):
    """A service that is failing in several ways at once."""

    @classmethod
    def setUpClass(cls):
        cls.report = diagnose(cpu=[95.0, 97.0, 99.0])

    def test_overall_status_is_critical(self):
        self.assertEqual(self.report.status, CRIT)

    def test_no_running_tasks_is_reported(self):
        self.assertIn("No tasks running", titles(self.report))

    def test_failed_rollout_is_reported(self):
        self.assertIn("Deployment rollout failed", titles(self.report))

    def test_oom_is_recognised_from_the_stop_reason(self):
        oom = [f for f in self.report.findings if "memory limit" in f.title]
        self.assertTrue(oom, "OOM stop reason should be recognised")
        self.assertEqual(oom[0].severity, CRIT)
        self.assertIn("2×", oom[0].title, "identical stop reasons should be grouped")
        self.assertIn("memory", oom[0].remediation.lower())

    def test_crash_looping_event_is_recognised(self):
        self.assertIn("Tasks crash-looping on start", titles(self.report))

    def test_unhealthy_targets_are_reported(self):
        unhealthy = [f for f in self.report.findings if "targets unhealthy" in f.title]
        self.assertTrue(unhealthy)
        self.assertEqual(unhealthy[0].severity, CRIT, "all targets down is critical")
        self.assertTrue(any("503" in line for line in unhealthy[0].evidence))

    def test_target_group_is_named_not_arn_suffixed(self):
        # arn:...:targetgroup/tg-payments/9d0f1a2b3c — the name, not the random id.
        text = titles(self.report)
        self.assertIn("tg-payments", text)
        self.assertNotIn("9d0f1a2b3c", text)

    def test_saturation_is_flagged(self):
        self.assertIn("CPU peaked at 99%", titles(self.report))

    def test_probable_cause_is_the_deployment_not_a_downstream_symptom(self):
        cause = self.report.probable_cause()
        self.assertIsNotNone(cause)
        self.assertEqual(cause.title, "Deployment rollout failed")
        self.assertIn("roll back", cause.remediation.lower())

    def test_timeline_is_ordered(self):
        stamps = [e.at for e in self.report.timeline()]
        self.assertEqual(stamps, sorted(stamps))

    def test_report_renders_in_every_format(self):
        self.assertIn("Deployment rollout failed", self.report.to_markdown())
        import json
        parsed = json.loads(self.report.to_json())
        self.assertEqual(parsed["status"], CRIT)
        self.assertEqual(parsed["probable_cause"]["title"], "Deployment rollout failed")


class TestHealthyService(unittest.TestCase):
    """The quiet case has to stay quiet, or the tool cries wolf."""

    @classmethod
    def setUpClass(cls):
        cls.report = diagnose(service=stubs.healthy_service(), tasks=[],
                              targets=stubs.healthy_targets(), cpu=[20.0, 25.0, 22.0])

    def test_no_problems(self):
        self.assertEqual(self.report.problems(), [])

    def test_status_is_ok(self):
        self.assertEqual(self.report.status, OK)

    def test_task_count_reported_as_ok(self):
        self.assertIn("Task count at desired", titles(self.report))

    def test_no_probable_cause(self):
        self.assertIsNone(self.report.probable_cause())


class TestMissingService(unittest.TestCase):
    def test_missing_service_is_skipped_not_crashed(self):
        report = Report("nope", "us-east-1", "1", "1h")
        aws = stubs.FakeAws(clients={"ecs": stubs.FakeEcs(service={})})
        result = ecs.diagnose(aws, "prod-cluster", "nope", report, WINDOW)
        self.assertIsNone(result)
        self.assertIn("ecs", report.skipped)


class TestEventPatterns(unittest.TestCase):
    """Service-event text has to map to the right severity and advice."""

    def test_capacity_failure(self):
        service = stubs.healthy_service()
        service["events"] = [{
            "createdAt": stubs.ago(minutes=5),
            "message": "(service payments) was unable to place a task because no container "
                       "instance met all of its requirements.",
        }]
        report = diagnose(service=service, tasks=[], targets=[], cpu=[5.0])
        hit = [f for f in report.findings if "capacity" in f.title.lower()]
        self.assertTrue(hit, "placement failure should be recognised")
        self.assertEqual(hit[0].severity, CRIT)

    def test_steady_state_is_not_a_problem(self):
        service = stubs.healthy_service()
        report = diagnose(service=service, tasks=[], targets=[], cpu=[5.0])
        self.assertNotIn("steady state", " ".join(f.title for f in report.problems()).lower())


if __name__ == "__main__":
    unittest.main(verbosity=2)
