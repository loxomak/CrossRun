"""Helpers that build synthetic ELF/AppImage files for the test suite."""

from __future__ import annotations

import struct
from pathlib import Path

EM_386 = 3
EM_X86_64 = 62
EM_AARCH64 = 183
ELF_MAGIC = b"\x7fELF"


def make_elf(
    directory: str | Path,
    name: str = "program",
    *,
    bits: int = 64,
    machine: int = EM_X86_64,
    e_type: int = 2,
    interpreter: str | None = None,
    executable: bool = False,
) -> Path:
    """Write a structurally valid ELF with optional PT_INTERP."""

    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name

    if bits == 64:
        ehsize, phentsize = 64, 56
        header_format = "<16sHHIQQQIHHHHHH"
        phdr_format = "<IIQQQQQQ"
        ident = ELF_MAGIC + bytes([2, 1, 1, 0]) + b"\x00" * 8
    elif bits == 32:
        ehsize, phentsize = 52, 32
        header_format = "<16sHHIIIIIHHHHHH"
        phdr_format = "<IIIIIIII"
        ident = ELF_MAGIC + bytes([1, 1, 1, 0]) + b"\x00" * 8
    else:
        raise ValueError("bits must be 32 or 64")

    count = 2 if interpreter else 1
    interp_bytes = interpreter.encode() + b"\x00" if interpreter else b""
    interp_offset = ehsize + count * phentsize
    file_size = interp_offset + len(interp_bytes)
    base = 0x400000

    if bits == 64:
        program_headers = [
            struct.pack(
                "<IIQQQQQQ",
                1,  # PT_LOAD
                5,
                0,
                base,
                base,
                file_size,
                file_size,
                0x1000,
            )
        ]
        if interpreter:
            program_headers.append(
                struct.pack(
                    "<IIQQQQQQ",
                    3,  # PT_INTERP
                    4,
                    interp_offset,
                    0,
                    0,
                    len(interp_bytes),
                    len(interp_bytes),
                    1,
                )
            )
    else:
        program_headers = [
            struct.pack(
                "<IIIIIIII",
                1,
                0,
                base,
                base,
                file_size,
                file_size,
                5,
                0x1000,
            )
        ]
        if interpreter:
            program_headers.append(
                struct.pack(
                    "<IIIIIIII",
                    3,
                    interp_offset,
                    0,
                    0,
                    len(interp_bytes),
                    len(interp_bytes),
                    4,
                    1,
                )
            )

    header = struct.pack(
        header_format,
        ident,
        e_type,
        machine,
        1,
        base + interp_offset,
        ehsize,
        0,
        0,
        ehsize,
        phentsize,
        count,
        0,
        0,
        0,
    )
    blob = header + b"".join(program_headers) + interp_bytes
    assert len(blob) == file_size
    path.write_bytes(blob)
    if executable:
        path.chmod(0o755)
    return path


def make_appimage(
    directory: str | Path,
    base_elf: Path,
    *,
    name: str = "tool.AppImage",
) -> tuple[Path, int]:
    """Append a plausible squashfs 4 superblock to an AMD64 ELF."""

    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    elf = bytearray(base_elf.read_bytes())
    elf[8:11] = b"AI\x02"
    padding = b"\x00" * 32
    offset = len(elf) + len(padding)
    superblock = bytearray(96)
    superblock[0:4] = b"hsqs"
    struct.pack_into("<I", superblock, 4, 42)  # inode count
    struct.pack_into("<I", superblock, 8, 0)  # mkfs time
    struct.pack_into("<I", superblock, 12, 4096)  # block size
    struct.pack_into("<H", superblock, 28, 4)  # squashfs major
    struct.pack_into("<H", superblock, 30, 0)
    path.write_bytes(bytes(elf) + padding + bytes(superblock) + b"\x00" * 64)
    return path, offset


def make_fake_unsquashfs(directory: Path, payload: Path) -> Path:
    """Create a stand-in for unsquashfs that populates a tiny AppDir."""

    directory.mkdir(parents=True, exist_ok=True)
    script = directory / "unsquashfs"
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
        '[ -n "$dest" ] || exit 2\n'
        'mkdir -p "$dest/usr/bin" "$dest/lib"\n'
        f'cp "{payload}" "$dest/AppRun"\n'
        'chmod 755 "$dest/AppRun"\n'
        'echo fake > "$dest/usr/bin/helper"\n'
        "exit 0\n"
    )
    script.chmod(0o755)
    return script
