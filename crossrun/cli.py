"""Command line interface for CrossRun."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from pathlib import Path

from . import APP_NAME, TAGLINE, __version__
from .core.apps import build_plan, import_app, launch_plan
from .core.backends import (
    AUTO,
    BACKEND_KEYS,
    detect_backends,
    sysroot_ready,
)
from .core.doctor import run_doctor
from .core.elf import NotAnElfFile, is_appimage, read_elf_info
from .core.installer import (
    plan_setup,
    run_setup,
    x86_binfmt_handlers,
)
from .core.platform import (
    data_dir,
    detect_platform,
    private_sysroot_dir,
    registry_path,
)
from .core.registry import AppEntry, Registry
from .core.runner import Runner
from .core.runtime import (
    BASE_PACKAGES,
    install_runtime_libraries,
    packages_to_install,
)
from .core.selftest import run_selftest
from .errors import CommandError, CrossRunError, UnsupportedHostError


def _registry() -> Registry:
    return Registry(registry_path())


def _confirm(prompt: str, assume_yes: bool) -> bool:
    if assume_yes:
        return True
    if not sys.stdin.isatty():
        print(f"{prompt} [y/N] aborted: stdin is not a terminal (use --yes)")
        return False
    try:
        answer = input(f"{prompt} [y/N] ").strip().lower()
    except EOFError:
        return False
    return answer in {"y", "yes"}


def _describe_backends(infos) -> str:
    parts = []
    for info in infos:
        if info.available:
            parts.append(f"{info.label}: {info.version or info.executable}")
        else:
            parts.append(f"{info.label}: not installed")
    return "\n".join(f"  {part}" for part in parts)


def _table(headers: list[str], rows: list[list[str]]) -> str:
    widths = [
        max(len(row[index]) for row in [headers, *rows])
        for index in range(len(headers))
    ]
    lines = ["  ".join(cell.ljust(widths[i]) for i, cell in enumerate(headers)).rstrip()]
    lines.append("  ".join("-" * width for width in widths))
    for row in rows:
        lines.append(
            "  ".join(cell.ljust(widths[i]) for i, cell in enumerate(row)).rstrip()
        )
    return "\n".join(lines)


def _render_app(entry: AppEntry) -> str:
    lines = [
        f"Name:      {entry.name}",
        f"ID:        {entry.id}",
        f"Kind:      {entry.kind}",
        f"Backend:   {entry.backend}",
        f"Entry:     {entry.path}",
        f"Source:    {entry.source}",
        f"Workdir:   {entry.workdir or '-'}",
        f"Added:     {entry.added_at}",
    ]
    if entry.args:
        lines.append(f"Arguments: {' '.join(entry.args)}")
    if entry.env:
        lines.append("Env:       " + " ".join(f"{k}={v}" for k, v in entry.env.items()))
    if entry.notes:
        lines.append(f"Notes:     {entry.notes}")
    return "\n".join(lines)


def _strip_separator(rest: list[str]) -> tuple[str, ...]:
    if rest and rest[0] == "--":
        rest = rest[1:]
    return tuple(rest)


def cmd_doctor(args: argparse.Namespace) -> int:
    report = run_doctor()
    if args.json:
        print(json.dumps(report.as_dict(), indent=2))
    else:
        print(report.render())
    return 0 if report.healthy else 1


def cmd_list(args: argparse.Namespace) -> int:
    apps = _registry().apps
    if args.json:
        print(json.dumps([entry.to_dict() for entry in apps], indent=2))
        return 0
    if not apps:
        print("No AMD64 applications are registered yet.")
        print("Add one with: crossrun add <binary-or-AppImage>")
        return 0
    rows = [[entry.name, entry.kind, entry.backend, entry.id] for entry in apps]
    print(_table(["NAME", "KIND", "BACKEND", "ID"], rows))
    return 0


def cmd_info(args: argparse.Namespace) -> int:
    entry = _registry().get(args.app)
    if args.json:
        print(json.dumps(entry.to_dict(), indent=2))
    else:
        print(_render_app(entry))
    return 0


def _parse_env_assignments(items: list[str]) -> dict[str, str]:
    env: dict[str, str] = {}
    for item in items:
        key, separator, value = item.partition("=")
        key = key.strip()
        if not separator or not key or "=" in key or any(ch.isspace() for ch in key):
            raise CrossRunError(
                f"invalid --env value {item!r} (expected KEY=VALUE)"
            )
        env[key] = value
    return env


def cmd_add(args: argparse.Namespace) -> int:
    env = _parse_env_assignments(args.env)
    registry = _registry()
    result = import_app(
        args.path,
        registry,
        name=args.name,
        backend=args.backend,
        entry=args.entry,
        runner=Runner(echo=print),
    )
    entry = result.entry
    if args.arg or env:
        entry.args = list(args.arg)
        entry.env = env
        registry.save()
    print(f"Added '{entry.name}' ({entry.kind})")
    print(_render_app(entry))
    for warning in result.warnings:
        print(f"warning: {warning}", file=sys.stderr)
    print(f"\nRun it with: crossrun run {entry.name!r}")
    return 0


def cmd_remove(args: argparse.Namespace) -> int:
    registry = _registry()
    entry = registry.get(args.app)
    if not _confirm(f"Remove '{entry.name}' from the library?", args.yes):
        return 1
    registry.remove(entry.id)
    print(f"Removed '{entry.name}' ({entry.id})")
    if entry.kind == "appimage":
        extracted = data_dir() / "apps" / entry.id
        if extracted.exists():
            shutil.rmtree(extracted)
            print(f"Deleted extracted data: {extracted}")
        print("The original AppImage was left untouched.")
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    entry = _registry().get(args.app)
    plan = build_plan(
        entry,
        backend=args.backend,
        extra_args=_strip_separator(list(args.rest)),
        data_root=data_dir(),
    )
    for note in plan.notes:
        print(f"note: {note}", file=sys.stderr)
    if args.dry_run:
        print(plan.command())
        return 0
    return launch_plan(plan)


def cmd_exec(args: argparse.Namespace) -> int:
    path = Path(args.binary).expanduser()
    if not path.exists():
        raise CrossRunError(f"{path} does not exist")
    if path.is_dir():
        raise CrossRunError(f"{path} is a directory; use: crossrun add {path}")
    resolved = path.resolve()
    if is_appimage(resolved):
        raise CrossRunError(
            "AppImages must be unpacked before they can run: crossrun add <file>"
        )
    try:
        info = read_elf_info(resolved)
    except (NotAnElfFile, OSError) as exc:
        raise CrossRunError(f"{resolved} is not an ELF binary: {exc}") from exc
    if not info.is_amd64:
        raise CrossRunError(f"{resolved} is {info.summary()}, not an AMD64 binary")
    if not os.access(resolved, os.X_OK):
        print(f"warning: {resolved.name} is not executable; chmod +x may be needed", file=sys.stderr)

    entry = AppEntry(
        id="adhoc",
        name=resolved.name,
        kind="elf",
        source=str(resolved),
        path=str(resolved),
        workdir=str(resolved.parent),
        backend=args.backend,
    )
    plan = build_plan(
        entry,
        backend=args.backend,
        extra_args=_strip_separator(list(args.rest)),
        data_root=data_dir(),
    )
    for note in plan.notes:
        print(f"note: {note}", file=sys.stderr)
    if args.dry_run:
        print(plan.command())
        return 0
    return launch_plan(plan)


def cmd_setup(args: argparse.Namespace) -> int:
    platform = detect_platform()
    if not platform.is_arm64:
        print(
            f"crossrun: setup needs an aarch64 host, this is {platform.machine}.",
            file=sys.stderr,
        )
        print("Use --dry-run to inspect the plan anyway.", file=sys.stderr)
        if not args.dry_run:
            return 2

    try:
        plan = plan_setup(
            platform,
            backend=args.backend,
            with_libs=args.with_libs,
            jobs=args.jobs,
        )
    except UnsupportedHostError as exc:
        print(f"crossrun: error: {exc}", file=sys.stderr)
        return 2

    print(f"{APP_NAME} setup - {platform.describe()}")
    print(
        f"Backends: {', '.join(plan.backends)}"
        + (" + amd64 libraries" if plan.with_libs else "")
    )
    print("Steps:")
    for index, step in enumerate(plan.steps, 1):
        print(f"  {index}. {step.title}")
    for note in plan.notes:
        print(f"  note: {note}")

    if args.dry_run:
        print("\nDry run, executing the steps in no-op mode:")
        runner = Runner(dry_run=True, echo=lambda line: print(line, flush=True))
        run_setup(plan, runner, on_line=lambda line: print(line, flush=True))
        if plan.with_libs:
            print("\nRuntime libraries (downloaded into a private sysroot):")
            install_runtime_libraries(
                private_sysroot_dir(),
                runner=runner,
                on_line=lambda line: print(line, flush=True),
            )
        print("\nDry run complete: no changes were made.")
        return 0

    if not _confirm("\nProceed with the installation?", args.yes):
        return 1

    runner = Runner(echo=lambda line: print(line, flush=True))
    try:
        outcome = run_setup(
            plan,
            runner,
            on_line=lambda line: print(line, flush=True),
        )
    except CommandError as exc:
        print(f"\ncrossrun: setup failed: {exc}", file=sys.stderr)
        return 1

    for warning in outcome.warnings:
        print(f"warning: {warning}", file=sys.stderr)

    runtime_ready = sysroot_ready(private_sysroot_dir())
    if plan.with_libs:
        print("\nInstalling x86-64 runtime libraries into a private sysroot...")
        try:
            runtime = install_runtime_libraries(
                private_sysroot_dir(),
                runner=runner,
                on_line=lambda line: print(line, flush=True),
            )
        except CrossRunError as exc:
            print(f"crossrun: runtime library setup failed: {exc}", file=sys.stderr)
            return 1
        runtime_ready = runtime.ready
        if runtime_ready:
            print(f"Private x86-64 sysroot: {runtime.dest}")
        else:
            print(
                "error: the private x86-64 sysroot is incomplete",
                file=sys.stderr,
            )
            return 1

    infos = detect_backends()
    print("\nVerification:")
    print(_describe_backends(infos))
    handlers = x86_binfmt_handlers()
    if handlers:
        for name, status in handlers.items():
            print(f"  binfmt {name}: {status.describe()}")
    else:
        print("  binfmt: no x86-64 handler registered (explicit launching still works)")
    if runtime_ready:
        print(f"  amd64 runtime: private sysroot {private_sysroot_dir()}")

    missing = [
        key
        for key in plan.backends
        if not any(info.key == key and info.available for info in infos)
    ]
    if missing:
        print(
            f"error: {', '.join(missing)} could not be found after setup",
            file=sys.stderr,
        )
        return 1
    print("\nSetup complete. Verify with: crossrun selftest")
    return 0


def cmd_selftest(args: argparse.Namespace) -> int:
    result = run_selftest(backend=args.backend)
    if args.json:
        print(
            json.dumps(
                {
                    "ok": result.ok,
                    "backend": result.backend,
                    "returncode": result.returncode,
                    "message": result.message,
                },
                indent=2,
            )
        )
    else:
        print(result.render())
    return 0 if result.ok else 1


def cmd_libs(args: argparse.Namespace) -> int:
    requested = tuple(args.packages) if args.packages else BASE_PACKAGES
    destination = private_sysroot_dir()
    packages = packages_to_install(
        requested, sysroot_ready_now=sysroot_ready(destination)
    )
    print(f"Private x86-64 sysroot: {destination}")
    if packages != requested:
        print(
            "Bootstrapping the base runtime first: " + ", ".join(BASE_PACKAGES)
        )
    print("Packages: " + ", ".join(f"{package}:amd64" for package in packages))
    runner = Runner(dry_run=args.dry_run, echo=lambda line: print(line, flush=True))
    try:
        result = install_runtime_libraries(
            destination,
            runner=runner,
            packages=packages,
            on_line=lambda line: print(line, flush=True),
        )
    except CrossRunError as exc:
        print(f"crossrun: {exc}", file=sys.stderr)
        return 1
    if args.dry_run:
        print("\nDry run complete: no changes were made.")
        return 0
    print(f"Unpacked {len(result.debs)} package(s); sysroot ready: {result.ready}")
    return 0 if result.ready else 1


def cmd_gui(args: argparse.Namespace) -> int:
    try:
        from .gui import main as gui_main
    except ImportError as exc:
        print(
            "The CrossRun GUI needs PyGObject: "
            "sudo apt-get install python3-gi gir1.2-gtk-3.0",
            file=sys.stderr,
        )
        print(f"details: {exc}", file=sys.stderr)
        return 1
    return gui_main([])


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="crossrun",
        description=f"{APP_NAME} {__version__} - {TAGLINE}",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "examples:\n"
            "  crossrun doctor\n"
            "  crossrun setup --backend both --with-libs\n"
            "  crossrun add ~/Downloads/Tool-x86_64.AppImage\n"
            "  crossrun add ./game/app --name Game\n"
            "  crossrun run Game\n"
            "  crossrun exec ./hello -- arg1\n"
            "  crossrun selftest --backend qemu\n"
            "  crossrun libs libgl1 libx11-6\n"
        ),
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = parser.add_subparsers(dest="command", metavar="COMMAND")

    doctor = sub.add_parser("doctor", help="check host, backends and libraries")
    doctor.add_argument("--json", action="store_true", help="machine readable output")
    doctor.set_defaults(func=cmd_doctor)

    setup = sub.add_parser(
        "setup", help="install Box64, QEMU user-mode and/or amd64 libraries"
    )
    setup.add_argument("--backend", choices=("box64", "qemu", "both"), default="both")
    setup.add_argument(
        "--with-libs",
        action="store_true",
        help="install amd64 multiarch runtime libraries (for QEMU and dynamic apps)",
    )
    setup.add_argument("--jobs", type=int, default=None, help="parallel compile jobs")
    setup.add_argument("--yes", "-y", action="store_true", help="assume yes")
    setup.add_argument("--dry-run", action="store_true", help="print commands only")
    setup.set_defaults(func=cmd_setup)

    add = sub.add_parser("add", help="register an AMD64 ELF, AppImage or directory")
    add.add_argument("path")
    add.add_argument("--name", "-n", help="display name")
    add.add_argument("--backend", choices=("auto", "box64", "qemu"), default="auto")
    add.add_argument("--entry", help="entrypoint relative to an extracted directory")
    add.add_argument(
        "--arg", action="append", default=[], help="default argument (repeatable)"
    )
    add.add_argument(
        "--env",
        action="append",
        default=[],
        metavar="KEY=VALUE",
        help="environment variable for the application (repeatable)",
    )
    add.set_defaults(func=cmd_add)

    listing = sub.add_parser("list", help="list registered applications")
    listing.add_argument("--json", action="store_true")
    listing.set_defaults(func=cmd_list)

    info = sub.add_parser("info", help="show one registered application")
    info.add_argument("app")
    info.add_argument("--json", action="store_true")
    info.set_defaults(func=cmd_info)

    remove = sub.add_parser("remove", help="remove an application from the library")
    remove.add_argument("app")
    remove.add_argument("--yes", "-y", action="store_true")
    remove.set_defaults(func=cmd_remove)

    run = sub.add_parser("run", help="run a registered application")
    run.add_argument("app")
    run.add_argument("--backend", choices=(AUTO, *BACKEND_KEYS), default=None)
    run.add_argument("--dry-run", action="store_true", help="print the command only")
    run.add_argument("rest", nargs=argparse.REMAINDER, help="arguments after --")
    run.set_defaults(func=cmd_run)

    execute = sub.add_parser("exec", help="run an AMD64 binary without registering it")
    execute.add_argument("binary")
    execute.add_argument("--backend", choices=(AUTO, *BACKEND_KEYS), default=None)
    execute.add_argument("--dry-run", action="store_true", help="print the command only")
    execute.add_argument("rest", nargs=argparse.REMAINDER, help="arguments after --")
    execute.set_defaults(func=cmd_exec)

    selftest = sub.add_parser(
        "selftest", help="verify a backend with a built-in static AMD64 program"
    )
    selftest.add_argument("--backend", choices=(AUTO, *BACKEND_KEYS), default=AUTO)
    selftest.add_argument("--json", action="store_true")
    selftest.set_defaults(func=cmd_selftest)

    libs = sub.add_parser(
        "libs", help="download extra x86-64 libraries into the private sysroot"
    )
    libs.add_argument(
        "packages",
        nargs="*",
        help="Debian package names, e.g. libgl1 libx11-6 (default: base runtime)",
    )
    libs.add_argument("--dry-run", action="store_true", help="print commands only")
    libs.set_defaults(func=cmd_libs)

    gui = sub.add_parser("gui", help="start the GTK application")
    gui.set_defaults(func=cmd_gui)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "command", None):
        print(f"{APP_NAME} {__version__} - {TAGLINE}\n")
        parser.print_help()
        return 0
    try:
        return args.func(args)
    except CrossRunError as exc:
        print(f"crossrun: error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("crossrun: interrupted", file=sys.stderr)
        return 130


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())

