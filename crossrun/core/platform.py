"""Detection of the host platform (Raspberry Pi model, distro, data dirs)."""

from __future__ import annotations

import os
import re
import shutil
import struct
import sys
from dataclasses import dataclass
from pathlib import Path

from .. import APP_NAME

_PI_RE = re.compile(
    r"(?i)raspberry pi\s+(zero\s*2\s*w?|zero|500|400|5|4|3|2|1|compute module\s*\d*)"
)


@dataclass(frozen=True)
class HostPlatform:
    """Immutable snapshot of the machine CrossRun runs on."""

    machine: str
    system: str
    distro_id: str = ""
    distro_version: str = ""
    distro_name: str = ""
    model: str = ""
    ram_mb: int = 0
    is_raspberry_pi: bool = False
    pi_generation: int | None = None

    @property
    def is_arm64(self) -> bool:
        return self.machine.lower() in {"aarch64", "arm64"}

    @property
    def is_64bit_python(self) -> bool:
        return struct.calcsize("P") * 8 == 64

    @property
    def distro_label(self) -> str:
        if self.distro_name and self.distro_version:
            return f"{self.distro_name} {self.distro_version}"
        return self.distro_name or self.distro_id or self.system

    def describe(self) -> str:
        parts = [self.model or self.machine]
        if self.distro_label:
            parts.append(self.distro_label)
        if self.ram_mb:
            parts.append(f"{self.ram_mb} MB RAM")
        return ", ".join(parts)


def _read_text(path: Path, limit: int = 4096) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")[:limit]
    except OSError:
        return ""


def parse_pi_model(raw: str) -> tuple[str, int | None]:
    """Return a cleaned model string and its generation, if recognisable."""

    model = raw.replace("\x00", "").strip()
    match = _PI_RE.search(model)
    if not match:
        return model, None
    token = re.sub(r"\s+", " ", match.group(1).lower())
    if token.startswith("zero 2"):
        return model, 3
    if token == "zero":
        return model, 1
    if token == "400":
        return model, 4
    if token == "500":
        return model, 5
    if token.startswith("compute module"):
        digits = re.search(r"(\d+)", token)
        if digits:
            return model, int(digits.group(1))
        return model, None
    if token.isdigit():
        return model, int(token)
    return model, None


def read_os_release(root: Path = Path("/")) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in _read_text(root / "etc" / "os-release").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        value = value.strip().strip('"').strip("'")
        values[key.strip()] = value
    return values


def read_ram_mb(root: Path = Path("/")) -> int:
    for line in _read_text(root / "proc" / "meminfo").splitlines():
        if line.startswith("MemTotal:"):
            fields = line.split()
            if len(fields) >= 2 and fields[1].isdigit():
                return int(fields[1]) // 1024
    return 0


def detect_platform(
    root: Path = Path("/"),
    *,
    machine: str | None = None,
    system: str | None = None,
) -> HostPlatform:
    os_release = read_os_release(root)
    model_raw = _read_text(root / "proc" / "device-tree" / "model", limit=256)
    if not model_raw:
        model_raw = _read_text(root / "sys" / "firmware" / "devicetree" / "base" / "model", 256)
    model, generation = parse_pi_model(model_raw)
    return HostPlatform(
        machine=(machine or os.uname().machine).lower(),
        system=system or sys.platform,
        distro_id=os_release.get("ID", ""),
        distro_version=os_release.get("VERSION_ID", ""),
        distro_name=os_release.get("NAME", ""),
        model=model,
        ram_mb=read_ram_mb(root),
        is_raspberry_pi=bool(model) and "raspberry pi" in model.lower(),
        pi_generation=generation,
    )


def data_dir(env: dict[str, str] | None = None) -> Path:
    env = os.environ if env is None else env
    base = env.get("XDG_DATA_HOME", "").strip()
    root = Path(base) if base else Path(env.get("HOME", str(Path.home()))) / ".local" / "share"
    return root / APP_NAME.lower()


def cache_dir(env: dict[str, str] | None = None) -> Path:
    env = os.environ if env is None else env
    base = env.get("XDG_CACHE_HOME", "").strip()
    root = Path(base) if base else Path(env.get("HOME", str(Path.home()))) / ".cache"
    return root / APP_NAME.lower()


def registry_path(env: dict[str, str] | None = None) -> Path:
    return data_dir(env) / "apps.json"


def apps_dir(env: dict[str, str] | None = None) -> Path:
    return data_dir(env) / "apps"


def private_sysroot_dir(env: dict[str, str] | None = None) -> Path:
    """Private, rootless x86-64 sysroot assembled from Debian .deb packages."""

    return data_dir(env) / "sysroot"


def qemu_shim_dir(env: dict[str, str] | None = None) -> Path:
    """Directory of symlinks to host multiarch x86-64 libraries (if any)."""

    return data_dir(env) / "qemu-prefix"


def find_terminal() -> str | None:
    """Return a terminal emulator suitable for running interactive commands."""

    for name in (
        "x-terminal-emulator",
        "lxterminal",
        "gnome-terminal",
        "konsole",
        "xfce4-terminal",
        "mate-terminal",
        "xterm",
    ):
        found = shutil.which(name)
        if found:
            return found
    return None


def terminal_command(terminal: str, shell_command: str) -> list[str]:
    """Build an argv that runs *shell_command* inside *terminal*."""

    name = Path(terminal).name
    if name in {"gnome-terminal", "mate-terminal"}:
        return [terminal, "--", "bash", "-lc", shell_command]
    return [terminal, "-e", "bash", "-lc", shell_command]
