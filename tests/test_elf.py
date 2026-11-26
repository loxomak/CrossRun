"""Tests for ELF inspection and AppImage detection."""

from __future__ import annotations

import struct
import tempfile
import unittest
from pathlib import Path

from crossrun.core.elf import (
    EM_386,
    EM_AARCH64,
    NotAnElfFile,
    classify,
    is_appimage,
    locate_squashfs_offset,
    read_elf_info,
)
from crossrun.core.selftest import SELFTEST_MESSAGE, build_selftest_binary
from tests.support import make_appimage, make_elf


class ElfParsingTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)

    def test_static_amd64_from_selftest_builder(self) -> None:
        binary = build_selftest_binary(self.tmp)
        info = read_elf_info(binary)
        self.assertTrue(info.is_amd64)
        self.assertTrue(info.is_static)
        self.assertFalse(info.is_pie)
        self.assertEqual(info.bits, 64)
        self.assertEqual(info.machine_name, "x86-64 (AMD64)")
        self.assertIn("AMD64", info.summary())
        self.assertEqual(info.linkage, "static")
        self.assertEqual(classify(binary), "amd64")
        self.assertIn(SELFTEST_MESSAGE.encode(), binary.read_bytes())

    def test_dynamic_pie_interpreter(self) -> None:
        path = make_elf(
            self.tmp,
            "dynamic",
            e_type=3,
            interpreter="/lib64/ld-linux-x86-64.so.2",
        )
        info = read_elf_info(path)
        self.assertTrue(info.is_amd64)
        self.assertTrue(info.is_pie)
        self.assertFalse(info.is_static)
        self.assertEqual(info.interpreter, "/lib64/ld-linux-x86-64.so.2")
        self.assertIn("interpreter", info.summary())

    def test_i386_and_arm64_are_not_amd64(self) -> None:
        i386 = read_elf_info(
            make_elf(self.tmp, "i386", bits=32, machine=EM_386)
        )
        self.assertTrue(i386.is_i386)
        self.assertFalse(i386.is_amd64)
        self.assertEqual(classify(self.tmp / "i386"), "i386")

        arm = make_elf(self.tmp, "arm", machine=EM_AARCH64, interpreter="/lib/ld-linux-aarch64.so.1")
        info = read_elf_info(arm)
        self.assertTrue(info.is_arm64)
        self.assertEqual(classify(arm), "arm64")

    def test_non_elf_and_missing_paths(self) -> None:
        script = self.tmp / "run.sh"
        script.write_text("#!/bin/sh\nexit 0\n")
        with self.assertRaises(NotAnElfFile):
            read_elf_info(script)
        self.assertEqual(classify(script), "not-elf")
        self.assertEqual(classify(self.tmp / "nope"), "missing")
        self.assertEqual(classify(self.tmp), "directory")

    def test_truncated_header_and_bad_program_table(self) -> None:
        empty = self.tmp / "empty"
        empty.write_bytes(b"\x7fELF")
        with self.assertRaises(NotAnElfFile):
            read_elf_info(empty)

        path = make_elf(self.tmp, "broken")
        data = bytearray(path.read_bytes())
        struct.pack_into("<Q", data, 32, 10_000)  # e_phoff
        struct.pack_into("<H", data, 54, 56)  # e_phentsize
        struct.pack_into("<H", data, 56, 1)  # e_phnum
        path.write_bytes(data)
        with self.assertRaises(NotAnElfFile):
            read_elf_info(path)


class AppImageTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)

    def test_magic_and_embedded_squashfs(self) -> None:
        base = build_selftest_binary(self.tmp)
        image, offset = make_appimage(self.tmp, base)
        self.assertTrue(is_appimage(image))
        self.assertEqual(locate_squashfs_offset(image), offset)
        self.assertEqual(classify(image), "amd64")

    def test_invalid_squashfs_superblock_is_rejected(self) -> None:
        path = self.tmp / "fake"
        path.write_bytes(b"hsqs" + b"\x00" * 32)
        self.assertIsNone(locate_squashfs_offset(path))
        self.assertFalse(is_appimage(path))

    def test_not_an_appimage(self) -> None:
        base = build_selftest_binary(self.tmp)
        self.assertFalse(is_appimage(base))


if __name__ == "__main__":
    unittest.main()
