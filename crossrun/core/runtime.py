"""Rootless x86-64 runtime sysroot assembled from Debian packages.

Installing amd64 packages with dpkg on Raspberry Pi OS can fail: the Pi's arm64
libc carries a ``+rpt`` version suffix while the amd64 libc comes straight from
Debian, and libc requires identical versions across architectures.  CrossRun
therefore downloads the amd64 ``.deb`` files and unpacks them into a private
sysroot.  Nothing is installed system-wide and no arm64 package is touched.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from ..errors import SetupError
from .backends import X86_LOADER, sysroot_ready
from .platform import cache_dir
from .runner import Command, LineHandler, Runner

BASE_PACKAGES = ("libc6", "libstdc++6", "libgcc-s1", "zlib1g")


def packages_to_install(
    requested: tuple[str, ...] = BASE_PACKAGES,
    *,
    sysroot_ready_now: bool,
) -> tuple[str, ...]:
    """Always include the base runtime when the sysroot has no loader yet."""

    requested = tuple(dict.fromkeys(requested))
    if sysroot_ready_now or set(BASE_PACKAGES).issubset(requested):
        return requested
    return tuple(dict.fromkeys((*BASE_PACKAGES, *requested)))


@dataclass
class RuntimeResult:
    dest: Path
    packages: tuple[str, ...]
    debs: list[Path] = field(default_factory=list)
    dry_run: bool = False

    @property
    def ready(self) -> bool:
        return self.dry_run or sysroot_ready(self.dest)


def install_runtime_libraries(
    dest: str | os.PathLike[str],
    *,
    runner: Runner,
    packages: tuple[str, ...] = BASE_PACKAGES,
    download_dir: str | os.PathLike[str] | None = None,
    apt_get: str = "apt-get",
    dpkg_deb: str = "dpkg-deb",
    on_line: LineHandler | None = None,
) -> RuntimeResult:
    """Download ``<package>:amd64`` and unpack it into *dest*."""

    destination = Path(dest)
    download = (
        Path(download_dir) if download_dir is not None else cache_dir() / "debs"
    )
    packages = tuple(dict.fromkeys(packages))

    if runner.dry_run:
        for package in packages:
            runner.run(
                Command((apt_get, "download", f"{package}:amd64"), cwd=str(download)),
                on_line=on_line,
            )
        for package in packages:
            runner.run(
                Command(
                    (
                        dpkg_deb,
                        "-x",
                        str(download / f"{package}_amd64.deb"),
                        str(destination),
                    )
                ),
                on_line=on_line,
            )
        return RuntimeResult(destination, packages, dry_run=True)

    download.mkdir(parents=True, exist_ok=True)
    for package in packages:
        for stale in download.glob(f"{package}_*_amd64.deb"):
            stale.unlink()

    debs: list[Path] = []
    for package in packages:
        if on_line is not None:
            on_line(f"Downloading {package}:amd64")
        result = runner.run(
            Command((apt_get, "download", f"{package}:amd64"), cwd=str(download)),
            check=False,
            on_line=on_line,
        )
        if not result.ok:
            raise SetupError(
                f"could not download {package}:amd64 (exit code {result.returncode}). "
                "Make sure the amd64 package lists are available: sudo dpkg "
                "--add-architecture amd64 && sudo apt-get update"
            )
        matches = sorted(download.glob(f"{package}_*_amd64.deb"))
        if not matches:
            raise SetupError(
                f"apt-get download {package}:amd64 produced no .deb file"
            )
        debs.append(matches[-1])

    destination.mkdir(parents=True, exist_ok=True)
    for deb in debs:
        runner.run(
            Command((dpkg_deb, "-x", str(deb), str(destination))),
            on_line=on_line,
        )

    _link_loader(destination)
    if not sysroot_ready(destination):
        raise SetupError(
            f"the private x86-64 sysroot at {destination} has no {X86_LOADER}"
        )
    return RuntimeResult(destination, packages, debs)


def _link_loader(destination: Path) -> None:
    """Make the guest loader reachable at /lib64 inside the sysroot."""

    candidates = (
        destination / "usr" / "lib" / "x86_64-linux-gnu" / X86_LOADER,
        destination / "usr" / "lib64" / X86_LOADER,
        destination / "lib" / "x86_64-linux-gnu" / X86_LOADER,
    )
    loader = next((path for path in candidates if path.exists()), None)
    if loader is None:
        return

    lib64 = destination / "lib64"
    lib64.mkdir(parents=True, exist_ok=True)
    link = lib64 / X86_LOADER
    if not link.exists() and not link.is_symlink():
        link.symlink_to(os.path.relpath(loader, lib64))

    lib_multiarch = destination / "lib" / "x86_64-linux-gnu"
    if not lib_multiarch.exists() and not lib_multiarch.is_symlink():
        target = destination / "usr" / "lib" / "x86_64-linux-gnu"
        if target.is_dir():
            (destination / "lib").mkdir(parents=True, exist_ok=True)
            lib_multiarch.symlink_to(os.path.relpath(target, lib_multiarch.parent))
