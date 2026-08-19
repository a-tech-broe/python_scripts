"""Log clustering, metric maths, windows, and table rendering.

These are the pure functions the diagnostics are built on. Every case here is one that
was wrong at some point against real data.
"""

from __future__ import annotations

import argparse
import io
import re
import sys
import unittest
from contextlib import redirect_stdout
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "lib"))

from sretk import logs, metrics, out, timewin  # noqa: E402


class TestLogFingerprint(unittest.TestCase):
    def test_identical_errors_with_different_ids_merge(self):
        a = ("ERROR 2026-08-18T10:00:00Z req=550e8400-e29b-41d4-a716-446655440000 "
             "connection refused to 10.0.1.5:5432")
        b = ("ERROR 2026-08-18T11:30:00Z req=990e8400-e29b-41d4-a716-446655440111 "
             "connection refused to 10.0.9.7:5432")
        self.assertEqual(logs.fingerprint(a), logs.fingerprint(b))

    def test_genuinely_different_errors_stay_apart(self):
        self.assertNotEqual(
            logs.fingerprint("ERROR connection refused"),
            logs.fingerprint("ERROR permission denied"),
        )

    def test_volatile_parts_are_normalised(self):
        shape = logs.fingerprint(
            "2026-08-19 00:16:01 UTC::@:[1336]:LOG: write=0.207 s, distance=65536 kB, "
            "lsn=7E/B8000600")
        for placeholder in ("<ts>", "<num>", "<lsn>"):
            self.assertIn(placeholder, shape)

    def test_shape_is_length_capped(self):
        self.assertLessEqual(len(logs.fingerprint("x" * 1000)), 240)


class TestErrorPattern(unittest.TestCase):
    """The filter that decides what counts as an error line."""

    def match(self, line: str) -> bool:
        return bool(re.search(logs.ERROR_PATTERN, line))

    def test_real_errors_match(self):
        for line in ("connection refused", "NullPointerException", "FATAL: out of memory",
                     "request timed out", "access denied", "OOMKilled"):
            self.assertTrue(self.match(line), line)

    def test_postgres_checkpoint_lines_do_not_match(self):
        # Regression: a bare 5\d{2} for HTTP 5xx matched the "536" inside 65536.
        self.assertFalse(self.match(
            "LOG: checkpoint complete: wrote 3 buffers; distance=65536 kB, "
            "estimate=65536 kB"))

    def test_ordinary_info_lines_do_not_match(self):
        self.assertFalse(self.match("started listening on port 8080"))

    def test_structured_info_lines_are_noise(self):
        # Regression: `"failed": 0` inside an INFO payload is not an error.
        line = '{"level": "INFO", "event": "done", "processed": 1, "failed": 0}'
        self.assertTrue(self.match(line), "the word 'failed' does appear")
        self.assertTrue(re.search(logs.NOISE_PATTERN, line),
                        "but the line's own level marks it as noise")

    def test_structured_error_lines_are_not_noise(self):
        line = '{"level": "ERROR", "event": "bedrock_invoke_failed"}'
        self.assertTrue(self.match(line))
        self.assertFalse(re.search(logs.NOISE_PATTERN, line))

    def test_filter_clause_excludes_noise(self):
        clause = logs._filter_clause()
        self.assertIn("not like", clause)
        self.assertIn("filter @message like", clause)


class TestSpikeDetection(unittest.TestCase):
    def test_flat_series_has_no_spike(self):
        self.assertIsNone(metrics.spike_index([2, 2, 3, 2, 2, 3, 2, 2]))

    def test_spike_is_found_at_the_jump(self):
        self.assertEqual(metrics.spike_index([1, 0, 1, 1, 1, 45, 60, 55]), 5)

    def test_short_series_is_not_guessed_at(self):
        self.assertIsNone(metrics.spike_index([1, 90]))

    def test_all_zero_series_has_no_spike(self):
        self.assertIsNone(metrics.spike_index([0, 0, 0, 0, 0, 0]))


class TestMetricHelpers(unittest.TestCase):
    def setUp(self):
        self.series = {"err": [1.0, 2.0, 3.0], "inv": [10.0, 10.0, 10.0], "empty": []}

    def test_total_mean_peak(self):
        self.assertEqual(metrics.total(self.series, "err"), 6.0)
        self.assertEqual(metrics.mean(self.series, "err"), 2.0)
        self.assertEqual(metrics.peak(self.series, "err"), 3.0)

    def test_missing_series_is_none_not_zero(self):
        # "no data" and "zero" must stay distinguishable.
        self.assertIsNone(metrics.mean(self.series, "empty"))
        self.assertIsNone(metrics.peak(self.series, "nonexistent"))

    def test_rate_pct(self):
        self.assertAlmostEqual(metrics.rate_pct(self.series, "err", "inv"), 20.0)

    def test_rate_pct_guards_zero_denominator(self):
        self.assertIsNone(metrics.rate_pct(self.series, "err", "empty"))


class TestWindows(unittest.TestCase):
    def test_parsing(self):
        self.assertEqual(timewin.parse("30m"), timedelta(minutes=30))
        self.assertEqual(timewin.parse("6h"), timedelta(hours=6))
        self.assertEqual(timewin.parse("2d"), timedelta(days=2))

    def test_rejects_nonsense(self):
        for bad in ("3s", "week", "", "1y", "-4h"):
            with self.assertRaises(argparse.ArgumentTypeError, msg=bad):
                timewin.parse(bad)

    def test_rejects_out_of_range(self):
        with self.assertRaises(argparse.ArgumentTypeError):
            timewin.parse("1m")     # below the 5m floor
        with self.assertRaises(argparse.ArgumentTypeError):
            timewin.parse("30d")    # above the 14d ceiling

    def test_labels_round_trip(self):
        for raw in ("30m", "6h", "2d"):
            self.assertEqual(timewin.label(timewin.parse(raw)), raw)

    def test_period_is_cloudwatch_legal(self):
        legal = {60, 300, 900, 3600, 21600, 86400}
        for raw in ("30m", "1h", "6h", "24h", "7d", "14d"):
            self.assertIn(timewin.period(timewin.parse(raw)), legal)

    def test_longer_windows_use_coarser_periods(self):
        self.assertLessEqual(timewin.period(timewin.parse("1h")),
                             timewin.period(timewin.parse("24h")))


class TestOutput(unittest.TestCase):
    def test_strip_removes_ansi(self):
        self.assertEqual(out.strip("\033[31mred\033[0m"), "red")

    def test_table_aligns_around_colour_codes(self):
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            out.table(["a", "b"], [["\033[31mxx\033[0m", "1"], ["yyyy", "2"]])
        columns = [out.strip(line).rstrip().split() for line in
                   buffer.getvalue().splitlines()[1:]]
        # Second column starts at the same offset on every row.
        offsets = [out.strip(line).index("1" if "1" in line else "2")
                   for line in buffer.getvalue().splitlines()[1:]]
        self.assertEqual(len(set(offsets)), 1, f"misaligned: {columns}")

    def test_sparkline_scales_and_handles_flat_series(self):
        self.assertEqual(len(out.sparkline([1, 2, 3, 4], 4)), 4)
        self.assertEqual(out.sparkline([0, 0, 0], 3), "▁▁▁")
        self.assertEqual(out.sparkline([5], 4), "", "one point is not a trend")

    def test_sparkline_buckets_down_to_width(self):
        self.assertEqual(len(out.sparkline(list(range(100)), 10)), 10)

    def test_severity_ranking_puts_crit_first(self):
        self.assertLess(out.RANK[out.CRIT], out.RANK[out.WARN])
        self.assertLess(out.RANK[out.WARN], out.RANK[out.INFO])


if __name__ == "__main__":
    unittest.main(verbosity=2)
