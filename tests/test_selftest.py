"""Tests for the built-in AMD64 self-test."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from crossrun.core.backends import BackendInfo
from crossrun.core.elf import read_elf_info
from crossrun.core.selftest import (
    SELFTEST_MESSAGE,
    build_selftest_binary,
    run_selftest,
)


class SelftestBinaryTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)

    def test_binary_is_a_static_amd64_elf(self) -> None:
        binary = build_selftest_binary(self.tmp)
        self.assertTrue(binary.stat().st_mode & 0o111)
        info = read_elf_info(binary)
        self.assertTrue(info.is_amd64)
        self.assertTrue(info.is_static)
        self.assertIn(SELFTEST_MESSAGE.encode(), binary.read_bytes())

    def test_fake_backend_runs_the_payload(self) -> None:
        script = self.tmp / "box64"
        script.write_text(
            "#!/bin/sh\n"
            f"echo '{SELFTEST_MESSAGE}'\n"
            "exit 0\n"
        )
        script.chmod(0o755)
        infos = [BackendInfo("box64", "Box64", str(script), "fake")]
        result = run_selftest(backend="box64", infos=infos)
        self.assertTrue(result.ok)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.backend, "box64")
        self.assertIsNotNone(result.plan)
        self.assertEqual(result.plan.argv[0], str(script))
        self.assertIn("PASS", result.render())

    def test_backend_failure_is_reported(self) -> None:
        script = self.tmp / "box64"
        script.write_text("#!/bin/sh\necho 'emulation exploded'\nexit 7\n")
        script.chmod(0o755)
        infos = [BackendInfo("box64", "Box64", str(script), "fake")]
        result = run_selftest(backend="box64", infos=infos)
        self.assertFalse(result.ok)
        self.assertEqual(result.returncode, 7)
        self.assertIn("emulation exploded", result.message)
        self.assertIn("FAIL", result.render())

    def test_missing_backend_is_reported(self) -> None:
        result = run_selftest(
            backend="qemu",
            infos=[BackendInfo("qemu", "QEMU", None)],
        )
        self.assertFalse(result.ok)
        self.assertIn("not installed", result.message)


if __name__ == "__main__":
    unittest.main()
