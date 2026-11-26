"""End-to-end tests that exercise the ``crossrun`` command line interface."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from crossrun.core.selftest import SELFTEST_MESSAGE, build_selftest_binary
from tests.support import make_elf

ROOT = Path(__file__).resolve().parents[1]


class CliTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.home = self.tmp / "home"
        self.data = self.tmp / "data"
        self.cache = self.tmp / "cache"
        for path in (self.home, self.data, self.cache):
            path.mkdir(parents=True)

        self.bin_dir = self.tmp / "bin"
        self.bin_dir.mkdir()
        self.fake_backend = self.bin_dir / "box64"
        self.fake_backend.write_text(
            "#!/bin/sh\n"
            'if [ "$1" = "--version" ]; then echo "Box64 fake 0.0.0"; exit 0; fi\n'
            f"echo '{SELFTEST_MESSAGE}'\n"
            "exit 0\n"
        )
        self.fake_backend.chmod(0o755)

        self.env = dict(os.environ)
        self.env.update(
            {
                "HOME": str(self.home),
                "XDG_DATA_HOME": str(self.data),
                "XDG_CACHE_HOME": str(self.cache),
                "PYTHONPATH": str(ROOT),
                "PATH": f"{self.bin_dir}:{os.environ.get('PATH', '')}",
                "DISPLAY": "",
                "WAYLAND_DISPLAY": "",
            }
        )
        self.binary = build_selftest_binary(self.tmp / "src")
        self.added_name = self.binary.stem

    def run_cli(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, "-m", "crossrun", *args],
            cwd=ROOT,
            env=self.env,
            capture_output=True,
            text=True,
            timeout=120,
        )

    def test_version_and_help(self) -> None:
        version = self.run_cli("--version")
        self.assertEqual(version.returncode, 0)
        self.assertIn("0.1.0", version.stdout)
        help_output = self.run_cli("--help")
        self.assertEqual(help_output.returncode, 0)
        for command in ("doctor", "setup", "add", "run", "selftest", "libs", "gui"):
            self.assertIn(command, help_output.stdout)

    def test_library_lifecycle(self) -> None:
        empty = self.run_cli("list", "--json")
        self.assertEqual(empty.returncode, 0)
        self.assertEqual(json.loads(empty.stdout), [])

        added = self.run_cli("add", str(self.binary))
        self.assertEqual(added.returncode, 0, added.stderr)
        self.assertIn("Added", added.stdout)

        listing = json.loads(self.run_cli("list", "--json").stdout)
        self.assertEqual(len(listing), 1)
        entry = listing[0]
        self.assertEqual(entry["name"], self.added_name)
        self.assertEqual(entry["kind"], "elf")

        info = self.run_cli("info", self.added_name, "--json")
        self.assertEqual(info.returncode, 0)
        self.assertEqual(json.loads(info.stdout)["id"], entry["id"])

        dry_run = self.run_cli(
            "run", "--dry-run", "--backend", "box64", self.added_name
        )
        self.assertEqual(dry_run.returncode, 0, dry_run.stderr)
        self.assertIn(str(self.fake_backend), dry_run.stdout)
        self.assertIn(str(self.binary), dry_run.stdout)

        exec_dry_run = self.run_cli(
            "exec", "--dry-run", "--backend", "box64", str(self.binary), "--", "--flag"
        )
        self.assertEqual(exec_dry_run.returncode, 0, exec_dry_run.stderr)
        self.assertIn("--flag", exec_dry_run.stdout)

        removed = self.run_cli("remove", "--yes", self.added_name)
        self.assertEqual(removed.returncode, 0, removed.stderr)
        self.assertEqual(json.loads(self.run_cli("list", "--json").stdout), [])

    def test_add_rejects_non_amd64_and_duplicates(self) -> None:
        arm = make_elf(self.tmp / "arm", machine=183)
        rejected = self.run_cli("add", str(arm))
        self.assertEqual(rejected.returncode, 1)
        self.assertIn("ARM64", rejected.stderr)

        first = self.run_cli("add", str(self.binary))
        self.assertEqual(first.returncode, 0, first.stderr)
        duplicate = self.run_cli("add", str(self.binary))
        self.assertEqual(duplicate.returncode, 1)
        self.assertIn("already in the library", duplicate.stderr)

    def test_selftest_through_cli(self) -> None:
        result = self.run_cli("selftest", "--backend", "box64")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("PASS", result.stdout)
        payload = self.run_cli("selftest", "--backend", "box64", "--json")
        data = json.loads(payload.stdout)
        self.assertTrue(data["ok"])
        self.assertEqual(data["backend"], "box64")

    def test_doctor_json(self) -> None:
        result = self.run_cli("doctor", "--json")
        self.assertIn(result.returncode, (0, 1))
        report = json.loads(result.stdout)
        self.assertIn("checks", report)
        names = {item["name"] for item in report["checks"]}
        self.assertIn("Host architecture", names)
        self.assertIn("Translation backends", names)

    def test_unknown_application(self) -> None:
        result = self.run_cli("info", "does-not-exist")
        self.assertEqual(result.returncode, 1)
        self.assertIn("not in the CrossRun library", result.stderr)

    def test_add_env_and_args(self) -> None:
        added = self.run_cli(
            "add",
            str(self.binary),
            "--name",
            "EnvApp",
            "--env",
            "FOO=bar",
            "--arg=--flag",
        )
        self.assertEqual(added.returncode, 0, added.stderr)
        info = json.loads(self.run_cli("info", "EnvApp", "--json").stdout)
        self.assertEqual(info["env"], {"FOO": "bar"})
        self.assertEqual(info["args"], ["--flag"])
        dry_run = self.run_cli("run", "--dry-run", "EnvApp")
        self.assertEqual(dry_run.returncode, 0, dry_run.stderr)
        self.assertIn("FOO=bar", dry_run.stdout)

    def test_invalid_env_is_rejected(self) -> None:
        result = self.run_cli("add", str(self.binary), "--env", "NOEQUALS")
        self.assertEqual(result.returncode, 1)
        self.assertIn("invalid --env", result.stderr)

    def test_gui_without_display(self) -> None:
        result = self.run_cli("gui")
        self.assertEqual(result.returncode, 1)
        self.assertIn("no graphical display", result.stderr)

    def test_libs_dry_run(self) -> None:
        result = self.run_cli("libs", "--dry-run", "zlib1g")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("zlib1g:amd64", result.stdout)
        self.assertIn("apt-get download zlib1g:amd64", result.stdout)
        self.assertIn("dpkg-deb -x", result.stdout)
        self.assertIn("Dry run complete", result.stdout)


if __name__ == "__main__":
    unittest.main()
