"""Tests for the subprocess runner."""

from __future__ import annotations

import sys
import unittest
from unittest.mock import patch

from crossrun.core.runner import Command, Runner
from crossrun.errors import CommandError


class RunnerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.lines: list[str] = []
        self.runner = Runner(echo=self.lines.append)

    def test_captures_stdout(self) -> None:
        result = self.runner.run(
            Command((sys.executable, "-c", "print('hello runner')"))
        )
        self.assertTrue(result.ok)
        self.assertEqual(result.stdout.strip(), "hello runner")
        self.assertTrue(self.lines[0].startswith("$ "))

    def test_failure_raises_and_reports_output(self) -> None:
        with self.assertRaises(CommandError) as caught:
            self.runner.run(
                Command(
                    (
                        sys.executable,
                        "-c",
                        "import sys; print('broken'); sys.exit(3)",
                    )
                )
            )
        message = str(caught.exception)
        self.assertIn("exit code 3", message)
        self.assertIn("broken", message)

    def test_check_false_returns_result(self) -> None:
        result = self.runner.run(
            Command((sys.executable, "-c", "import sys; sys.exit(5)")),
            check=False,
        )
        self.assertEqual(result.returncode, 5)
        self.assertFalse(result.ok)

    def test_streaming_lines(self) -> None:
        streamed: list[str] = []
        result = self.runner.run(
            Command((sys.executable, "-c", "print('one'); print('two')")),
            on_line=streamed.append,
        )
        self.assertTrue(result.ok)
        self.assertEqual(streamed, ["one", "two"])
        self.assertEqual(result.stdout, "one\ntwo")

    def test_missing_command(self) -> None:
        with self.assertRaises(CommandError):
            self.runner.run(Command(("/definitely/not/a/command",)))

    def test_dry_run_does_not_execute(self) -> None:
        runner = Runner(dry_run=True, echo=self.lines.append)
        result = runner.run(Command((sys.executable, "-c", "raise SystemExit(9)")))
        self.assertEqual(result.returncode, 0)
        self.assertIn("$ ", self.lines[0])

    def test_root_prefix_and_cwd_display(self) -> None:
        command = Command(("true",), needs_root=True, cwd="/tmp")
        with patch("crossrun.core.runner.os.geteuid", return_value=1000), patch(
            "crossrun.core.runner.shutil.which", return_value="/usr/bin/sudo"
        ):
            argv = self.runner.materialize(command)
            display = self.runner.display(command)
        self.assertEqual(argv, ["/usr/bin/sudo", "true"])
        self.assertIn("cd /tmp", display)
        self.assertIn("/usr/bin/sudo true", display)

    def test_root_without_sudo(self) -> None:
        command = Command(("true",), needs_root=True)
        with patch("crossrun.core.runner.os.geteuid", return_value=1000), patch(
            "crossrun.core.runner.shutil.which", return_value=None
        ):
            with self.assertRaises(CommandError):
                self.runner.materialize(command)


if __name__ == "__main__":
    unittest.main()
