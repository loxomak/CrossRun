"""Subprocess execution with dry-run support, logging and root escalation."""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess
from dataclasses import dataclass, field
from typing import Callable

from ..errors import CommandError

Echo = Callable[[str], None]
LineHandler = Callable[[str], None]


@dataclass(frozen=True)
class Command:
    """A command CrossRun intends to run."""

    argv: tuple[str, ...]
    cwd: str | None = None
    env: dict[str, str] = field(default_factory=dict)
    needs_root: bool = False

    def with_env(self, **values: str) -> "Command":
        merged = dict(self.env)
        merged.update(values)
        return Command(self.argv, self.cwd, merged, self.needs_root)


@dataclass
class CommandResult:
    command: Command
    returncode: int
    stdout: str = ""
    stderr: str = ""

    @property
    def ok(self) -> bool:
        return self.returncode == 0

    def output_tail(self, lines: int = 12) -> str:
        combined = "\n".join(part for part in (self.stdout, self.stderr) if part.strip())
        return "\n".join(combined.strip().splitlines()[-lines:])


class Runner:
    """Execute commands or just print them when ``dry_run`` is enabled."""

    def __init__(self, *, dry_run: bool = False, echo: Echo | None = None) -> None:
        self.dry_run = dry_run
        self.echo = echo

    def _emit(self, text: str) -> None:
        if self.echo is not None:
            self.echo(text)
        else:
            print(text)

    def materialize(self, command: Command) -> list[str]:
        argv = list(command.argv)
        if command.needs_root and os.geteuid() != 0:
            sudo = shutil.which("sudo")
            if not sudo:
                raise CommandError(
                    f"'{shlex.join(argv)}' needs root privileges but sudo is not installed"
                )
            argv.insert(0, sudo)
        return argv

    def display(self, command: Command) -> str:
        prefix = f"(cd {shlex.quote(command.cwd)} && " if command.cwd else ""
        suffix = ")" if command.cwd else ""
        return f"{prefix}{shlex.join(self.materialize(command))}{suffix}"

    def run(
        self,
        command: Command,
        *,
        check: bool = True,
        timeout: float | None = None,
        on_line: LineHandler | None = None,
    ) -> CommandResult:
        argv = self.materialize(command)
        self._emit(f"$ {self.display(command)}")
        if self.dry_run:
            return CommandResult(command=command, returncode=0)

        env = dict(os.environ)
        env.update(command.env)

        if on_line is None:
            try:
                proc = subprocess.run(
                    argv,
                    cwd=command.cwd,
                    env=env,
                    capture_output=True,
                    text=True,
                    timeout=timeout,
                )
            except FileNotFoundError as exc:
                raise CommandError(f"command not found: {argv[0]}") from exc
            except subprocess.TimeoutExpired as exc:
                raise CommandError(f"command timed out: {shlex.join(argv)}") from exc
            result = CommandResult(command, proc.returncode, proc.stdout or "", proc.stderr or "")
        else:
            collected: list[str] = []
            try:
                proc = subprocess.Popen(
                    argv,
                    cwd=command.cwd,
                    env=env,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    bufsize=1,
                )
            except FileNotFoundError as exc:
                raise CommandError(f"command not found: {argv[0]}") from exc
            assert proc.stdout is not None
            with proc.stdout:
                for raw in proc.stdout:
                    line = raw.rstrip("\n")
                    collected.append(line)
                    on_line(line)
            returncode = proc.wait(timeout=timeout)
            result = CommandResult(command, returncode, "\n".join(collected), "")

        if check and not result.ok:
            detail = result.output_tail()
            message = f"command failed with exit code {result.returncode}: {self.display(command)}"
            if detail:
                message += f"\n{detail}"
            raise CommandError(message)
        return result


def run_capture(argv: list[str], *, timeout: float = 10.0) -> CommandResult:
    """Run a read-only helper command and capture its output."""

    command = Command(tuple(argv))
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError):
        return CommandResult(command=command, returncode=127)
    return CommandResult(command, proc.returncode, proc.stdout or "", proc.stderr or "")
