"""Tests for the application registry."""

from __future__ import annotations

import json
import stat
import tempfile
import unittest
from pathlib import Path

from crossrun.core.registry import AppEntry, Registry, slugify
from crossrun.errors import (
    AmbiguousAppError,
    AppNotFoundError,
    RegistryError,
)


def make_entry(app_id: str = "demo-aaa111", name: str = "Demo") -> AppEntry:
    return AppEntry(
        id=app_id,
        name=name,
        kind="elf",
        source=f"/tmp/{app_id}/app",
        path=f"/tmp/{app_id}/app",
        workdir=f"/tmp/{app_id}",
    )


class RegistryTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.path = self.tmp / "data" / "apps.json"
        self.registry = Registry(self.path)

    def test_add_persist_and_lookup(self) -> None:
        entry = self.registry.add(make_entry())
        self.assertEqual(len(self.registry), 1)
        self.assertIs(self.registry.get(entry.id), entry)
        self.assertIs(self.registry.get("demo"), entry)
        self.assertIs(self.registry.get("DEMO"), entry)

        reloaded = Registry(self.path)
        self.assertEqual(reloaded.get(entry.id).to_dict(), entry.to_dict())
        mode = stat.S_IMODE(self.path.stat().st_mode)
        self.assertEqual(mode, 0o600)
        self.assertFalse(self.path.with_name("apps.json.tmp").exists())

    def test_remove(self) -> None:
        self.registry.add(make_entry())
        removed = self.registry.remove("Demo")
        self.assertEqual(removed.id, "demo-aaa111")
        self.assertEqual(len(Registry(self.path)), 0)

    def test_missing_and_ambiguous_names(self) -> None:
        self.registry.add(make_entry("one-111111", "Same"))
        self.registry.add(make_entry("two-222222", "Same"))
        with self.assertRaises(AmbiguousAppError):
            self.registry.get("Same")
        with self.assertRaises(AppNotFoundError):
            self.registry.get("nope")

    def test_duplicate_id_and_source_lookup(self) -> None:
        self.registry.add(make_entry())
        with self.assertRaises(RegistryError):
            self.registry.add(make_entry())
        self.assertEqual(
            self.registry.find_by_source("/tmp/demo-aaa111/app").id, "demo-aaa111"
        )
        self.assertIsNone(self.registry.find_by_source("/tmp/other"))

    def test_unique_names_and_ids(self) -> None:
        self.assertEqual(self.registry.unique_name("Game"), "Game")
        self.registry.add(make_entry("game-111111", "Game"))
        self.assertEqual(self.registry.unique_name("Game"), "Game (2)")
        new_id = self.registry.new_id("My Game!")
        self.assertTrue(new_id.startswith("my-game-"))
        self.assertNotEqual(new_id, "game-111111")
        self.assertEqual(slugify("  Hello, World!  "), "hello-world")

    def test_invalid_json_and_schema(self) -> None:
        self.path.parent.mkdir(parents=True)
        self.path.write_text("{not json")
        with self.assertRaises(RegistryError):
            Registry(self.path).load()
        self.path.write_text(json.dumps({"schema": 99, "apps": []}))
        with self.assertRaises(RegistryError):
            Registry(self.path).load()
        self.path.write_text(json.dumps([]))
        with self.assertRaises(RegistryError):
            Registry(self.path).load()
        self.path.write_text(json.dumps({"schema": 1, "apps": {}}))
        with self.assertRaises(RegistryError):
            Registry(self.path).load()

    def test_entry_validation(self) -> None:
        with self.assertRaises(RegistryError):
            AppEntry.from_dict({"id": "x"})
        bad = make_entry().to_dict()
        bad["kind"] = "exe"
        with self.assertRaises(RegistryError):
            AppEntry.from_dict(bad)
        bad = make_entry().to_dict()
        bad["backend"] = "wine"
        with self.assertRaises(RegistryError):
            AppEntry.from_dict(bad)
        bad = make_entry().to_dict()
        bad["args"] = "not-a-list"
        with self.assertRaises(RegistryError):
            AppEntry.from_dict(bad)
        bad = make_entry().to_dict()
        bad["env"] = ["A=1"]
        with self.assertRaises(RegistryError):
            AppEntry.from_dict(bad)

    def test_reload_picks_up_external_changes(self) -> None:
        self.assertEqual(len(self.registry), 0)
        other = Registry(self.path)
        other.add(make_entry())
        self.assertEqual(len(self.registry), 0)
        self.registry.reload()
        self.assertEqual(self.registry.get("Demo").id, "demo-aaa111")

    def test_empty_registry(self) -> None:
        self.assertEqual(self.registry.apps, [])
        self.assertEqual(len(self.registry), 0)


if __name__ == "__main__":
    unittest.main()
