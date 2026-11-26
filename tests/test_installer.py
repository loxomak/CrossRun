"""Tests for installation plans, binfmt parsing and dry-run execution."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from crossrun.core.installer import (
    BinfmtStatus,
    plan_setup,
    read_binfmt_status,
    run_setup,
    x86_binfmt_handlers,
)
from crossrun.core.platform import HostPlatform
from crossrun.core.runner import Runner
from crossrun.errors import SetupError, UnsupportedHostError


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


class PlanTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.source = self.tmp / "src" / "box64"

    def test_full_plan_contents(self) -> None:
        plan = plan_setup(
            pi5(),
            backend="both",
            with_libs=True,
            source_dir=self.source,
            jobs=3,
        )
        self.assertEqual(plan.backends, ("box64", "qemu"))
        titles = [step.title for step in plan.steps]
        self.assertEqual(titles[0], "Enable the amd64 package architecture")
        self.assertIn("Refresh package lists", titles)
        self.assertTrue(any("Compile Box64" in title for title in titles))
        self.assertFalse(any("Install x86-64 runtime libraries" in title for title in titles))

        all_args = [arg for step in plan.steps for arg in step.command.argv]
        self.assertIn("qemu-user-static", all_args)
        self.assertIn("squashfs-tools", all_args)
        self.assertNotIn("libc6:amd64", all_args)
        self.assertIn("-DRPI5ARM64=ON", all_args)
        self.assertIn("--parallel", all_args)
        self.assertTrue(any(step.command.needs_root for step in plan.steps))
        self.assertTrue(any(note.startswith("enabling the Raspberry Pi 5") for note in plan.notes))
        self.assertTrue(any("private sysroot" in note for note in plan.notes))

    def test_qemu_only_plan_has_no_compiler_steps(self) -> None:
        plan = plan_setup(pi5(), backend="qemu", source_dir=self.source)
        text = " ".join(
            " ".join(step.command.argv) + " " + step.title for step in plan.steps
        )
        self.assertIn("qemu-user-static", text)
        self.assertNotIn("cmake", text)
        self.assertNotIn("git clone", text)
        self.assertNotIn("libc6:amd64", text)

    def test_box64_only_plan(self) -> None:
        plan = plan_setup(pi5(), backend="box64", source_dir=self.source)
        text = " ".join(" ".join(step.command.argv) for step in plan.steps)
        self.assertIn("git", text)
        self.assertIn("cmake", text)
        self.assertNotIn("qemu-user-static", text)

    def test_unknown_backend_and_non_arm_host(self) -> None:
        with self.assertRaises(SetupError):
            plan_setup(pi5(), backend="wine", source_dir=self.source)
        intel = HostPlatform(machine="x86_64", system="linux")
        with self.assertRaises(UnsupportedHostError):
            plan_setup(intel, source_dir=self.source)
        with self.assertRaises(UnsupportedHostError):
            plan_setup(
                HostPlatform(machine="armv7l", system="linux"),
                source_dir=self.source,
            )

    def test_dry_run_executes_no_commands(self) -> None:
        plan = plan_setup(pi5(), backend="both", with_libs=True, source_dir=self.source)
        lines: list[str] = []
        runner = Runner(dry_run=True, echo=lines.append)
        outcome = run_setup(plan, runner, on_line=lines.append)
        self.assertEqual(len(outcome.results), len(plan.steps))
        self.assertTrue(all(result.returncode == 0 for result in outcome.results))
        self.assertTrue(any(line.startswith("$ ") for line in lines))
        self.assertTrue(any("==>" in line for line in lines))


class BinfmtTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)

    def test_read_binfmt_status(self) -> None:
        path = self.tmp / "qemu-x86_64"
        path.write_text(
            "enabled\n"
            "interpreter /usr/libexec/qemu-binfmt/x86_64-binfmt-P\n"
            "flags: OCF\n"
            "offset 0\n"
            "magic 7f454c46\n"
        )
        status = read_binfmt_status(path)
        self.assertTrue(status.registered)
        self.assertTrue(status.enabled)
        self.assertIn("x86_64-binfmt-P", status.interpreter)
        self.assertEqual(status.flags, "OCF")
        self.assertIn("enabled", status.describe())

    def test_missing_binfmt_entry(self) -> None:
        status = read_binfmt_status(self.tmp / "nope")
        self.assertFalse(status.registered)
        self.assertEqual(status.describe(), "not registered")

    def test_handler_filtering(self) -> None:
        misc = self.tmp / "misc"
        misc.mkdir()
        (misc / "status").write_text("enabled")
        (misc / "qemu-x86_64").write_text(
            "enabled\ninterpreter /usr/libexec/qemu-binfmt/x86_64-binfmt-P\n"
        )
        (misc / "box64-x86_64").write_text("enabled\ninterpreter /usr/local/bin/box64\n")
        (misc / "qemu-aarch64").write_text(
            "enabled\ninterpreter /usr/libexec/qemu-binfmt/aarch64-binfmt-P\n"
        )
        handlers = x86_binfmt_handlers(misc)
        self.assertIn("qemu-x86_64", handlers)
        self.assertIn("box64-x86_64", handlers)
        self.assertNotIn("qemu-aarch64", handlers)
        self.assertNotIn("status", handlers)
        self.assertIsInstance(handlers["qemu-x86_64"], BinfmtStatus)


if __name__ == "__main__":
    unittest.main()
