"""Environment diagnostics shown by ``crossrun doctor`` and the GTK UI."""

from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

from ..errors import RegistryError
from .backends import X86_LOADER, detect_backends, native_x86_loader
from .installer import BinfmtStatus, x86_binfmt_handlers
from .platform import (
    HostPlatform,
    detect_platform,
    private_sysroot_dir,
    registry_path,
)
from .registry import Registry


class Status(str, Enum):
    OK = "ok"
    WARN = "warn"
    FAIL = "fail"
    INFO = "info"


@dataclass(frozen=True)
class Check:
    name: str
    status: Status
    detail: str
    hint: str = ""

    def as_dict(self) -> dict:
        return {
            "name": self.name,
            "status": self.status.value,
            "detail": self.detail,
            "hint": self.hint,
        }


@dataclass
class DoctorReport:
    platform: HostPlatform
    checks: list[Check] = field(default_factory=list)

    @property
    def failures(self) -> list[Check]:
        return [check for check in self.checks if check.status is Status.FAIL]

    @property
    def warnings(self) -> list[Check]:
        return [check for check in self.checks if check.status is Status.WARN]

    @property
    def healthy(self) -> bool:
        return not self.failures

    def as_dict(self) -> dict:
        return {
            "host": {
                "machine": self.platform.machine,
                "distro": self.platform.distro_label,
                "model": self.platform.model,
                "ram_mb": self.platform.ram_mb,
            },
            "healthy": self.healthy,
            "checks": [check.as_dict() for check in self.checks],
        }

    def render(self) -> str:
        lines = ["CrossRun doctor", f"Host: {self.platform.describe()}", ""]
        for check in self.checks:
            lines.append(f"[{check.status.value:^4}] {check.name}: {check.detail}")
            if check.hint:
                lines.append(f"       hint: {check.hint}")
        hints = [check.hint for check in self.checks if check.hint]
        if hints:
            lines.append("")
            lines.append("Suggestions:")
            lines.extend(f"  - {hint}" for hint in dict.fromkeys(hints))
        counts = {
            status: sum(1 for check in self.checks if check.status is status)
            for status in Status
        }
        lines.append("")
        lines.append(
            f"Summary: {counts[Status.OK]} ok, {counts[Status.WARN]} warnings, "
            f"{counts[Status.FAIL]} failures, {counts[Status.INFO]} info"
        )
        return "\n".join(lines)


def _foreign_architectures() -> set[str]:
    try:
        proc = subprocess.run(
            ["dpkg", "--print-foreign-architectures"],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return set()
    return {line.strip() for line in proc.stdout.splitlines() if line.strip()}


def run_doctor(
    *,
    platform: HostPlatform | None = None,
    infos=None,
    binfmt_handlers: dict[str, BinfmtStatus] | None = None,
    loader: Path | None = None,
    sysroot: Path | None = None,
    which=shutil.which,
    registry: Registry | None = None,
) -> DoctorReport:
    """Collect environment facts without changing anything."""

    host = platform or detect_platform()
    checks: list[Check] = []

    if host.is_arm64 and host.is_64bit_python:
        checks.append(Check("Host architecture", Status.OK, f"{host.machine}, 64-bit"))
    else:
        checks.append(
            Check(
                "Host architecture",
                Status.FAIL,
                f"{host.machine} (64-bit Python: {host.is_64bit_python})",
                "CrossRun needs a 64-bit ARM OS (aarch64); reinstall Raspberry Pi OS 64-bit.",
            )
        )

    checks.append(Check("Operating system", Status.INFO, host.distro_label or host.system))
    hardware = host.model or host.machine
    checks.append(
        Check(
            "Hardware",
            Status.INFO,
            f"{hardware}, {host.ram_mb} MB RAM" if host.ram_mb else hardware,
        )
    )

    available = detect_backends(which=which) if infos is None else infos
    installed = [info for info in available if info.available]
    if installed:
        detail = "; ".join(
            f"{info.label}: {info.version or info.executable}" for info in installed
        )
        checks.append(Check("Translation backends", Status.OK, detail))
    else:
        checks.append(
            Check(
                "Translation backends",
                Status.FAIL,
                "no backend installed",
                "Run: crossrun setup --backend both --with-libs",
            )
        )

    handlers = x86_binfmt_handlers() if binfmt_handlers is None else binfmt_handlers
    enabled = {name: status for name, status in handlers.items() if status.enabled}
    if enabled:
        detail = "; ".join(
            f"{name} -> {status.interpreter}" for name, status in enabled.items()
        )
        checks.append(Check("binfmt handler", Status.OK, detail))
    elif handlers:
        checks.append(
            Check(
                "binfmt handler",
                Status.WARN,
                "registered but disabled",
                "Run: sudo systemctl restart systemd-binfmt",
            )
        )
    else:
        checks.append(
            Check(
                "binfmt handler",
                Status.INFO,
                "not registered; applications are started through the backend explicitly",
            )
        )

    loader_path = loader if loader is not None else native_x86_loader()
    runtime = sysroot if sysroot is not None else private_sysroot_dir()
    runtime_loader = runtime / "lib64" / X86_LOADER
    runtime_libc = runtime / "usr" / "lib" / "x86_64-linux-gnu" / "libc.so.6"
    if loader_path is not None and loader_path.exists():
        checks.append(Check("AMD64 runtime libraries", Status.OK, str(loader_path)))
    elif runtime_loader.exists() or runtime_libc.exists():
        checks.append(
            Check(
                "AMD64 runtime libraries",
                Status.OK,
                f"private sysroot at {runtime}",
            )
        )
    else:
        checks.append(
            Check(
                "AMD64 runtime libraries",
                Status.WARN,
                "no amd64 loader or libc found",
                "Run: crossrun setup --with-libs (needed by dynamically linked apps)",
            )
        )

    foreign = _foreign_architectures()
    checks.append(
        Check(
            "Multiarch",
            Status.OK if "amd64" in foreign else Status.INFO,
            f"foreign architectures: {', '.join(sorted(foreign)) or 'none'}",
            ""
            if "amd64" in foreign
            else "Run: sudo dpkg --add-architecture amd64 (setup --with-libs does this)",
        )
    )

    if os.geteuid() == 0:
        checks.append(Check("Privileges", Status.INFO, "running as root"))
    elif which("sudo"):
        checks.append(Check("Privileges", Status.OK, "sudo is available for setup"))
    else:
        checks.append(
            Check("Privileges", Status.WARN, "sudo not found", "Run the setup as root.")
        )

    library = registry or Registry(registry_path())
    try:
        count = len(library)
    except RegistryError as exc:
        checks.append(
            Check(
                "Application library",
                Status.FAIL,
                str(exc),
                "Remove or repair apps.json",
            )
        )
    else:
        checks.append(
            Check(
                "Application library",
                Status.OK if count else Status.INFO,
                f"{count} application(s) in {library.path}",
            )
        )

    return DoctorReport(platform=host, checks=checks)
