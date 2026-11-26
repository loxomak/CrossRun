"""The AMD64 translation backends CrossRun can drive.

Box64 is a userspace x86-64 dynamic recompiler and is preferred for speed.
QEMU user-mode plus Debian's ``amd64`` multiarch libraries is the compatibility
fallback.  Both are invoked explicitly; CrossRun never relies on the host's
binfmt handlers to start an application, which keeps behaviour predictable.
"""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from ..errors import MissingBackendError

BOX64 = "box64"
QEMU = "qemu"
AUTO = "auto"
BACKEND_KEYS = (BOX64, QEMU)

X86_LIB_DIR = Path("/usr/lib/x86_64-linux-gnu")
X86_LOADER = "ld-linux-x86-64.so.2"

Which = Callable[[str], str | None]


@dataclass(frozen=True)
class BackendInfo:
    key: str
    label: str
    executable: str | None
    version: str | None = None
    detail: str = ""

    @property
    def available(self) -> bool:
        return bool(self.executable)


@dataclass
class LaunchPlan:
    """A fully resolved command line for launching an AMD64 payload."""

    argv: tuple[str, ...]
    env: dict[str, str] = field(default_factory=dict)
    cwd: str | None = None
    backend: str = ""
    notes: tuple[str, ...] = ()

    def command(self) -> str:
        env_prefix = " ".join(
            f"{key}={shlex.quote(value)}" for key, value in sorted(self.env.items())
        )
        command = shlex.join(self.argv)
        return f"{env_prefix} {command}" if env_prefix else command


def _probe_version(executable: str) -> str | None:
    try:
        proc = subprocess.run(
            [executable, "--version"],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    output = (proc.stdout or proc.stderr or "").strip()
    return output.splitlines()[0].strip() if output else None


def detect_backends(
    *,
    which: Which = shutil.which,
    probe_version: bool = True,
) -> list[BackendInfo]:
    """Return one :class:`BackendInfo` per known backend, installed or not."""

    box64 = which("box64")
    qemu = which("qemu-x86_64-static") or which("qemu-x86_64")

    box64_version = _probe_version(box64) if box64 and probe_version else None
    qemu_version = _probe_version(qemu) if qemu and probe_version else None

    return [
        BackendInfo(
            key=BOX64,
            label="Box64 (dynamic recompiler)",
            executable=box64,
            version=box64_version,
            detail=box64 or "not installed - run: crossrun setup --backend box64",
        ),
        BackendInfo(
            key=QEMU,
            label="QEMU user-mode (x86-64)",
            executable=qemu,
            version=qemu_version,
            detail=qemu or "not installed - run: crossrun setup --backend qemu",
        ),
    ]


def select_backend_info(
    key: str, infos: list[BackendInfo] | None = None, *, which: Which = shutil.which
) -> BackendInfo:
    """Resolve ``auto``/``box64``/``qemu`` to an installed backend."""

    infos = detect_backends(which=which) if infos is None else infos
    if key not in (AUTO, *BACKEND_KEYS):
        raise MissingBackendError(f"unknown backend '{key}' (expected auto, box64 or qemu)")

    if key == AUTO:
        for info in infos:
            if info.available:
                return info
        raise MissingBackendError(
            "no AMD64 translation backend is installed. Run: crossrun setup"
        )

    for info in infos:
        if info.key == key:
            if not info.available:
                raise MissingBackendError(
                    f"{info.label} is not installed. Run: crossrun setup --backend {key}"
                )
            return info
    raise MissingBackendError(f"backend '{key}' is not known")


def backend_for(
    key: str,
    *,
    infos: list[BackendInfo] | None = None,
    sysroot: Path | None = None,
    which: Which = shutil.which,
) -> "Backend":
    """Instantiate the backend implementation for *key*."""

    info = select_backend_info(key, infos, which=which)
    assert info.executable is not None
    if info.key == BOX64:
        return Box64Backend(info.executable)
    return QemuBackend(info.executable, sysroot=sysroot)


def ensure_qemu_sysroot(
    base: Path,
    *,
    host_lib_dir: Path = X86_LIB_DIR,
    dry_run: bool = False,
) -> Path | None:
    """Create a tiny sysroot of symlinks to the host's amd64 libraries.

    QEMU translates absolute guest paths by prepending ``QEMU_LD_PREFIX``.  With
    Debian multiarch the x86-64 libraries live in ``/usr/lib/x86_64-linux-gnu``
    and the loader is reached through ``/lib64``.  A directory of symlinks makes
    those paths resolvable inside the prefix without copying any file.

    Returns the usable prefix, or ``None`` when no amd64 loader is installed.
    """

    loader = host_lib_dir / X86_LOADER
    if not loader.exists():
        return None
    if not dry_run:
        (base / "lib64").mkdir(parents=True, exist_ok=True)
        (base / "lib").mkdir(parents=True, exist_ok=True)
        (base / "usr" / "lib").mkdir(parents=True, exist_ok=True)
        _replace_symlink(base / "lib64" / X86_LOADER, loader)
        _replace_symlink(base / "lib" / X86_LOADER, loader)
        _replace_symlink(base / "lib" / "x86_64-linux-gnu", host_lib_dir)
        _replace_symlink(base / "usr" / "lib" / "x86_64-linux-gnu", host_lib_dir)
    return base


def _replace_symlink(link: Path, target: Path) -> None:
    if link.is_symlink():
        if os.readlink(link) == str(target):
            return
        link.unlink()
    elif link.exists():
        # Never replace real files or directories, for example an extracted
        # private sysroot, with a link to the host's libraries.
        return
    link.symlink_to(target)


def sysroot_ready(base: Path) -> bool:
    return (base / "lib64" / X86_LOADER).exists()


def sysroot_library_dirs(sysroot: Path | None) -> tuple[str, ...]:
    """x86-64 library directories inside a sysroot, for Box64's search path."""

    if sysroot is None:
        return ()
    found: list[str] = []
    for relative in (
        "usr/lib/x86_64-linux-gnu",
        "lib/x86_64-linux-gnu",
        "usr/lib64",
        "lib64",
    ):
        candidate = sysroot / relative
        if candidate.is_dir():
            found.append(str(candidate))
    return tuple(dict.fromkeys(found))


def native_x86_loader() -> Path | None:
    """Return the x86-64 loader path if the host root already resolves it.

    On Debian multiarch systems, installing ``libc6:amd64`` can make the guest
    interpreter reachable at its original absolute path.  When that is the case
    QEMU needs no ``QEMU_LD_PREFIX``, which is preferable because the prefix
    also rewrites other absolute paths used by the application.
    """

    for candidate in (
        Path("/lib64") / X86_LOADER,
        Path("/lib") / X86_LOADER,
        Path("/usr/lib64") / X86_LOADER,
    ):
        if candidate.exists():
            return candidate
    return None


class Backend:
    """Base class for a translation backend invocation strategy."""

    key = ""
    label = ""

    def __init__(self, executable: str, *, sysroot: Path | None = None) -> None:
        self.executable = executable
        self.sysroot = sysroot

    def build_argv(self, binary: str, args: tuple[str, ...] = ()) -> list[str]:
        return [self.executable, binary, *args]

    def default_env(self, libdirs: tuple[str, ...] = ()) -> dict[str, str]:
        return {}

    def warnings(self) -> tuple[str, ...]:
        return ()

    def build_plan(
        self,
        binary: str,
        args: tuple[str, ...] = (),
        *,
        cwd: str | None = None,
        extra_env: dict[str, str] | None = None,
        libdirs: tuple[str, ...] = (),
    ) -> LaunchPlan:
        env = self.default_env(libdirs)
        if extra_env:
            env.update(extra_env)
        parent = str(Path(binary).parent)
        return LaunchPlan(
            argv=tuple(self.build_argv(binary, args)),
            env=env,
            cwd=cwd or parent,
            backend=self.key,
            notes=self.warnings(),
        )

    def __str__(self) -> str:  # pragma: no cover - debug helper
        return f"{self.label} ({self.executable})"


class Box64Backend(Backend):
    key = BOX64
    label = "Box64"

    def default_env(self, libdirs: tuple[str, ...] = ()) -> dict[str, str]:
        env = {"BOX64_NOBANNER": "1"}
        if libdirs:
            existing = os.environ.get("BOX64_LD_LIBRARY_PATH", "")
            env["BOX64_LD_LIBRARY_PATH"] = ":".join(
                [part for part in (existing, *libdirs) if part]
            )
        return env


class QemuBackend(Backend):
    key = QEMU
    label = "QEMU user-mode"

    def default_env(self, libdirs: tuple[str, ...] = ()) -> dict[str, str]:
        if native_x86_loader() is not None:
            return {}
        if self.sysroot is not None and sysroot_ready(self.sysroot):
            return {"QEMU_LD_PREFIX": str(self.sysroot)}
        return {}

    def warnings(self) -> tuple[str, ...]:
        if native_x86_loader() is not None:
            return ()
        if self.sysroot is not None and sysroot_ready(self.sysroot):
            return (
                "using an isolated QEMU sysroot: dynamically linked apps run, but "
                "absolute configuration paths inside them may not resolve",
            )
        return (
            "no x86-64 runtime libraries were found; only statically linked "
            "binaries will run. Install them with: crossrun setup --with-libs",
        )
