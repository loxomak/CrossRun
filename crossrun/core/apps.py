"""Importing AMD64 payloads (ELF binaries, AppImages, extracted trees)."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from ..errors import (
    AppExistsError,
    ExtractionError,
    MissingBackendError,
    UnsupportedPayloadError,
)
from .backends import (
    AUTO,
    BOX64,
    LaunchPlan,
    backend_for,
    detect_backends,
    ensure_qemu_sysroot,
    select_backend_info,
    sysroot_library_dirs,
    sysroot_ready,
)
from .elf import NotAnElfFile, is_appimage, locate_squashfs_offset, read_elf_info
from .platform import apps_dir as default_apps_dir
from .platform import data_dir as default_data_dir
from .registry import AppEntry, BACKEND_CHOICES, Registry
from .runner import Command, Runner

_LIB_DIR_NAMES = {"lib", "lib32", "lib64", "libexec", "share", "doc", "man", "locale", "icons"}
_APPIMAGE_SUFFIXES = (".AppImage", ".appimage")


@dataclass(frozen=True)
class ImportResult:
    entry: AppEntry
    warnings: tuple[str, ...] = ()


def default_app_name(path: Path) -> str:
    name = path.name
    for suffix in (*_APPIMAGE_SUFFIXES, ".run", ".bin"):
        if name.endswith(suffix):
            return name[: -len(suffix)] or path.name
    return path.stem or path.name


def _amd64_info(path: Path):
    """Return ElfInfo for *path*, converting parser errors to a friendly error."""

    try:
        info = read_elf_info(path)
    except NotAnElfFile as exc:
        raise UnsupportedPayloadError(f"{path} is not an ELF binary ({exc})") from exc
    except OSError as exc:
        raise UnsupportedPayloadError(f"cannot read {path}: {exc}") from exc
    if not info.is_amd64:
        if info.is_i386:
            hint = (
                "32-bit x86 needs Box86, which CrossRun does not manage; use a "
                "64-bit build of the application instead."
            )
        elif info.is_arm64:
            hint = "It is already an ARM64 binary and can run directly."
        else:
            hint = "Only 64-bit x86-64 (AMD64) Linux binaries can be translated."
        raise UnsupportedPayloadError(f"{path} is {info.summary()}. {hint}")
    return info


def scan_amd64_executables(
    root: Path, *, max_depth: int = 6, limit: int = 200
) -> list[Path]:
    """Find executable AMD64 ELF files below *root*, ignoring libraries."""

    root = root.resolve()
    found: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(root):
        current = Path(dirpath)
        depth = len(current.relative_to(root).parts)
        if depth >= max_depth:
            dirnames[:] = []
        dirnames[:] = sorted(
            name
            for name in dirnames
            if not name.startswith(".") and name.lower() not in _LIB_DIR_NAMES
        )
        for filename in sorted(filenames):
            if filename.startswith(".") or ".so" in filename:
                continue
            candidate = current / filename
            try:
                if not candidate.is_file() or not os.access(candidate, os.X_OK):
                    continue
                info = read_elf_info(candidate)
            except (OSError, NotAnElfFile):
                continue
            if info.is_amd64:
                found.append(candidate)
                if len(found) >= limit:
                    return found
    return found


def _relative(path: Path, root: Path) -> str:
    try:
        return str(path.relative_to(root))
    except ValueError:
        return str(path)


def _normalise_name(value: str) -> str:
    value = re.sub(r"\s+\(\d+\)$", "", value.strip())
    return re.sub(r"[^a-z0-9]+", "", value.casefold())


def find_app_executable(
    root: Path, *, app_name: str = "", entry: str | None = None
) -> Path:
    """Pick the AMD64 entrypoint inside an extracted application tree."""

    root = root.resolve()
    if entry:
        candidate = (root / entry).resolve()
        try:
            candidate.relative_to(root)
        except ValueError as exc:
            raise UnsupportedPayloadError(f"--entry must stay inside {root}") from exc
        if not candidate.is_file():
            raise UnsupportedPayloadError(f"entrypoint {candidate} does not exist")
        _amd64_info(candidate)
        if not os.access(candidate, os.X_OK):
            raise UnsupportedPayloadError(f"entrypoint {candidate} is not executable")
        return candidate

    apprun = root / "AppRun"
    if apprun.is_file():
        try:
            if read_elf_info(apprun).is_amd64:
                if not os.access(apprun, os.X_OK):
                    raise UnsupportedPayloadError(f"{apprun} is not executable")
                return apprun
        except (OSError, NotAnElfFile):
            pass

    candidates = scan_amd64_executables(root)
    if not candidates:
        raise UnsupportedPayloadError(
            f"no executable AMD64 binary was found under {root}. Use --entry to "
            "select one manually."
        )
    if len(candidates) == 1:
        return candidates[0]

    if app_name:
        wanted = _normalise_name(app_name)
        exact = [
            item for item in candidates if _normalise_name(item.stem) == wanted
        ]
        if len(exact) == 1:
            return exact[0]
        matches = [
            item for item in candidates if wanted and wanted in _normalise_name(item.name)
        ]
        if len(matches) == 1:
            return matches[0]
        if matches:
            candidates = matches
        else:
            relative = [
                item
                for item in candidates
                if wanted and wanted in _normalise_name(_relative(item, root))
            ]
            if len(relative) == 1:
                return relative[0]
            if relative:
                candidates = relative

    listing = ", ".join(_relative(item, root) for item in candidates[:8])
    more = "" if len(candidates) <= 8 else f" (+{len(candidates) - 8} more)"
    raise UnsupportedPayloadError(
        f"several AMD64 executables were found under {root}: {listing}{more}. "
        "Re-add the application with --entry <relative path>."
    )


def extract_appimage(
    image: Path,
    destination: Path,
    *,
    runner: Runner | None = None,
    which=shutil.which,
) -> Path:
    """Unpack a type 2 AppImage without executing its x86-64 runtime.

    ``unsquashfs -o <offset>`` reads the appended squashfs payload directly, so
    extraction also works before a translation backend is installed.
    """

    destination = Path(destination)
    if (destination / "AppRun").exists():
        return destination

    offset = locate_squashfs_offset(image)
    if offset is None:
        if is_appimage(image):
            raise ExtractionError(
                f"{image.name} is not a type 2 AppImage. Type 1 AppImages are not "
                "supported; ask the vendor for a type 2 build."
            )
        raise ExtractionError(f"{image} does not contain a squashfs payload")

    unsquashfs = which("unsquashfs")
    if not unsquashfs:
        raise ExtractionError(
            "unsquashfs is required to unpack AppImages. Install it with: "
            "sudo apt-get install squashfs-tools"
        )

    temporary = destination.with_name(destination.name + ".part")
    if temporary.exists():
        shutil.rmtree(temporary)
    destination.parent.mkdir(parents=True, exist_ok=True)

    active_runner = runner or Runner()
    result = active_runner.run(
        Command((unsquashfs, "-o", str(offset), "-d", str(temporary), "-f", str(image))),
        check=False,
    )
    if not result.ok or not (temporary / "AppRun").exists():
        shutil.rmtree(temporary, ignore_errors=True)
        detail = result.output_tail(6)
        raise ExtractionError(
            f"unsquashfs could not unpack {image.name}" + (f":\n{detail}" if detail else "")
        )

    if destination.exists():
        if any(destination.iterdir()):
            shutil.rmtree(temporary, ignore_errors=True)
            raise ExtractionError(f"{destination} already exists and is not empty")
        destination.rmdir()
    temporary.rename(destination)
    return destination


def import_app(
    source: str | os.PathLike[str],
    registry: Registry,
    *,
    name: str | None = None,
    backend: str = "auto",
    entry: str | None = None,
    data_root: str | os.PathLike[str] | None = None,
    runner: Runner | None = None,
    which=shutil.which,
) -> ImportResult:
    """Validate *source* and add it to the library."""

    if backend not in BACKEND_CHOICES:
        raise UnsupportedPayloadError(
            f"unknown backend '{backend}' (expected {', '.join(BACKEND_CHOICES)})"
        )
    raw = Path(source).expanduser()
    if not raw.exists():
        raise UnsupportedPayloadError(f"{raw} does not exist")
    resolved = raw.resolve()

    existing = registry.find_by_source(str(resolved))
    if existing is not None:
        raise AppExistsError(
            f"{resolved} is already in the library as '{existing.name}' ({existing.id})"
        )

    display = registry.unique_name((name or default_app_name(resolved)).strip())
    app_id = registry.new_id(display)
    root = Path(data_root) if data_root is not None else default_apps_dir()
    warnings: list[str] = []

    if resolved.is_dir():
        executable = find_app_executable(resolved, app_name=display, entry=entry)
        kind, workdir = "directory", resolved
    elif resolved.is_file():
        if is_appimage(resolved):
            app_root = root / app_id
            try:
                appdir = extract_appimage(
                    resolved, app_root / "AppDir", runner=runner, which=which
                )
                executable = find_app_executable(appdir, app_name=display, entry=entry)
            except Exception:
                shutil.rmtree(app_root, ignore_errors=True)
                raise
            kind, workdir = "appimage", appdir
            warnings.append(
                "AppImage AppRun setup was skipped; the bundled AMD64 binary is "
                "launched directly"
            )
        else:
            _amd64_info(resolved)
            if not os.access(resolved, os.X_OK):
                warnings.append(f"{resolved.name} is not executable; run chmod +x first")
            executable, kind, workdir = resolved, "elf", resolved.parent
    else:
        raise UnsupportedPayloadError(f"{resolved} is not a file or directory")

    app = AppEntry(
        id=app_id,
        name=display,
        kind=kind,
        source=str(resolved),
        path=str(executable),
        workdir=str(workdir),
        backend=backend,
        notes="\n".join(warnings),
    )
    registry.add(app)
    return ImportResult(app, tuple(warnings))


def library_dirs(entry: AppEntry) -> tuple[str, ...]:
    """Existing library directories shipped alongside the application."""

    base = Path(entry.workdir or Path(entry.path).parent)
    found: list[str] = []
    for relative in ("lib", "lib64", "usr/lib", "usr/lib64", "usr/lib/x86_64-linux-gnu"):
        candidate = base / relative
        if candidate.is_dir():
            found.append(str(candidate))
    return tuple(dict.fromkeys(found))


def resolve_sysroot(
    data_root: str | os.PathLike[str] | None = None,
) -> Path | None:
    """Return a usable x86-64 sysroot for QEMU/Box64, if one can be built."""

    root = Path(data_root) if data_root is not None else default_data_dir()
    private = root / "sysroot"
    if sysroot_ready(private):
        return private
    return ensure_qemu_sysroot(root / "qemu-prefix")


def build_plan(
    entry: AppEntry,
    *,
    backend: str | None = None,
    extra_args: tuple[str, ...] = (),
    infos=None,
    sysroot: Path | None = None,
    data_root: str | os.PathLike[str] | None = None,
    which=shutil.which,
) -> LaunchPlan:
    """Resolve the backend and build the exact command that runs *entry*."""

    key = backend or entry.backend or AUTO
    available = detect_backends(which=which) if infos is None else infos
    info = select_backend_info(key, available, which=which)

    if sysroot is None:
        sysroot = resolve_sysroot(data_root)

    implementation = backend_for(info.key, infos=available, sysroot=sysroot, which=which)
    app_libs = library_dirs(entry)
    libdirs = app_libs
    if info.key == BOX64:
        libdirs = app_libs + sysroot_library_dirs(sysroot)

    plan = implementation.build_plan(
        entry.path,
        tuple(entry.args) + tuple(extra_args),
        cwd=entry.workdir,
        extra_env=dict(entry.env),
        libdirs=libdirs,
    )
    notes = list(plan.notes)
    if entry.kind == "appimage":
        notes.append(
            "AppImage wrapper skipped: the bundled AMD64 executable is launched directly"
        )
    if info.key == BOX64 and app_libs:
        notes.append("application libraries are exposed through BOX64_LD_LIBRARY_PATH")
    plan.notes = tuple(dict.fromkeys(notes))
    return plan


def plan_environment(plan: LaunchPlan, base: dict[str, str] | None = None) -> dict[str, str]:
    env = dict(os.environ if base is None else base)
    env.update(plan.env)
    return env


def launch_plan(plan: LaunchPlan, *, env: dict[str, str] | None = None) -> int:
    """Run *plan* in the foreground and return a shell-style exit code."""

    try:
        process = subprocess.Popen(
            list(plan.argv),
            cwd=plan.cwd,
            env=plan_environment(plan, env),
        )
    except FileNotFoundError as exc:
        raise MissingBackendError(f"cannot start {plan.argv[0]}: {exc}") from exc
    try:
        returncode = process.wait()
    except KeyboardInterrupt:
        process.terminate()
        raise
    return 128 - returncode if returncode < 0 else returncode


def spawn_plan(plan: LaunchPlan, *, env: dict[str, str] | None = None) -> subprocess.Popen:
    """Start *plan* in the background (used by the GTK interface)."""

    try:
        return subprocess.Popen(
            list(plan.argv),
            cwd=plan.cwd,
            env=plan_environment(plan, env),
            start_new_session=True,
        )
    except FileNotFoundError as exc:
        raise MissingBackendError(f"cannot start {plan.argv[0]}: {exc}") from exc


def capture_plan(
    plan: LaunchPlan, *, env: dict[str, str] | None = None, timeout: float = 30.0
) -> subprocess.CompletedProcess:
    """Run *plan* and capture its output (used by the self-test)."""

    try:
        return subprocess.run(
            list(plan.argv),
            cwd=plan.cwd,
            env=plan_environment(plan, env),
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except FileNotFoundError as exc:
        raise MissingBackendError(f"cannot start {plan.argv[0]}: {exc}") from exc
