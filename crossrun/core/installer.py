"""Installation plans for Box64, QEMU user-mode and amd64 runtime libraries."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from ..errors import SetupError, UnsupportedHostError
from .backends import BOX64, QEMU
from .platform import HostPlatform, cache_dir
from .runner import Command, CommandResult, LineHandler, Runner

BOX64_REPO = "https://github.com/ptitSeb/box64.git"
BINFMT_STATUS = Path("/proc/sys/fs/binfmt_misc/qemu-x86_64")


@dataclass(frozen=True)
class InstallStep:
    title: str
    command: Command
    optional: bool = False


@dataclass
class InstallPlan:
    backends: tuple[str, ...]
    steps: list[InstallStep] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    with_libs: bool = False


@dataclass
class SetupOutcome:
    plan: InstallPlan
    results: list[CommandResult] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class BinfmtStatus:
    registered: bool
    enabled: bool = False
    interpreter: str = ""
    flags: str = ""

    def describe(self) -> str:
        if not self.registered:
            return "not registered"
        state = "enabled" if self.enabled else "disabled"
        detail = f", interpreter {self.interpreter}" if self.interpreter else ""
        return f"{state}{detail}"


def _parse_backends(backend: str) -> tuple[str, ...]:
    if backend == "both":
        return (BOX64, QEMU)
    if backend not in (BOX64, QEMU):
        raise SetupError(f"unknown backend '{backend}' (expected box64, qemu or both)")
    return (backend,)


def plan_setup(
    platform: HostPlatform,
    *,
    backend: str = "both",
    with_libs: bool = False,
    source_dir: Path | None = None,
    jobs: int | None = None,
) -> InstallPlan:
    """Build a reviewable plan; nothing is executed here."""

    if not platform.is_arm64:
        raise UnsupportedHostError(
            f"this is a {platform.machine} system. AMD64 translation requires a "
            "64-bit ARM (aarch64) OS such as Raspberry Pi OS 64-bit."
        )

    chosen = _parse_backends(backend)
    plan = InstallPlan(backends=chosen, with_libs=with_libs)

    packages = ["squashfs-tools"]
    if BOX64 in chosen:
        packages += ["git", "cmake", "build-essential"]
    if QEMU in chosen:
        packages += ["qemu-user-static"]

    if with_libs:
        plan.steps.append(
            InstallStep(
                "Enable the amd64 package architecture",
                Command(("dpkg", "--add-architecture", "amd64"), needs_root=True),
            )
        )

    plan.steps.append(
        InstallStep(
            "Refresh package lists",
            Command(("apt-get", "update"), needs_root=True),
        )
    )
    plan.steps.append(
        InstallStep(
            "Install required system packages",
            Command(("apt-get", "install", "-y", *packages), needs_root=True),
        )
    )

    if BOX64 in chosen:
        source = Path(source_dir) if source_dir is not None else cache_dir() / "src" / "box64"
        if not (source / ".git").exists():
            plan.steps.append(
                InstallStep(
                    "Download Box64 sources",
                    Command(
                        ("git", "clone", "--depth", "1", BOX64_REPO, str(source)),
                        cwd=str(source.parent),
                    ),
                )
            )
        else:
            plan.notes.append(f"using the cached Box64 source tree at {source}")

        flags = ["-S", str(source), "-B", str(source / "build")]
        flags += ["-DARM_DYNAREC=ON", "-DCMAKE_BUILD_TYPE=RelWithDebInfo"]
        if platform.pi_generation == 5:
            flags.append("-DRPI5ARM64=ON")
            plan.notes.append("enabling the Raspberry Pi 5 (Cortex-A76) tuning flags")
        elif platform.pi_generation == 4:
            flags.append("-DRPI4ARM64=ON")
            plan.notes.append("enabling the Raspberry Pi 4 tuning flags")
        elif platform.pi_generation == 3:
            flags.append("-DRPI3ARM64=ON")
            plan.notes.append("enabling the Raspberry Pi 3 tuning flags")
        else:
            flags.append("-DARM64=ON")
            plan.notes.append("building for generic ARM64")

        plan.steps.append(
            InstallStep(
                "Configure the Box64 build",
                Command(("cmake", *flags)),
            )
        )
        plan.steps.append(
            InstallStep(
                "Compile Box64 (several minutes)",
                Command(
                    (
                        "cmake",
                        "--build",
                        str(source / "build"),
                        "--parallel",
                        str(jobs or os.cpu_count() or 2),
                    )
                ),
            )
        )
        plan.steps.append(
            InstallStep(
                "Install Box64 to /usr/local",
                Command(("cmake", "--install", str(source / "build")), needs_root=True),
            )
        )
        plan.notes.append(
            "Box64 is built from source because upstream does not ship a Debian "
            "package. Expect 10-20 minutes on a Pi 5 and longer on a Pi 4; the "
            "build is incremental, so retries resume."
        )

    if QEMU in chosen:
        plan.steps.append(
            InstallStep(
                "Register the x86-64 binfmt handler",
                Command(("systemctl", "restart", "systemd-binfmt.service"), needs_root=True),
                optional=True,
            )
        )
        plan.notes.append(
            "qemu-user-static installs a binfmt handler named qemu-x86_64, so "
            "AMD64 binaries can also be started transparently."
        )

    if with_libs:
        plan.notes.append(
            "The amd64 runtime libraries are downloaded and unpacked into "
            "CrossRun's private sysroot instead of being installed system-wide. "
            "This avoids version conflicts between Raspberry Pi OS arm64 "
            "packages and Debian amd64 packages."
        )

    if platform.ram_mb and platform.ram_mb < 2048 and BOX64 in chosen:
        plan.notes.append(
            "Compiling Box64 may be slow with less than 2 GB of RAM; enable swap."
        )
    return plan


def run_setup(
    plan: InstallPlan,
    runner: Runner,
    *,
    on_line: LineHandler | None = None,
) -> SetupOutcome:
    """Execute an install plan step by step."""

    outcome = SetupOutcome(plan=plan)
    for index, step in enumerate(plan.steps, 1):
        if on_line is not None:
            on_line(f"==> [{index}/{len(plan.steps)}] {step.title}")
        if step.command.cwd and not runner.dry_run:
            Path(step.command.cwd).mkdir(parents=True, exist_ok=True)
        runner.echo = on_line or runner.echo
        result = runner.run(step.command, check=not step.optional, on_line=on_line)
        outcome.results.append(result)
        if step.optional and not result.ok:
            outcome.warnings.append(f"{step.title} failed (optional step)")
    return outcome


def read_binfmt_status(path: Path = BINFMT_STATUS) -> BinfmtStatus:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return BinfmtStatus(registered=False)
    enabled = False
    interpreter = ""
    flags = ""
    for line in text.splitlines():
        line = line.strip()
        if line == "enabled":
            enabled = True
        elif line.startswith("interpreter "):
            interpreter = line.split(" ", 1)[1].strip()
        elif line.startswith("flags:"):
            flags = line.split(":", 1)[1].strip()
    return BinfmtStatus(True, enabled, interpreter, flags)


def x86_binfmt_handlers(
    misc_dir: Path = Path("/proc/sys/fs/binfmt_misc"),
) -> dict[str, BinfmtStatus]:
    """Return every registered handler that targets x86-64 emulation."""

    handlers: dict[str, BinfmtStatus] = {}
    try:
        entries = sorted(misc_dir.iterdir())
    except OSError:
        return handlers
    for entry in entries:
        if entry.name in {"status", "register"} or entry.name.startswith("."):
            continue
        status = read_binfmt_status(entry)
        haystack = f"{entry.name} {status.interpreter}".lower()
        if "x86_64" in haystack or "x86-64" in haystack or "box64" in haystack:
            handlers[entry.name] = status
    return handlers
