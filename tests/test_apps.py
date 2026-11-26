"""Tests for importing payloads and building launch plans."""

from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path

from crossrun.core.apps import (
    build_plan,
    find_app_executable,
    import_app,
    library_dirs,
)
from crossrun.core.backends import BackendInfo
from crossrun.core.elf import EM_AARCH64
from crossrun.core.registry import AppEntry, Registry
from crossrun.core.runner import Runner
from crossrun.core.selftest import build_selftest_binary
from crossrun.errors import (
    AppExistsError,
    ExtractionError,
    MissingBackendError,
    UnsupportedPayloadError,
)
from tests.support import make_appimage, make_elf, make_fake_unsquashfs

SILENT = Runner(echo=lambda line: None)


class ImportTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.binary = build_selftest_binary(self.tmp / "payload")
        self.registry = Registry(self.tmp / "data" / "apps.json")
        self.data_root = self.tmp / "data" / "apps"

    def test_import_elf(self) -> None:
        result = import_app(
            self.binary, self.registry, data_root=self.data_root, runner=SILENT
        )
        entry = result.entry
        self.assertEqual(entry.kind, "elf")
        self.assertEqual(entry.path, str(self.binary))
        self.assertEqual(entry.workdir, str(self.binary.parent))
        self.assertEqual(entry.source, str(self.binary.resolve()))
        self.assertEqual(entry.backend, "auto")
        self.assertEqual(self.registry.get(entry.name).id, entry.id)

    def test_duplicate_source_is_rejected(self) -> None:
        import_app(self.binary, self.registry, data_root=self.data_root)
        with self.assertRaises(AppExistsError):
            import_app(self.binary, self.registry, data_root=self.data_root)

    def test_duplicate_names_get_suffix(self) -> None:
        copy = self.tmp / "other-name"
        shutil.copy2(self.binary, copy)
        first = import_app(self.binary, self.registry, data_root=self.data_root)
        second = import_app(
            copy, self.registry, name=first.entry.name, data_root=self.data_root
        )
        self.assertEqual(second.entry.name, first.entry.name + " (2)")

    def test_rejects_non_amd64_payloads(self) -> None:
        arm = make_elf(self.tmp / "arm", machine=EM_AARCH64)
        with self.assertRaises(UnsupportedPayloadError) as caught:
            import_app(arm, self.registry, data_root=self.data_root)
        self.assertIn("ARM64", str(caught.exception))

        i386 = make_elf(self.tmp / "i386", bits=32, machine=3)
        with self.assertRaises(UnsupportedPayloadError) as caught:
            import_app(i386, self.registry, data_root=self.data_root)
        self.assertIn("Box86", str(caught.exception))

    def test_import_directory_and_entry_override(self) -> None:
        game = self.tmp / "Game"
        game.mkdir()
        shutil.copy2(self.binary, game / "launcher")
        shutil.copy2(self.binary, game / "helper")
        (game / "launcher").chmod(0o755)
        (game / "helper").chmod(0o755)

        with self.assertRaises(UnsupportedPayloadError):
            find_app_executable(game)
        chosen = find_app_executable(game, entry="helper")
        self.assertEqual(chosen, (game / "helper").resolve())

        result = import_app(
            game,
            self.registry,
            name="Game",
            entry="helper",
            data_root=self.data_root,
        )
        self.assertEqual(result.entry.kind, "directory")
        self.assertEqual(result.entry.workdir, str(game.resolve()))
        self.assertTrue(result.entry.path.endswith("helper"))

    def test_directory_with_single_binary(self) -> None:
        folder = self.tmp / "Solo"
        folder.mkdir()
        shutil.copy2(self.binary, folder / "solo")
        (folder / "solo").chmod(0o755)
        result = import_app(folder, self.registry, data_root=self.data_root)
        self.assertEqual(result.entry.kind, "directory")
        self.assertTrue(result.entry.path.endswith("solo"))

    def test_appimage_is_extracted_with_unsquashfs(self) -> None:
        image, _offset = make_appimage(self.tmp / "dist", self.binary)
        script = make_fake_unsquashfs(self.tmp / "bin", self.binary)
        which = lambda name: str(script) if name == "unsquashfs" else None
        result = import_app(
            image,
            self.registry,
            data_root=self.data_root,
            runner=SILENT,
            which=which,
        )
        entry = result.entry
        self.assertEqual(entry.kind, "appimage")
        self.assertTrue(Path(entry.path).is_file())
        self.assertTrue(Path(entry.workdir).is_dir())
        self.assertTrue(entry.path.endswith("AppRun"))
        self.assertEqual(entry.source, str(image.resolve()))
        self.assertTrue(any("AppRun" in warning for warning in result.warnings))

    def test_appimage_requires_unsquashfs(self) -> None:
        image, _offset = make_appimage(self.tmp / "dist", self.binary)
        with self.assertRaises(ExtractionError) as caught:
            import_app(
                image,
                self.registry,
                data_root=self.data_root,
                which=lambda name: None,
            )
        self.assertIn("squashfs-tools", str(caught.exception))

    def test_appimage_without_squashfs_payload(self) -> None:
        fake = self.tmp / "bad.AppImage"
        blob = bytearray(self.binary.read_bytes())
        blob[8:11] = b"AI\x02"
        fake.write_bytes(bytes(blob))
        with self.assertRaises(ExtractionError) as caught:
            import_app(fake, self.registry, data_root=self.data_root)
        self.assertIn("type 2", str(caught.exception))

    def test_name_matching_ignores_parent_directory(self) -> None:
        root = self.tmp / "appimagetool-abc123"
        (root / "usr" / "bin").mkdir(parents=True)
        for name in ("appimagetool", "othertool"):
            target = root / "usr" / "bin" / name
            shutil.copy2(self.binary, target)
            target.chmod(0o755)
        chosen = find_app_executable(root, app_name="AppImageTool")
        self.assertEqual(chosen, (root / "usr" / "bin" / "appimagetool").resolve())

    def test_failed_appimage_resolution_cleans_up(self) -> None:
        image, _offset = make_appimage(self.tmp / "dist", self.binary)
        bin_dir = self.tmp / "bin-fail"
        bin_dir.mkdir()
        script = bin_dir / "unsquashfs"
        script.write_text(
            "#!/bin/sh\n"
            'dest=""\n'
            'while [ "$#" -gt 0 ]; do\n'
            '  case "$1" in\n'
            '    -d) dest="$2"; shift 2 ;;\n'
            "    -o|-f) shift 2 ;;\n"
            "    *) shift ;;\n"
            "  esac\n"
            "done\n"
            'mkdir -p "$dest/usr/bin"\n'
            'printf "#!/bin/sh\\nexit 0\\n" > "$dest/AppRun"\n'
            'chmod 755 "$dest/AppRun"\n'
            f'cp "{self.binary}" "$dest/usr/bin/one"\n'
            f'cp "{self.binary}" "$dest/usr/bin/two"\n'
            'chmod 755 "$dest/usr/bin/one" "$dest/usr/bin/two"\n'
            "exit 0\n"
        )
        script.chmod(0o755)
        with self.assertRaises(UnsupportedPayloadError):
            import_app(
                image,
                self.registry,
                data_root=self.data_root,
                runner=SILENT,
                which=lambda name: str(script),
            )
        self.assertFalse(any(self.data_root.glob("*")))


class LaunchPlanTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.binary = build_selftest_binary(self.tmp / "payload")
        self.entry = AppEntry(
            id="demo-aaa111",
            name="Demo",
            kind="elf",
            source=str(self.binary),
            path=str(self.binary),
            workdir=str(self.binary.parent),
            backend="box64",
            args=["--default"],
        )
        self.infos = [BackendInfo("box64", "Box64", "/opt/box64", "test")]

    def test_plan_arguments_environment_and_cwd(self) -> None:
        self.entry.env = {"APP_VAR": "yes"}
        plan = build_plan(
            self.entry,
            infos=self.infos,
            extra_args=("--extra",),
            sysroot=self.tmp / "no-sysroot",
            which=lambda name: None,
        )
        self.assertEqual(
            plan.argv, ("/opt/box64", str(self.binary), "--default", "--extra")
        )
        self.assertEqual(plan.cwd, str(self.binary.parent))
        self.assertEqual(plan.backend, "box64")
        self.assertEqual(plan.env["APP_VAR"], "yes")
        self.assertEqual(plan.env["BOX64_NOBANNER"], "1")

    def test_auto_without_backend_raises(self) -> None:
        entry = AppEntry(
            id="x",
            name="X",
            kind="elf",
            source=str(self.binary),
            path=str(self.binary),
            backend="auto",
        )
        unavailable = [
            BackendInfo("box64", "Box64", None),
            BackendInfo("qemu", "QEMU", None),
        ]
        with self.assertRaises(MissingBackendError):
            build_plan(
                entry,
                infos=unavailable,
                sysroot=self.tmp / "no-sysroot",
                which=lambda name: None,
            )

    def test_library_dirs(self) -> None:
        root = self.tmp / "AppDir"
        (root / "lib").mkdir(parents=True)
        (root / "usr" / "lib" / "x86_64-linux-gnu").mkdir(parents=True)
        entry = AppEntry(
            id="x",
            name="X",
            kind="appimage",
            source="x",
            path=str(root / "AppRun"),
            workdir=str(root),
        )
        dirs = library_dirs(entry)
        self.assertIn(str(root / "lib"), dirs)
        self.assertIn(str(root / "usr" / "lib" / "x86_64-linux-gnu"), dirs)

    def test_box64_receives_private_sysroot_libraries(self) -> None:
        sysroot = self.tmp / "sysroot"
        libdir = sysroot / "usr" / "lib" / "x86_64-linux-gnu"
        libdir.mkdir(parents=True)
        plan = build_plan(
            self.entry,
            infos=self.infos,
            sysroot=sysroot,
            which=lambda name: None,
        )
        search_path = plan.env.get("BOX64_LD_LIBRARY_PATH", "")
        self.assertIn(str(libdir), search_path.split(":"))


if __name__ == "__main__":
    unittest.main()
