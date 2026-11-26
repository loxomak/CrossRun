"""Tests for the environment doctor."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from crossrun.core.backends import BackendInfo
from crossrun.core.doctor import Status, run_doctor
from crossrun.core.installer import BinfmtStatus
from crossrun.core.platform import HostPlatform
from crossrun.core.registry import Registry


def pi5() -> HostPlatform:
    return HostPlatform(
        machine="aarch64",
        system="linux",
        distro_id="debian",
        distro_version="13",
        distro_name="Debian GNU/Linux",
        model="Raspberry Pi 5 Model B Rev 1.1",
        ram_mb=8058,
        is_raspberry_pi=True,
        pi_generation=5,
    )


class DoctorTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.loader = self.tmp / "ld-linux-x86-64.so.2"
        self.loader.write_text("")
        self.registry = Registry(self.tmp / "apps.json")

    def check(self, report, name: str):
        return next(check for check in report.checks if check.name == name)

    def test_healthy_system(self) -> None:
        report = run_doctor(
            platform=pi5(),
            infos=[BackendInfo("box64", "Box64", "/usr/local/bin/box64", "v0.3.4")],
            binfmt_handlers={
                "qemu-x86_64": BinfmtStatus(
                    True, True, "/usr/libexec/qemu-binfmt/x86_64-binfmt-P", "OCF"
                )
            },
            loader=self.loader,
            sysroot=self.tmp / "sysroot",
            which=lambda name: "/usr/bin/sudo",
            registry=self.registry,
        )
        self.assertTrue(report.healthy)
        self.assertEqual(self.check(report, "Host architecture").status, Status.OK)
        self.assertEqual(self.check(report, "Translation backends").status, Status.OK)
        self.assertEqual(self.check(report, "binfmt handler").status, Status.OK)
        self.assertEqual(self.check(report, "AMD64 runtime libraries").status, Status.OK)
        self.assertIn("v0.3.4", self.check(report, "Translation backends").detail)
        self.assertEqual(report.failures, [])

    def test_missing_backend_is_a_failure(self) -> None:
        report = run_doctor(
            platform=pi5(),
            infos=[BackendInfo("box64", "Box64", None)],
            binfmt_handlers={},
            loader=self.loader,
            which=lambda name: None,
            registry=self.registry,
        )
        self.assertFalse(report.healthy)
        backend_check = self.check(report, "Translation backends")
        self.assertEqual(backend_check.status, Status.FAIL)
        self.assertIn("crossrun setup", backend_check.hint or "")
        self.assertEqual(self.check(report, "Privileges").status, Status.WARN)
        self.assertEqual(self.check(report, "binfmt handler").status, Status.INFO)

    def test_unsupported_host(self) -> None:
        report = run_doctor(
            platform=HostPlatform(machine="x86_64", system="linux"),
            infos=[BackendInfo("box64", "Box64", "/bin/box64")],
            binfmt_handlers={},
            loader=self.loader,
            which=lambda name: "/usr/bin/sudo",
            registry=self.registry,
        )
        self.assertFalse(report.healthy)
        self.assertEqual(self.check(report, "Host architecture").status, Status.FAIL)
        self.assertIn("64-bit ARM", self.check(report, "Host architecture").hint)

    def test_missing_libraries_warns(self) -> None:
        report = run_doctor(
            platform=pi5(),
            infos=[BackendInfo("box64", "Box64", "/bin/box64")],
            binfmt_handlers={
                "qemu-x86_64": BinfmtStatus(True, False, "", "")
            },
            loader=None,
            sysroot=self.tmp / "empty",
            which=lambda name: "/usr/bin/sudo",
            registry=self.registry,
        )
        self.assertEqual(
            self.check(report, "AMD64 runtime libraries").status, Status.WARN
        )
        self.assertEqual(self.check(report, "binfmt handler").status, Status.WARN)
        self.assertTrue(report.warnings)

    def test_json_and_render(self) -> None:
        report = run_doctor(
            platform=pi5(),
            infos=[BackendInfo("qemu", "QEMU", "/usr/bin/qemu-x86_64-static")],
            binfmt_handlers={},
            loader=self.loader,
            which=lambda name: "/usr/bin/sudo",
            registry=self.registry,
        )
        data = report.as_dict()
        self.assertTrue(data["healthy"])
        self.assertEqual(data["host"]["model"], "Raspberry Pi 5 Model B Rev 1.1")
        self.assertTrue(any(item["status"] == "ok" for item in data["checks"]))
        text = report.render()
        self.assertIn("CrossRun doctor", text)
        self.assertIn("Summary:", text)
        self.assertIn("application(s)", text)


if __name__ == "__main__":
    unittest.main()
