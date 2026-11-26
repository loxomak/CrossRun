"""Tests for platform detection and directory helpers."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from crossrun.core.platform import (
    cache_dir,
    data_dir,
    detect_platform,
    parse_pi_model,
    private_sysroot_dir,
    qemu_shim_dir,
    terminal_command,
)


class PiModelTests(unittest.TestCase):
    def test_generations(self) -> None:
        cases = {
            "Raspberry Pi 5 Model B Rev 1.1": 5,
            "Raspberry Pi 4 Model B Rev 1.4": 4,
            "Raspberry Pi 400 Rev 1.0": 4,
            "Raspberry Pi 500 Rev 1.0": 5,
            "Raspberry Pi Compute Module 4 Rev 1.0": 4,
            "Raspberry Pi 3 Model B Plus Rev 1.3": 3,
            "Raspberry Pi Zero 2 W Rev 1.0": 3,
            "Raspberry Pi Zero Rev 1.3": 1,
        }
        for raw, generation in cases.items():
            model, parsed = parse_pi_model(raw)
            self.assertEqual(parsed, generation, raw)
            self.assertIn("Raspberry Pi", model)
        self.assertEqual(parse_pi_model("generic virtual machine")[1], None)
        self.assertEqual(parse_pi_model("")[1], None)

    def test_detect_platform_from_fake_root(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "etc").mkdir(parents=True)
            (root / "etc" / "os-release").write_text(
                'NAME="Raspberry Pi OS"\nID=debian\nVERSION_ID="13"\n'
            )
            (root / "proc").mkdir()
            (root / "proc" / "meminfo").write_text("MemTotal:  4194304 kB\n")
            (root / "proc" / "device-tree").mkdir()
            (root / "proc" / "device-tree" / "model").write_text(
                "Raspberry Pi 5 Model B Rev 1.0\x00"
            )
            platform = detect_platform(root=root, machine="aarch64", system="linux")
        self.assertEqual(platform.machine, "aarch64")
        self.assertTrue(platform.is_arm64)
        self.assertEqual(platform.distro_id, "debian")
        self.assertEqual(platform.distro_version, "13")
        self.assertEqual(platform.ram_mb, 4096)
        self.assertTrue(platform.is_raspberry_pi)
        self.assertEqual(platform.pi_generation, 5)
        self.assertIn("Raspberry Pi 5", platform.describe())

    def test_data_directories(self) -> None:
        env = {
            "HOME": "/home/user",
            "XDG_DATA_HOME": "/data",
            "XDG_CACHE_HOME": "/cache",
        }
        self.assertEqual(data_dir(env), Path("/data/crossrun"))
        self.assertEqual(cache_dir(env), Path("/cache/crossrun"))
        self.assertEqual(private_sysroot_dir(env), Path("/data/crossrun/sysroot"))
        self.assertEqual(qemu_shim_dir(env), Path("/data/crossrun/qemu-prefix"))
        fallback = {"HOME": "/home/user"}
        self.assertEqual(data_dir(fallback), Path("/home/user/.local/share/crossrun"))
        self.assertEqual(cache_dir(fallback), Path("/home/user/.cache/crossrun"))

    def test_terminal_commands(self) -> None:
        self.assertEqual(
            terminal_command("/usr/bin/lxterminal", "echo hi"),
            ["/usr/bin/lxterminal", "-e", "bash", "-lc", "echo hi"],
        )
        self.assertEqual(
            terminal_command("/usr/bin/gnome-terminal", "echo hi"),
            ["/usr/bin/gnome-terminal", "--", "bash", "-lc", "echo hi"],
        )


if __name__ == "__main__":
    unittest.main()
