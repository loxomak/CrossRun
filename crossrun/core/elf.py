"""Minimal, dependency-free inspection of ELF headers.

CrossRun only needs a handful of facts about a payload: is it a 64-bit x86-64
ELF, is it dynamically linked, which program interpreter does it request, and
is it an AppImage (an ELF runtime with an embedded squashfs).  Parsing the
headers directly avoids relying on ``file(1)`` or the host's binfmt handlers.
"""

from __future__ import annotations

import mmap
import os
import struct
from dataclasses import dataclass
from typing import Union

ELF_MAGIC = b"\x7fELF"
ET_EXEC = 2
ET_DYN = 3
EM_386 = 3
EM_X86_64 = 62
EM_AARCH64 = 183
PT_INTERP = 3

APPIMAGE_MAGIC = (b"AI\x01", b"AI\x02")
SQUASHFS_MAGIC = b"hsqs"

_MACHINE_NAMES = {
    EM_386: "x86 (32-bit)",
    EM_X86_64: "x86-64 (AMD64)",
    EM_AARCH64: "AArch64 (ARM64)",
}


class NotAnElfFile(ValueError):
    """Raised when a file is too short, truncated or lacks the ELF magic."""


@dataclass(frozen=True)
class ElfInfo:
    """The ELF facts CrossRun cares about."""

    bits: int
    e_type: int
    machine: int
    interpreter: str | None = None

    @property
    def is_amd64(self) -> bool:
        return self.bits == 64 and self.machine == EM_X86_64

    @property
    def is_i386(self) -> bool:
        return self.bits == 32 and self.machine == EM_386

    @property
    def is_arm64(self) -> bool:
        return self.bits == 64 and self.machine == EM_AARCH64

    @property
    def is_pie(self) -> bool:
        return self.e_type == ET_DYN

    @property
    def is_static(self) -> bool:
        return self.interpreter is None

    @property
    def machine_name(self) -> str:
        return _MACHINE_NAMES.get(self.machine, f"ELF machine {self.machine}")

    @property
    def linkage(self) -> str:
        return "static" if self.is_static else "dynamic"

    def summary(self) -> str:
        details = [f"{self.machine_name}, {self.bits}-bit, {self.linkage}"]
        if self.is_pie:
            details.append("PIE")
        if self.interpreter:
            details.append(f"interpreter {self.interpreter}")
        return ", ".join(details)


def _read_exact(handle, offset: int, size: int) -> bytes:
    handle.seek(offset)
    data = handle.read(size)
    if len(data) != size:
        raise NotAnElfFile("ELF file is truncated")
    return data


def read_elf_info(path: str | os.PathLike[str], *, max_phnum: int = 512) -> ElfInfo:
    """Parse the ELF header of *path*.

    Raises :class:`NotAnElfFile` for non-ELF or malformed input, and the usual
    :class:`OSError` subclasses for unreadable paths.
    """

    with open(path, "rb") as handle:
        header = handle.read(64)
        if len(header) < 52:
            raise NotAnElfFile("file is too small to be an ELF binary")
        if header[:4] != ELF_MAGIC:
            raise NotAnElfFile("missing ELF magic bytes")

        ei_class, ei_data, ei_version = header[4], header[5], header[6]
        if ei_version != 1:
            raise NotAnElfFile("unsupported ELF version")
        if ei_data == 1:
            endian = "<"
        elif ei_data == 2:
            endian = ">"
        else:
            raise NotAnElfFile("invalid ELF byte order")
        if ei_class == 2:
            bits = 64
        elif ei_class == 1:
            bits = 32
        else:
            raise NotAnElfFile("invalid ELF class")

        e_type, e_machine = struct.unpack_from(endian + "HH", header, 16)
        if bits == 64:
            if len(header) < 64:
                raise NotAnElfFile("ELF header is truncated")
            e_phoff = struct.unpack_from(endian + "Q", header, 32)[0]
            e_phentsize, e_phnum = struct.unpack_from(endian + "HH", header, 54)
            phdr_format, phdr_size = endian + "IIQQQQQQ", 56
        else:
            e_phoff = struct.unpack_from(endian + "I", header, 28)[0]
            e_phentsize, e_phnum = struct.unpack_from(endian + "HH", header, 42)
            phdr_format, phdr_size = endian + "IIIIIIII", 32

        interpreter = _find_interpreter(
            handle,
            bits=bits,
            phoff=e_phoff,
            phentsize=e_phentsize,
            phnum=e_phnum,
            phdr_format=phdr_format,
            phdr_size=phdr_size,
            max_phnum=max_phnum,
        )

    return ElfInfo(bits=bits, e_type=e_type, machine=e_machine, interpreter=interpreter)


def _find_interpreter(
    handle,
    *,
    bits: int,
    phoff: int,
    phentsize: int,
    phnum: int,
    phdr_format: str,
    phdr_size: int,
    max_phnum: int,
) -> str | None:
    if not phoff or not phnum or phnum > max_phnum:
        return None
    if phentsize < phdr_size:
        return None

    file_size = os.fstat(handle.fileno()).st_size
    table_size = phentsize * phnum
    if phoff + table_size > file_size:
        raise NotAnElfFile("ELF program header table extends past end of file")

    table = _read_exact(handle, phoff, table_size)
    for index in range(phnum):
        entry = table[index * phentsize : index * phentsize + phdr_size]
        fields = struct.unpack(phdr_format, entry)
        if fields[0] != PT_INTERP:
            continue
        if bits == 64:
            offset, size = fields[2], fields[5]
        else:
            offset, size = fields[1], fields[4]
        if not size or size > 4096:
            return None
        if offset + size > file_size:
            raise NotAnElfFile("ELF interpreter path extends past end of file")
        raw = _read_exact(handle, offset, size)
        return raw.split(b"\x00", 1)[0].decode("utf-8", "replace") or None
    return None


def is_appimage(path: str | os.PathLike[str]) -> bool:
    """Return True when *path* looks like an AppImage (types 1 and 2)."""

    try:
        with open(path, "rb") as handle:
            head = handle.read(12)
    except OSError:
        return False
    return len(head) >= 11 and head[:4] == ELF_MAGIC and head[8:11] in APPIMAGE_MAGIC


def locate_squashfs_offset(path: str | os.PathLike[str]) -> int | None:
    """Find the embedded squashfs image of an AppImage without executing it.

    Type 2 AppImages append a squashfs filesystem to the ELF runtime.  Scan for
    the ``hsqs`` superblock magic and accept the last block that carries a
    plausible squashfs 4.0 superblock, which avoids false positives from the
    runtime's own strings.
    """

    try:
        with open(path, "rb") as handle:
            if os.fstat(handle.fileno()).st_size == 0:
                return None
            with mmap.mmap(handle.fileno(), 0, access=mmap.ACCESS_READ) as data:
                found: int | None = None
                start = 0
                while True:
                    index = data.find(SQUASHFS_MAGIC, start)
                    if index < 0:
                        break
                    if _looks_like_squashfs(data, index):
                        found = index
                    start = index + 1
                return found
    except (OSError, ValueError):
        return None


def _looks_like_squashfs(data: Union[bytes, mmap.mmap], offset: int) -> bool:
    if offset + 32 > len(data):
        return False
    block_size = int.from_bytes(data[offset + 12 : offset + 16], "little")
    major = int.from_bytes(data[offset + 28 : offset + 30], "little")
    inode_count = int.from_bytes(data[offset + 4 : offset + 8], "little")
    return (
        major == 4
        and inode_count > 0
        and 4096 <= block_size <= 1048576
        and block_size & (block_size - 1) == 0
    )


def classify(path: str | os.PathLike[str]) -> str:
    """Return a short label for *path* without raising on unreadable input."""

    try:
        info = read_elf_info(path)
    except IsADirectoryError:
        return "directory"
    except FileNotFoundError:
        return "missing"
    except (NotAnElfFile, OSError):
        return "not-elf"
    if info.is_amd64:
        return "amd64"
    if info.is_i386:
        return "i386"
    if info.is_arm64:
        return "arm64"
    return "elf-other"
