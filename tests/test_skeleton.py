"""Regression checks for starter plumbing, without real forks or SysV queues."""
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


class SkeletonPlumbingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        compiler = shutil.which("cc")
        if compiler is None:
            raise unittest.SkipTest("C compiler is unavailable")
        cls.root = Path(os.environ.get("SKELETON_ROOT", Path(__file__).resolve().parents[1]))
        cls.temp = tempfile.TemporaryDirectory(prefix="skeleton-plumbing-")
        cls.addClassCleanup(cls.temp.cleanup)
        cls.binary = Path(cls.temp.name) / "harness"
        result = subprocess.run(
            [compiler, "-Wall", "-Wextra", "-Werror", "-I", str(cls.root / "skeleton"),
             str(Path(__file__).with_name("skeleton_harness.c")), "-o", str(cls.binary)],
            capture_output=True, text=True, timeout=30,
        )
        if result.returncode:
            raise AssertionError(result.stdout + result.stderr)

    def run_case(self, name, workload_path=None):
        result = subprocess.run(
            [str(self.binary), name, str(workload_path or self.root / "workload.txt")],
            capture_output=True, text=True, timeout=5,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return result

    def test_long_trace_retries_interrupted_and_partial_writes(self):
        self.run_case("long_trace")

    def test_partial_fork_failure_cleans_only_created_children(self):
        result = self.run_case("fork_failure")
        self.assertIn("fork", result.stderr)

    def test_trace_open_failure_cleans_all_created_children(self):
        result = self.run_case("open_failure")
        self.assertIn("open trace", result.stderr)

    def test_cleanup_retries_wait_and_handles_sparse_pids_once(self):
        self.run_case("cleanup_success")

    def test_cleanup_reports_queue_and_file_errors(self):
        result = self.run_case("cleanup_failure")
        self.assertIn("remove message queue", result.stderr)
        self.assertIn("close", result.stderr)

    def test_zero_byte_write_fails_and_cleans_resources(self):
        result = self.run_case("zero_write")
        self.assertIn("write", result.stderr)

    def test_workload_reader_discards_entire_long_ascii_and_utf8_comments(self):
        # Both full-line comments have 4096 characters, within the public
        # input contract. Neither may leave continuation chunks as tokens.
        records = "tasks 10\n" + "\n".join(
            f"task {task} 0 1 1 0" for task in range(10)
        )
        comments = "#" + "x" * 4095 + "\n" + " \t#" + "한" * 4093 + "\n"
        with tempfile.TemporaryDirectory(prefix="skeleton-workload-") as directory:
            workload = Path(directory) / "comments.txt"
            workload.write_text(comments + records, encoding="utf-8")
            result = self.run_case("read_workload", workload)
        # Preserve record boundaries and the final record without a newline.
        self.assertEqual(result.stdout, records)

    def test_workload_reader_preserves_records_after_more_than_64k_of_blank_lines(self):
        records = "tasks 10\n" + "".join(
            f"task {task} 0 1 1 0\n" for task in range(10)
        )
        # Legal blank lines previously exhausted the fixed 64-KiB buffer
        # before it reached any actual workload records.
        contents = "\n" * 65536 + records
        with tempfile.TemporaryDirectory(prefix="skeleton-workload-") as directory:
            workload = Path(directory) / "blank-lines.txt"
            workload.write_text(contents, encoding="utf-8")
            result = self.run_case("read_workload", workload)
        self.assertEqual(result.stdout, contents)


if __name__ == "__main__":
    unittest.main()
