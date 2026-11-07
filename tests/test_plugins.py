"""In-process extension points: discovery, wiring, and the "off" baseline.

The contract this file pins is the one an enterprise deployment pays for:

* a stock install behaves exactly as before (no entry points, no code path);
* a plugin that raises on import is reported and skipped, never fatal;
* the audit sink receives **the same dict** that lands in ``audit.jsonl``, and
  the file is still written (a sink forwards, it does not replace);
* a zone policy can withhold a path, but ``None`` means "as you were";
* ``SCHEDA_PLUGINS=off`` makes the whole discovery pass disappear, which is how
  "this is not a plugin problem" is proven during an incident.

The tests stub ``importlib.metadata.entry_points`` rather than installing a
package: discovery is the only thing that touches packaging metadata, and a
stub keeps the suite runnable from a bare clone (no pip, no network).
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from scheda import audit, index, plugins, service  # noqa: E402
from scheda.config import Config, load_config  # noqa: E402

#: Two indexable pages, one per default zone.  The latin sentinels are the probe
#: queries; they survive FTS tokenisation exactly like the e2e fixture does.
PAGES = {
    "z0-public/mount.md": "# 挂载点失效的处置\n\n唯一标识 UNIQ-Z0-ALPHA-11111\n",
    "z1-restricted/delivery.md": "# 交付偏好\n\n唯一标识 UNIQ-Z1-BRAVO-22222\n",
}

Z0_QUERY = "UNIQ-Z0-ALPHA-11111"
Z1_QUERY = "UNIQ-Z1-BRAVO-22222"


# --------------------------------------------------------------------------- #
# stubs — structural, not isinstance-based, exactly like the loader
# --------------------------------------------------------------------------- #
class _EntryPoint:
    """The two attributes the loader uses: ``name`` and ``load()``."""

    def __init__(self, name: str, target):
        self.name = name
        self.value = f"{name} = stub:{name}"
        self.target = target

    def load(self):
        if isinstance(self.target, BaseException):
            raise self.target
        return self.target


class _RecordingSink:
    def __init__(self):
        self.records: list[dict] = []

    def emit(self, record: dict) -> None:
        self.records.append(record)


class _ExplodingSink:
    def emit(self, record: dict) -> None:
        raise RuntimeError("sink down")


class _FixedActor:
    def __init__(self, name: str = "alice"):
        self.name = name
        self.contexts: list[dict] = []

    def identify(self, context: dict) -> str:
        self.contexts.append(context)
        return self.name


class _PrefixPolicy:
    """Denies one prefix; every other path gets ``None`` (built-in logic)."""

    def __init__(self, prefix: str, verdict=False):
        self.prefix = prefix
        self.verdict = verdict
        self.calls: list[tuple[str, str]] = []

    def allow(self, rel: str, action: str):
        self.calls.append((rel, action))
        return self.verdict if rel.startswith(self.prefix) else None


class _AllThree:
    """One object implementing all three protocols at once."""

    def __init__(self):
        self.records: list[dict] = []

    def identify(self, context: dict) -> str:
        return "carol"

    def emit(self, record: dict) -> None:
        self.records.append(record)

    def allow(self, rel: str, action: str):
        return None


class _ShapeLess:
    """Installed, loads fine, provides no protocol: visible in ``names`` only."""


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
@contextlib.contextmanager
def installed(*stubs):
    """Pretend these ``(name, target)`` entry points are installed.

    The loader caches its result for the process, so every stub window resets
    the cache on the way in and on the way out.
    """
    entries = [_EntryPoint(name, target) for name, target in stubs]
    with mock.patch("importlib.metadata.entry_points", return_value=entries):
        plugins.reset_cache()
        try:
            yield entries
        finally:
            plugins.reset_cache()


def _lines(path: Path) -> list[str]:
    if not path.exists():
        return []
    return [line for line in path.read_text(encoding="utf-8").split("\n") if line]


def _records(path: Path) -> list[dict]:
    return [json.loads(line) for line in _lines(path)]


class _EnvCase(unittest.TestCase):
    """Every case starts from a clean switch and a clean audit rotation."""

    ENV_KEYS = ("SCHEDA_PLUGINS", "DSH_KB_PLUGINS",
                "SCHEDA_LOG_MAX_BYTES", "SCHEDA_LOG_BACKUPS")

    def setUp(self):
        saved = {key: os.environ.pop(key, None) for key in self.ENV_KEYS}
        self.addCleanup(lambda: [os.environ.pop(k, None) for k in self.ENV_KEYS])
        for key, value in saved.items():
            if value is not None:
                self.addCleanup(lambda k=key, v=value: os.environ.__setitem__(k, v))
        self._tmpdir = tempfile.mkdtemp(prefix="scheda-plugins-")
        self.addCleanup(shutil.rmtree, self._tmpdir, True)
        self.tmp = Path(self._tmpdir)
        self.cfg = Config(home=self.tmp / "home")
        plugins.reset_cache()
        self.addCleanup(plugins.reset_cache)


class _IndexedCase(_EnvCase):
    """Adds a real mirror + FTS index so search/read go through the real code."""

    def setUp(self):
        super().setUp()
        self.cfg = Config(home=self.tmp / "home", stale_minutes=15)
        for rel, body in PAGES.items():
            target = self.cfg.mirror_dir / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(body, encoding="utf-8")
        index.build(self.cfg)


# --------------------------------------------------------------------------- #
# A. discovery
# --------------------------------------------------------------------------- #
class TestDiscovery(_EnvCase):
    def test_no_plugins_means_every_slot_is_empty(self):
        with installed():
            bundle = plugins.load()
        self.assertIsNone(bundle.auth)
        self.assertIsNone(bundle.audit)
        self.assertIsNone(bundle.zone)
        self.assertEqual(bundle.names, ())
        self.assertEqual(bundle.failed, ())

    def test_stock_environment_loads_no_real_entry_points(self):
        """A clone that never heard of plugins must discover nothing.

        If this machine really has a ``scheda.plugins`` package installed the
        assertion would be meaningless, so say so instead of passing by luck.
        """
        import importlib.metadata as metadata

        real = [ep.name for ep in metadata.entry_points(group=plugins.ENTRY_POINT_GROUP)]
        if real:
            self.skipTest(f"real plugins installed: {real}")
        plugins.reset_cache()
        self.assertEqual(plugins.load().names, ())

    def test_one_object_can_fill_all_three_slots(self):
        bundle_target = _AllThree()
        with installed(("acme-governance", bundle_target)):
            loaded = plugins.load()
        self.assertIs(loaded.auth, bundle_target)
        self.assertIs(loaded.audit, bundle_target)
        self.assertIs(loaded.zone, bundle_target)

    def test_plugin_name_appears_in_names(self):
        with installed(("acme-governance", _RecordingSink())):
            loaded = plugins.load()
        self.assertEqual(loaded.names, ("acme-governance",))

    def test_import_failure_is_reported_and_harmless(self):
        boom = ImportError("no such module: acme_backend")
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            with installed(("broken-plugin", boom), ("acme-audit", _RecordingSink())):
                loaded = plugins.load()
        self.assertIn("broken-plugin", stderr.getvalue())
        self.assertIn("failed to load", stderr.getvalue())
        self.assertIn("no such module: acme_backend", stderr.getvalue())
        # The broken one is recorded as failed; the healthy one still wired up.
        self.assertEqual(loaded.failed, ("broken-plugin",))
        self.assertEqual(loaded.names, ("acme-audit",))
        self.assertIsNotNone(loaded.audit)

    def test_switch_off_disables_discovery_even_when_installed(self):
        sink = _RecordingSink()
        os.environ["SCHEDA_PLUGINS"] = "off"
        with installed(("acme-audit", sink)):
            loaded = plugins.load()
        self.assertEqual(loaded, plugins.EMPTY)
        self.assertEqual(loaded.names, ())

    def test_object_without_any_protocol_is_ignored(self):
        with installed(("shape-less", _ShapeLess())):
            loaded = plugins.load()
        self.assertIsNone(loaded.auth)
        self.assertIsNone(loaded.audit)
        self.assertIsNone(loaded.zone)
        self.assertEqual(loaded.names, ("shape-less",))


# --------------------------------------------------------------------------- #
# B. audit sink + actor
# --------------------------------------------------------------------------- #
class TestAuditWiring(_EnvCase):
    def test_sink_receives_exactly_the_record_that_is_written(self):
        sink = _RecordingSink()
        with installed(("acme-audit", sink)):
            service.audit_event(self.cfg, "read", path="z0-public/mount.md",
                                transport="web", lines=7)
        written = _records(audit.audit_path(self.cfg))
        self.assertEqual(len(written), 1)
        self.assertEqual(len(sink.records), 1)
        self.assertEqual(sink.records[0], written[0])
        # 键顺序也是契约：企业 sink 按固定列写入自己的审计库。
        self.assertEqual(list(sink.records[0]), list(written[0]))

    def test_local_audit_file_is_still_written(self):
        sink = _RecordingSink()
        with installed(("acme-audit", sink)):
            service.audit_event(self.cfg, "search", zone="all", query="q", hits=2)
        self.assertEqual(len(_lines(audit.audit_path(self.cfg))), 1)
        self.assertEqual(len(sink.records), 1)

    def test_auth_provider_names_the_actor(self):
        actor = _FixedActor("alice")
        with installed(("acme-auth", actor)):
            service.audit_event(self.cfg, "read", path="z0-public/mount.md")
        record = _records(audit.audit_path(self.cfg))[0]
        self.assertEqual(record["actor"], "alice")
        self.assertEqual(actor.contexts[0]["transport"], "cli")
        self.assertEqual(actor.contexts[0]["path"], "z0-public/mount.md")

    def test_explicit_actor_wins_over_the_provider(self):
        with installed(("acme-auth", _FixedActor("alice"))):
            service.audit_event(self.cfg, "read", path="p.md", actor="bob")
        self.assertEqual(_records(audit.audit_path(self.cfg))[0]["actor"], "bob")

    def test_sink_that_raises_does_not_lose_the_file(self):
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            with installed(("acme-audit", _ExplodingSink())):
                service.audit_event(self.cfg, "search", query="q", hits=0)
        self.assertEqual(len(_records(audit.audit_path(self.cfg))), 1)
        self.assertIn("plugin audit failed", stderr.getvalue())

    def test_default_path_writes_the_same_line_as_audit_module(self):
        """No plugins ⇒ ``service.audit_event`` and ``audit.audit_event`` are
        the same bytes (frozen clock, same arguments, relative path)."""
        frozen = "2026-01-01T00:00:00+00:00"
        fields = {"zone": "all", "query": "挂载", "path": "z0-public/mount.md",
                  "hits": 3, "lines": None, "transport": "web"}
        with mock.patch.object(audit, "_now", return_value=frozen):
            service.audit_event(self.cfg, "search", **fields)
        other = Config(home=self.tmp / "other-home")
        with mock.patch.object(audit, "_now", return_value=frozen):
            audit.audit_event(other, "search", **fields)
        self.assertEqual(
            _lines(audit.audit_path(self.cfg)),
            _lines(audit.audit_path(other)),
        )


# --------------------------------------------------------------------------- #
# C. zone policy
# --------------------------------------------------------------------------- #
class TestZonePolicy(_IndexedCase):
    def test_search_hits_from_a_denied_prefix_disappear(self):
        policy = _PrefixPolicy("z1-restricted/")
        with installed(("acme-zone", policy)):
            payload = service.search_payload(self.cfg, Z1_QUERY, zone="z01", audit=False)
        self.assertEqual(payload["hits"], [])
        self.assertEqual(payload["count"], 0)
        self.assertFalse(payload["ok"])
        self.assertIn(service.DENIED_NOTE, payload["note"])
        self.assertTrue(any(rel.startswith("z1-restricted/") for rel, _ in policy.calls))
        self.assertTrue(all(action == "query" for _, action in policy.calls))

    def test_none_verdict_leaves_the_builtin_behaviour_alone(self):
        baseline = service.search_payload(self.cfg, Z1_QUERY, zone="z01", audit=False)
        with installed(("acme-zone", _PrefixPolicy("nothing-matches/"))):
            actual = service.search_payload(self.cfg, Z1_QUERY, zone="z01", audit=False)
        self.assertEqual([h["path"] for h in actual["hits"]],
                         [h["path"] for h in baseline["hits"]])
        self.assertEqual(actual["count"], baseline["count"])
        self.assertNotIn(service.DENIED_NOTE, actual["note"])

    def test_allowed_prefix_still_returns_hits(self):
        with installed(("acme-zone", _PrefixPolicy("z1-restricted/", verdict=None))):
            payload = service.search_payload(self.cfg, Z0_QUERY, zone="z01", audit=False)
        self.assertEqual([h["path"] for h in payload["hits"]], ["z0-public/mount.md"])

    def test_read_of_a_denied_path_raises_permission_error(self):
        with installed(("acme-zone", _PrefixPolicy("z1-restricted/"))):
            with self.assertRaises(PermissionError):
                service.read_payload(self.cfg, "z1-restricted/delivery.md")

    def test_denied_read_is_audited_as_a_refusal(self):
        with installed(("acme-zone", _PrefixPolicy("z1-restricted/"))):
            with self.assertRaises(PermissionError):
                service.read_payload(self.cfg, "z1-restricted/delivery.md")
        record = _records(audit.audit_path(self.cfg))[0]
        self.assertEqual(record["action"], "read")
        self.assertEqual(record["path"], "z1-restricted/delivery.md")
        self.assertEqual(record["lines"], 0)

    def test_allowed_read_still_works(self):
        with installed(("acme-zone", _PrefixPolicy("z1-restricted/"))):
            payload = service.read_payload(self.cfg, "z0-public/mount.md")
        self.assertTrue(payload["ok"])
        self.assertIn("UNIQ-Z0-ALPHA-11111", "".join(row["text"] for row in payload["lines"]))


# --------------------------------------------------------------------------- #
# D. the configuration switch
# --------------------------------------------------------------------------- #
class TestConfigSwitch(_EnvCase):
    def test_plugins_are_enabled_by_default(self):
        self.assertTrue(Config(home=self.tmp / "home").plugins_enabled)
        self.assertTrue(load_config(home=self.tmp / "home").plugins_enabled)

    def test_env_off_reaches_the_config(self):
        os.environ["SCHEDA_PLUGINS"] = "off"
        self.assertFalse(load_config(home=self.tmp / "home").plugins_enabled)

    def test_string_off_in_config_json_is_normalised(self):
        """``"plugins_enabled": "off"`` must mean off, not ``bool("off")``."""
        self.assertFalse(Config(home=self.tmp / "home", plugins_enabled="off").plugins_enabled)
        self.assertFalse(Config(home=self.tmp / "home", plugins_enabled="0").plugins_enabled)
        self.assertTrue(Config(home=self.tmp / "home", plugins_enabled="yes").plugins_enabled)

    def test_config_switch_prevents_the_service_from_loading(self):
        sink = _RecordingSink()
        off = Config(home=self.tmp / "home", plugins_enabled=False)
        with installed(("acme-audit", sink)):
            service.audit_event(off, "search", query="q", hits=0)
        self.assertEqual(sink.records, [])
        self.assertEqual(len(_lines(audit.audit_path(off))), 1)


if __name__ == "__main__":
    unittest.main()
