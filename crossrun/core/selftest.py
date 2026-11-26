"""A hand-assembled static AMD64 program that proves a backend works.

CrossRun can be validated without downloading a test binary: this module emits a
minimal x86-64 ELF that calls ``write(2)`` and ``exit(2)`` directly, then runs it
through the selected backend.  Static syscalls mean no guest libraries are
required, so the check isolates translation itself.
"""

from __future__ import annotations

import os
import shutil
import struct
import tempfile
from dataclasses import dataclass
from pathlib import Path

from ..errors import CrossRunError, MissingBackendError
from .apps import build_plan, capture_plan
from .backends import AUTO, LaunchPlan
from .elf import read_elf_info
from .registry import AppEntry

SELFTEST_MESSAGE = "crossrun: AMD64 payload executed successfully"
CODE_OFFSET = 0x1000
BASE_ADDRESS = 0x400000


def _build_code(exit_code: int) -> bytes:
    message = (SELFTEST_MESSAGE + "\n").encode()
    code = b"\xb8\x01\x00\x00\x00"  # mov eax, 1        (write)
    code += b"\xbf\x01\x00\x00\x00"  # mov edi, 1        (stdout)
    lea_offset = len(code)
    code += b"\x48\x8d\x35\x00\x00\x00\x00"  # lea rsi, [rip+disp]
    code += b"\xba" + struct.pack("<I", len(message))  # mov edx, len
    code += b"\x0f\x05"  # syscall
    code += b"\xb8\x3c\x00\x00\x00"  # mov eax, 60       (exit)
    code += b"\xbf" + struct.pack("<I", exit_code)  # mov edi, exit_code
    code += b"\x0f\x05"  # syscall
    displacement = len(code) - (lea_offset + 7)
    code = code[: lea_offset + 3] + struct.pack("<i", displacement) + code[lea_offset + 7 :]
    return code + message


def build_selftest_binary(directory: str | os.PathLike[str], *, exit_code: int = 0) -> Path:
    """Write an executable, statically linked x86-64 ELF into *directory*."""

    code = _build_code(exit_code)
    file_size = CODE_OFFSET + len(code)
    ident = b"\x7fELF" + bytes([2, 1, 1, 0]) + b"\x00" * 8
    header = struct.pack(
        "<16sHHIQQQIHHHHHH",
        ident,
        2,  # ET_EXEC
        62,  # EM_X86_64
        1,
        BASE_ADDRESS + CODE_OFFSET,
        64,  # e_phoff
        0,
        0,
        64,  # e_ehsize
        56,  # e_phentsize
        1,  # e_phnum
        64,
        0,
        0,
    )
    program_header = struct.pack(
        "<IIQQQQQQ",
        1,  # PT_LOAD
        5,  # R+X
        0,
        BASE_ADDRESS,
        BASE_ADDRESS,
        file_size,
        file_size,
        CODE_OFFSET,
    )
    blob = (header + program_header).ljust(CODE_OFFSET, b"\x00") + code
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    binary = directory / "crossrun-selftest-amd64"
    binary.write_bytes(blob)
    binary.chmod(0o755)

    info = read_elf_info(binary)
    if not (info.is_amd64 and info.is_static):
        raise CrossRunError("internal error: generated self-test binary is malformed")
    return binary


@dataclass
class SelftestResult:
    ok: bool
    backend: str
    message: str
    returncode: int | None = None
    plan: LaunchPlan | None = None

    def render(self) -> str:
        status = "PASS" if self.ok else "FAIL"
        lines = [f"[{status}] backend: {self.backend}"]
        if self.plan is not None:
            lines.append(f"command: {self.plan.command()}")
        if self.returncode is not None:
            lines.append(f"exit code: {self.returncode}")
        lines.append(self.message.strip())
        return "\n".join(line for line in lines if line)


def run_selftest(
    *,
    backend: str = AUTO,
    infos=None,
    data_root: str | os.PathLike[str] | None = None,
    which=shutil.which,
    timeout: float = 60.0,
) -> SelftestResult:
    """Run the built-in AMD64 payload through the chosen backend."""

    workspace = Path(tempfile.mkdtemp(prefix="crossrun-selftest-"))
    try:
        binary = build_selftest_binary(workspace)
        entry = AppEntry(
            id="selftest",
            name="CrossRun self-test",
            kind="elf",
            source=str(binary),
            path=str(binary),
            workdir=str(workspace),
            backend=backend,
        )
        try:
            plan = build_plan(
                entry,
                backend=backend,
                infos=infos,
                data_root=data_root,
                which=which,
            )
            process = capture_plan(plan, timeout=timeout)
        except MissingBackendError as exc:
            return SelftestResult(False, backend, str(exc))
        except CrossRunError as exc:
            return SelftestResult(False, backend, str(exc))

        output = (process.stdout or "") + (process.stderr or "")
        ok = process.returncode == 0 and SELFTEST_MESSAGE in (process.stdout or "")
        if not ok and not output.strip():
            output = "(the backend produced no output)"
        return SelftestResult(ok, plan.backend, output, process.returncode, plan)
    finally:
        shutil.rmtree(workspace, ignore_errors=True)
