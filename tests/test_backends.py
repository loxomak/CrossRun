"""Tests for backend selection, launch plans and the QEMU sysroot shim."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from crossrun.core.backends import (
    BOX64,
    QEMU,
    BackendInfo,
    Box64Backend,
    QemuBackend,
    backend_for,
    detect_backends,
    ensure_qemu_sysroot,
    select_backend_info,
    sysroot_library_dirs,
    sysroot_ready,
)
from crossrun.errors import MissingBackendError


def fake_infos(box64: str | None = None, qemu: str | None = None) -> list[BackendInfo]:
    return [
        BackendInfo(BOX64, "Box64", box64, "0.3.4"),
        BackendInfo(QEMU, "QEMU", qemu, "9.2"),
    ]


class DetectionTests(unittest.TestCase):
    def test_detect_backends_uses_which(self) -> None:
        mapping = {
            "box64": "/usr/local/bin/box64",
            "qemu-x86_64-static": "/usr/bin/qemu-x86_64-static",
        }
        infos = detect_backends(which=lambda name: mapping.get(name), probe_version=False)
        self.assertEqual([info.key for info in infos], [BOX64, QEMU])
        self.assertEqual(infos[0].executable, "/usr/local/bin/box64")
        self.assertEqual(infos[1].executable, "/usr/bin/qemu-x86_64-static")
        self.assertTrue(all(info.available for info in infos))

    def test_detect_backends_without_any_backend(self) -> None:
        infos = detect_backends(which=lambda name: None, probe_version=False)
        self.assertFalse(any(info.available for info in infos))
        self.assertIn("crossrun setup", infos[0].detail)

    def test_auto_prefers_box64_then_qemu(self) -> None:
        self.assertEqual(select_backend_info("auto", fake_infos("/bin/box64", "/bin/qemu")).key, BOX64)
        self.assertEqual(select_backend_info("auto", fake_infos(None, "/bin/qemu")).key, QEMU)
        with self.assertRaises(MissingBackendError):
            select_backend_info("auto", fake_infos())

    def test_explicit_missing_backend(self) -> None:
        with self.assertRaises(MissingBackendError) as caught:
            select_backend_info("box64", fake_infos(None, "/bin/qemu"))
        self.assertIn("setup --backend box64", str(caught.exception))
        with self.assertRaises(MissingBackendError):
            select_backend_info("wine", fake_infos("/bin/box64"))

    def test_backend_for_factory(self) -> None:
        box = backend_for("box64", infos=fake_infos("/bin/box64"))
        self.assertIsInstance(box, Box64Backend)
        qemu = backend_for("qemu", infos=fake_infos(None, "/bin/qemu"), sysroot=None)
        self.assertIsInstance(qemu, QemuBackend)


class PlanTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)

    def test_box64_plan(self) -> None:
        backend = Box64Backend("/usr/bin/box64")
        with patch.dict(os.environ, {"BOX64_LD_LIBRARY_PATH": ""}, clear=False):
            plan = backend.build_plan(
                "/apps/game/game",
                ("--fullscreen",),
                cwd="/apps/game",
                extra_env={"CUSTOM": "1"},
                libdirs=("/apps/game/lib",),
            )
        self.assertEqual(
            plan.argv,
            ("/usr/bin/box64", "/apps/game/game", "--fullscreen"),
        )
        self.assertEqual(plan.backend, "box64")
        self.assertEqual(plan.cwd, "/apps/game")
        self.assertEqual(plan.env["BOX64_NOBANNER"], "1")
        self.assertEqual(plan.env["BOX64_LD_LIBRARY_PATH"], "/apps/game/lib")
        self.assertEqual(plan.env["CUSTOM"], "1")
        self.assertIn("BOX64_NOBANNER=1", plan.command())

    def test_box64_without_extra_libraries(self) -> None:
        with patch.dict(os.environ, {"BOX64_LD_LIBRARY_PATH": ""}, clear=False):
            env = Box64Backend("/usr/bin/box64").default_env(())
        self.assertNotIn("BOX64_LD_LIBRARY_PATH", env)

    def test_qemu_uses_sysroot_when_loader_is_not_native(self) -> None:
        host_lib = self.tmp / "hostlib"
        host_lib.mkdir()
        (host_lib / "ld-linux-x86-64.so.2").write_text("")
        (host_lib / "libc.so.6").write_text("")
        sysroot = ensure_qemu_sysroot(self.tmp / "sysroot", host_lib_dir=host_lib)
        assert sysroot is not None
        self.assertTrue(sysroot_ready(sysroot))
        self.assertTrue((sysroot / "lib64" / "ld-linux-x86-64.so.2").exists())
        self.assertTrue((sysroot / "lib" / "x86_64-linux-gnu").is_symlink())

        backend = QemuBackend("/usr/bin/qemu-x86_64-static", sysroot=sysroot)
        with patch("crossrun.core.backends.native_x86_loader", return_value=None):
            env = backend.default_env()
            plan = backend.build_plan("/apps/tool/tool")
            warnings = backend.warnings()
        self.assertEqual(env["QEMU_LD_PREFIX"], str(sysroot))
        self.assertEqual(plan.env["QEMU_LD_PREFIX"], str(sysroot))
        self.assertTrue(any("sysroot" in note for note in warnings))

    def test_qemu_prefers_native_multiarch(self) -> None:
        loader = self.tmp / "ld-linux-x86-64.so.2"
        loader.write_text("")
        backend = QemuBackend("/usr/bin/qemu-x86_64-static", sysroot=self.tmp / "nope")
        with patch("crossrun.core.backends.native_x86_loader", return_value=loader):
            self.assertEqual(backend.default_env(), {})
            self.assertEqual(backend.warnings(), ())

    def test_qemu_without_libraries_only_runs_static_binaries(self) -> None:
        backend = QemuBackend("/usr/bin/qemu", sysroot=None)
        with patch("crossrun.core.backends.native_x86_loader", return_value=None):
            self.assertEqual(backend.default_env(), {})
            self.assertTrue(any("static" in note for note in backend.warnings()))
        self.assertIsNone(
            ensure_qemu_sysroot(self.tmp / "empty-sysroot", host_lib_dir=self.tmp)
        )

    def test_sysroot_library_dirs(self) -> None:
        sysroot = self.tmp / "sysroot"
        libdir = sysroot / "usr" / "lib" / "x86_64-linux-gnu"
        libdir.mkdir(parents=True)
        self.assertEqual(sysroot_library_dirs(sysroot), (str(libdir),))
        self.assertEqual(sysroot_library_dirs(None), ())

    def test_shim_never_replaces_real_files(self) -> None:
        base = self.tmp / "shim"
        real = base / "lib64" / "ld-linux-x86-64.so.2"
        real.parent.mkdir(parents=True)
        real.write_text("real loader")
        host_lib = self.tmp / "hostlib"
        host_lib.mkdir()
        (host_lib / "ld-linux-x86-64.so.2").write_text("host loader")
        ensure_qemu_sysroot(base, host_lib_dir=host_lib)
        self.assertFalse(real.is_symlink())
        self.assertEqual(real.read_text(), "real loader")


if __name__ == "__main__":
    unittest.main()
