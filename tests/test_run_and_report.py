#!/usr/bin/env python3
"""Tests for scripts/run-and-report.sh."""

import os
import subprocess
import unittest

_SCRIPT_PATH = os.path.join(
    os.path.dirname(__file__), "..", "scripts", "run-and-report.sh"
)


def _run(*args, env=None):
    command_env = os.environ.copy()
    if env:
        command_env.update(env)
    return subprocess.run(
        ["bash", _SCRIPT_PATH, *args],
        capture_output=True,
        text=True,
        env=command_env,
    )


class TestRunAndReport(unittest.TestCase):
    """The helper must preserve the command's exit status and report its tail."""

    def test_success_reports_exit_zero_and_output(self):
        result = _run("sh", "-c", "echo hello")

        self.assertEqual(result.returncode, 0)
        self.assertIn("hello", result.stderr)
        self.assertIn("EXIT=0", result.stderr)
        self.assertEqual(result.stdout, "")

    def test_failure_preserves_exit_code_and_output(self):
        result = _run("sh", "-c", "echo boom >&2; exit 7")

        self.assertEqual(result.returncode, 7)
        self.assertIn("boom", result.stderr)
        self.assertIn("EXIT=7", result.stderr)

    def test_reports_only_the_last_lines(self):
        result = _run(
            "sh",
            "-c",
            "for i in 1 2 3 4 5; do echo line$i; done",
            env={"RUN_REPORT_LINES": "2"},
        )

        self.assertEqual(result.returncode, 0)
        self.assertIn("line4", result.stderr)
        self.assertIn("line5", result.stderr)
        self.assertNotIn("line1", result.stderr)

    def test_no_arguments_prints_usage(self):
        result = _run()

        self.assertEqual(result.returncode, 2)
        self.assertIn("Usage", result.stderr)

    def test_unwritable_temp_dir_exits_three(self):
        result = _run(
            "sh",
            "-c",
            "echo hello",
            env={"TMPDIR": "/nonexistent/runner-report"},
        )

        self.assertEqual(result.returncode, 3)
        self.assertIn("cannot create temporary log", result.stderr)


if __name__ == "__main__":
    unittest.main()
