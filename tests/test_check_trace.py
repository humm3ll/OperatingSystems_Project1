"""Regression tests using hand-authored traces, never a second scheduler.

Run from the repository root: python3 -m unittest discover -s tests -v
OS_PROJ1_CHECKER can point at check_trace.py when testing an external copy.
"""

from contextlib import redirect_stderr, redirect_stdout
import importlib.util
import io
import os
from pathlib import Path
import re
import tempfile
import unittest


FIXTURES = Path(__file__).resolve().parent / "fixtures" / "check_trace"
CHECKER_PATH = Path(os.environ.get(
    "OS_PROJ1_CHECKER",
    str(Path(__file__).resolve().parents[1] / "check_trace.py"),
))
SPEC = importlib.util.spec_from_file_location("checker_under_test", CHECKER_PATH)
CHECKER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(CHECKER)
CATEGORIES = ("FORMAT", "STRUCTURE", "ORDER", "QUANTUM", "BURST", "ACCOUNTING")


class CheckTraceTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="os-proj1-trace-test-")
        self.addCleanup(self.directory.cleanup)

    def run_fixture(self, name, replacements=()):
        """Exercise main(), including file parsing, summaries and exit status."""
        trace = (FIXTURES / (name + ".trace.txt")).read_text(encoding="utf-8")
        workload = (FIXTURES / (name + ".workload.txt")).read_text(encoding="utf-8")
        for before, after in replacements:
            self.assertIn(before, trace, "mutation must change its intended fixture")
            trace = trace.replace(before, after, 1)

        directory = Path(self.directory.name)
        trace_path = directory / "trace.txt"
        workload_path = directory / "workload.txt"
        trace_path.write_text(trace, encoding="utf-8")
        workload_path.write_text(workload, encoding="utf-8")
        tick_count = sum(line.startswith("[") for line in trace.splitlines())
        stdout, stderr = io.StringIO(), io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            status = CHECKER.main([
                str(trace_path), str(workload_path), "--ticks", str(tick_count),
            ])
        output = stdout.getvalue() + stderr.getvalue()
        categories = dict(re.findall(
            r"^  ([A-Z]+)\s+(PASS|FAIL|SKIP)\b", stdout.getvalue(), re.MULTILINE,
        ))
        return status, categories, output

    def assert_result(self, result, fails=(), skips=()):
        status, categories, output = result
        expected = {category: "PASS" for category in CATEGORIES}
        expected.update((category, "FAIL") for category in fails)
        expected.update((category, "SKIP") for category in skips)
        self.assertEqual(status, 1 if fails or skips else 0, output)
        self.assertEqual(categories, expected, output)

    def test_rr_basic_ignores_io_and_keeps_quantum_across_bursts(self):
        self.assert_result(self.run_fixture("rr_basic"))

    def test_rr_zero_io_does_not_block_or_reset_quantum(self):
        self.assert_result(self.run_fixture("rr_zero_io"))

    def test_rr_iodone_precedes_arrivals_and_unserved_response_is_excluded(self):
        self.assert_result(self.run_fixture("rr_io_arrivals"))

    def test_iodone_without_subsequent_run_has_zero_response_average(self):
        self.assert_result(self.run_fixture("rr_unserved_io"))

    def test_unserved_iodone_cannot_contribute_an_incomplete_response(self):
        result = self.run_fixture("rr_unserved_io", [
            ("io_resp=0.00", "io_resp=1.00"),
        ])
        self.assert_result(result, fails=("ACCOUNTING",))

    def test_workload_accepts_arrival_cpu_and_io_beyond_signed_int_range(self):
        self.assert_result(self.run_fixture("rr_large_values"))

    def test_all_idle_has_zero_utilization_and_fairness_one(self):
        self.assert_result(self.run_fixture("rr_idle"))

    def test_mlfq_applies_pending_preemption_before_boost(self):
        self.assert_result(self.run_fixture("mlfq_pending_boost"))

    def test_mlfq_boost_wins_over_collision_io_return_same(self):
        self.assert_result(self.run_fixture("mlfq_boost_collision"))

    def test_vtime_equal_runtime_preserves_ready_entry_order(self):
        self.assert_result(self.run_fixture("vtime_tie"))

    def test_vtime_late_arrival_is_clamped_to_minimum_runtime(self):
        self.assert_result(self.run_fixture("vtime_arrival_clamp"))

    def test_vtime_io_wakeup_is_clamped_before_stable_insertion(self):
        self.assert_result(self.run_fixture("vtime_wakeup_clamp"))

    def test_wrong_q_left_fails_only_quantum(self):
        result = self.run_fixture("rr_basic", [("q_left=1", "q_left=0")])
        self.assert_result(result, fails=("QUANTUM",))

    def test_wrong_wq_countdown_fails_only_burst(self):
        result = self.run_fixture("rr_io_arrivals", [
            ("WQ: [P0(io:1)]", "WQ: [P0(io:2)]"),
        ])
        self.assert_result(result, fails=("BURST",))

    def test_wrong_event_phase_order_fails_only_order(self):
        result = self.run_fixture("rr_io_arrivals", [
            ("IODONE P0 ARRIVE P2 ARRIVE P3", "ARRIVE P2 IODONE P0 ARRIVE P3"),
        ])
        self.assert_result(result, fails=("ORDER",))

    def test_correct_metric_with_wrong_precision_fails_only_format(self):
        result = self.run_fixture("rr_io_arrivals", [
            ("avg_wait=1.50", "avg_wait=1.5"),
        ])
        self.assert_result(result, fails=("FORMAT",))

    def test_invalid_pid_metadata_does_not_suppress_semantic_checks(self):
        result = self.run_fixture("mlfq_boost_collision", [
            ("P9=4109", "P9=4100"),
        ])
        self.assert_result(result, fails=("FORMAT",))

    def test_missing_quantum_skips_only_dependent_categories(self):
        result = self.run_fixture("rr_basic", [(" quantum=2", "")])
        self.assert_result(result, fails=("FORMAT",), skips=("ORDER", "QUANTUM"))

    def test_invalid_tick_ms_skips_accounting_when_other_totals_are_correct(self):
        result = self.run_fixture("rr_basic", [("tick_ms=1", "tick_ms=bad")])
        self.assert_result(result, fails=("FORMAT",), skips=("ACCOUNTING",))

    def test_invalid_tick_ms_still_reports_independent_accounting_error(self):
        result = self.run_fixture("rr_basic", [
            ("tick_ms=1", "tick_ms=bad"),
            ("WAIT P0 total_wait=0 total_cpu=4", "WAIT P0 total_wait=0 total_cpu=3"),
        ])
        self.assert_result(result, fails=("FORMAT", "ACCOUNTING"))

    def test_mlfq_preemption_after_boost_is_rejected(self):
        result = self.run_fixture("mlfq_pending_boost", [
            ("RQ0: [P0] RQ1: []", "RQ0: [] RQ1: [P0]"),
        ])
        self.assert_result(result, fails=("ORDER",))

    def test_mlfq_collision_cannot_restore_level_from_before_boost(self):
        result = self.run_fixture("mlfq_boost_collision", [
            ("[0004] RUN P0 q_left=0", "[0004] RUN P0 q_left=1"),
            ("ev: IODONE P0 PREEMPT P0", "ev: IODONE P0"),
        ])
        self.assert_result(result, fails=("QUANTUM",))

    def test_vtime_late_arrival_cannot_get_unclamped_service(self):
        result = self.run_fixture("vtime_arrival_clamp", [
            (
                "[0002] RUN P0 q_left=0 | RQ: [P1] | WQ: [] | ev: ARRIVE P1 PREEMPT P0",
                "[0002] RUN P1 q_left=0 | RQ: [P0] | WQ: [] | ev: ARRIVE P1 PREEMPT P1",
            ),
        ])
        # The changed RUN/RQ owners also invalidate the unchanged WAIT/METRIC
        # constants; its q_left and burst timing remain internally correct.
        self.assert_result(result, fails=("ORDER", "ACCOUNTING"))


if __name__ == "__main__":
    unittest.main()
