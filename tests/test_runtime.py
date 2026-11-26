"""Tests for the rootless amd64 runtime sysroot."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from crossrun.core.backends import sysroot_ready
from crossrun.core.runner import Runner
from crossrun.core.runtime import (
    BASE_PACKAGES,
    install_runtime_libraries,
    packages_to_install,
)
from crossrun.errors import SetupError

FAKE_APT = """#!/bin/sh
[ "$1" = "download" ] || exit 2
pkg="${2%%:*}"
: > "${pkg}_fake_amd64.deb"
echo "downloaded $pkg"
"""

FAKE_DPKG = """#!/bin/sh
[ "$1" = "-x" ] || exit 2
mkdir -p "$3/usr/lib/x86_64-linux-gnu" "$3/usr/lib64"
: > "$3/usr/lib/x86_64-linux-gnu/ld-linux-x86-64.so.2"
: > "$3/usr/lib/x86_64-linux-gnu/libc.so.6"
: > "$3/usr/lib64/ld-linux-x86-64.so.2"
echo "unpacked"
"""


class RuntimeTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.dest = self.tmp / "sysroot"
        self.download = self.tmp / "debs"
        self.apt = self._script("apt-get", FAKE_APT)
        self.dpkg = self._script("dpkg-deb", FAKE_DPKG)

    def _script(self, name: str, body: str) -> Path:
        path = self.tmp / name
        path.write_text(body)
        path.chmod(0o755)
        return path

    def install(self, *, runner: Runner | None = None, **kwargs):
        options = {
            "download_dir": self.download,
            "apt_get": str(self.apt),
            "dpkg_deb": str(self.dpkg),
        }
        options.update(kwargs)
        return install_runtime_libraries(
            self.dest,
            runner=runner or Runner(echo=lambda line: None),
            **options,
        )

    def test_downloads_packages_and_links_the_loader(self) -> None:
        result = self.install()
        self.assertTrue(result.ready)
        self.assertTrue(sysroot_ready(self.dest))
        self.assertEqual(result.packages, BASE_PACKAGES)
        self.assertEqual(len(result.debs), len(BASE_PACKAGES))
        self.assertTrue(
            (self.dest / "lib64" / "ld-linux-x86-64.so.2").is_symlink()
        )
        self.assertTrue(
            (self.dest / "lib" / "x86_64-linux-gnu").is_symlink()
        )
        self.assertTrue((self.dest / "usr" / "lib" / "x86_64-linux-gnu" / "libc.so.6").exists())

    def test_dry_run_prints_commands_without_touching_disk(self) -> None:
        lines: list[str] = []
        runner = Runner(dry_run=True, echo=lines.append)
        result = self.install(runner=runner, packages=("libc6", "zlib1g"))
        self.assertTrue(result.dry_run)
        self.assertFalse(self.dest.exists())
        self.assertEqual(result.debs, [])
        commands = [line for line in lines if line.startswith("$ ")]
        self.assertEqual(len(commands), 4)  # 2 downloads + 2 extractions
        self.assertTrue(any("download" in command for command in commands))

    def test_download_failure_raises(self) -> None:
        failing = self._script("apt-fail", "#!/bin/sh\necho 'no candidate' >&2\nexit 3\n")
        with self.assertRaises(SetupError) as caught:
            self.install(apt_get=str(failing))
        self.assertIn("could not download", str(caught.exception))

    def test_missing_deb_file_raises(self) -> None:
        silent = self._script("apt-silent", "#!/bin/sh\nexit 0\n")
        with self.assertRaises(SetupError) as caught:
            self.install(apt_get=str(silent), packages=("libc6",))
        self.assertIn("produced no .deb", str(caught.exception))

    def test_custom_package_set(self) -> None:
        result = self.install(packages=("libc6",))
        self.assertEqual(result.packages, ("libc6",))
        self.assertEqual(len(result.debs), 1)

    def test_extra_packages_bootstrap_base_until_sysroot_is_ready(self) -> None:
        self.assertEqual(
            packages_to_install(("libgl1",), sysroot_ready_now=False),
            (*BASE_PACKAGES, "libgl1"),
        )
        self.assertEqual(
            packages_to_install(("libgl1",), sysroot_ready_now=True), ("libgl1",)
        )
        self.assertEqual(
            packages_to_install((), sysroot_ready_now=False), BASE_PACKAGES
        )
        self.assertEqual(
            packages_to_install(
                ("libgl1", "libc6"), sysroot_ready_now=False
            ),
            (*BASE_PACKAGES, "libgl1"),
        )


if __name__ == "__main__":
    unittest.main()
