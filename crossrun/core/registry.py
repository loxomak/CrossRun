"""Persistent registry of imported AMD64 applications.

The registry is a small JSON document in the user's data directory.  Writes are
atomic so a crash cannot leave a half-written file behind.
"""

from __future__ import annotations

import json
import os
import re
import secrets
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from ..errors import AmbiguousAppError, AppNotFoundError, RegistryError

SCHEMA_VERSION = 1
KINDS = ("elf", "appimage", "directory")
BACKEND_CHOICES = ("auto", "box64", "qemu")


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def slugify(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.casefold()).strip("-")
    return slug[:40] or "app"


@dataclass
class AppEntry:
    """One imported AMD64 payload."""

    id: str
    name: str
    kind: str
    source: str
    path: str
    workdir: str | None = None
    backend: str = "auto"
    args: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    added_at: str = field(default_factory=utc_now)
    notes: str = ""

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "kind": self.kind,
            "source": self.source,
            "path": self.path,
            "workdir": self.workdir,
            "backend": self.backend,
            "args": list(self.args),
            "env": dict(self.env),
            "added_at": self.added_at,
            "notes": self.notes,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "AppEntry":
        if not isinstance(data, dict):
            raise RegistryError("registry entry is not an object")
        if not isinstance(data.get("args", []), list):
            raise RegistryError("registry entry 'args' must be a list")
        if not isinstance(data.get("env", {}), dict):
            raise RegistryError("registry entry 'env' must be an object")
        try:
            entry = cls(
                id=str(data["id"]),
                name=str(data["name"]),
                kind=str(data["kind"]),
                source=str(data["source"]),
                path=str(data["path"]),
                workdir=str(data["workdir"]) if data.get("workdir") else None,
                backend=str(data.get("backend", "auto")),
                args=[str(item) for item in data.get("args", [])],
                env={str(k): str(v) for k, v in data.get("env", {}).items()},
                added_at=str(data.get("added_at") or utc_now()),
                notes=str(data.get("notes", "")),
            )
        except KeyError as exc:
            raise RegistryError(f"registry entry is missing '{exc.args[0]}'") from exc
        if entry.kind not in KINDS:
            raise RegistryError(f"unknown application kind '{entry.kind}'")
        if entry.backend not in BACKEND_CHOICES:
            raise RegistryError(f"unknown backend '{entry.backend}'")
        return entry


class Registry:
    """Read/write access to ``apps.json``."""

    def __init__(self, path: str | os.PathLike[str]) -> None:
        self.path = Path(path)
        self._apps: dict[str, AppEntry] | None = None

    # -- reading ---------------------------------------------------------
    def load(self) -> None:
        if self._apps is not None:
            return
        if not self.path.exists():
            self._apps = {}
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except OSError as exc:
            raise RegistryError(f"cannot read {self.path}: {exc}") from exc
        except json.JSONDecodeError as exc:
            raise RegistryError(f"{self.path} is not valid JSON: {exc}") from exc

        if not isinstance(raw, dict):
            raise RegistryError(f"{self.path} does not contain a JSON object")
        schema = raw.get("schema", SCHEMA_VERSION)
        if schema != SCHEMA_VERSION:
            raise RegistryError(
                f"unsupported registry schema {schema!r} in {self.path} "
                f"(expected {SCHEMA_VERSION})"
            )
        items = raw.get("apps", [])
        if not isinstance(items, list):
            raise RegistryError(f"'apps' in {self.path} must be a list")
        apps: dict[str, AppEntry] = {}
        for item in items:
            entry = AppEntry.from_dict(item)
            if entry.id in apps:
                raise RegistryError(f"duplicate application id '{entry.id}'")
            apps[entry.id] = entry
        self._apps = apps

    @property
    def apps(self) -> list[AppEntry]:
        self.load()
        assert self._apps is not None
        return sorted(self._apps.values(), key=lambda entry: entry.name.casefold())

    def __len__(self) -> int:
        self.load()
        assert self._apps is not None
        return len(self._apps)

    def reload(self) -> None:
        """Drop the cache and re-read apps.json (for external changes)."""

        self._apps = None
        self.load()

    def get(self, key: str) -> AppEntry:
        """Find an entry by id, then by exact (case-insensitive) name."""

        self.load()
        assert self._apps is not None
        if key in self._apps:
            return self._apps[key]
        folded = key.casefold()
        matches = [entry for entry in self._apps.values() if entry.name.casefold() == folded]
        if not matches:
            raise AppNotFoundError(f"'{key}' is not in the CrossRun library")
        if len(matches) > 1:
            names = ", ".join(entry.id for entry in matches)
            raise AmbiguousAppError(f"'{key}' matches several applications: {names}")
        return matches[0]

    def find_by_source(self, source: str) -> AppEntry | None:
        self.load()
        assert self._apps is not None
        resolved = str(Path(source).expanduser().resolve())
        for entry in self._apps.values():
            if entry.source == resolved:
                return entry
        return None

    # -- writing ---------------------------------------------------------
    def add(self, entry: AppEntry) -> AppEntry:
        self.load()
        assert self._apps is not None
        if entry.id in self._apps:
            raise RegistryError(f"application id '{entry.id}' is already in use")
        self._apps[entry.id] = entry
        self.save()
        return entry

    def remove(self, key: str) -> AppEntry:
        entry = self.get(key)
        assert self._apps is not None
        del self._apps[entry.id]
        self.save()
        return entry

    def save(self) -> None:
        self.load()
        assert self._apps is not None
        payload = {
            "schema": SCHEMA_VERSION,
            "apps": [entry.to_dict() for entry in self.apps],
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_name(self.path.name + ".tmp")
        with open(temporary, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, self.path)

    # -- helpers ---------------------------------------------------------
    def new_id(self, name: str) -> str:
        self.load()
        assert self._apps is not None
        base = slugify(name)
        while True:
            candidate = f"{base}-{secrets.token_hex(3)}"
            if candidate not in self._apps:
                return candidate

    def unique_name(self, name: str) -> str:
        existing = {entry.name.casefold() for entry in self.apps}
        candidate = name.strip() or "Application"
        index = 2
        while candidate.casefold() in existing:
            candidate = f"{name.strip() or 'Application'} ({index})"
            index += 1
        return candidate
