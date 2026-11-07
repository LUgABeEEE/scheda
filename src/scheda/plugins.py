# SPDX-License-Identifier: AGPL-3.0-or-later
"""In-process extension points — the only place a governance layer mounts.

Why this module exists
----------------------
Everything an enterprise deployment needs to change about *who may see what*
lives on the wrong side of a fork today: the audit trail, the identity of the
caller and the zone policy are all built into :mod:`scheda.service` /
:mod:`scheda.audit`.  A vendor that needs "reads from the finance share must be
rejected" therefore copies the tree, and the copy drifts — two months later the
fork is missing a security fix because nobody re-ran the diff.

The extension surface is therefore deliberately **three narrow protocols**, not
a plugin API:

* :class:`AuthProvider` — one method, maps the call context to an actor name;
* :class:`AuditSink` — one method, receives the audit record that was just
  written locally (a *forwarder*, not a replacement);
* :class:`ZonePolicy` — one method, vetoes a path for an action, or returns
  ``None`` to leave the decision to the built-in logic.

Anything wider would become a second configuration system.  Protocols are
structural: an implementation is detected with ``hasattr``, never with
``isinstance``, so a governance module does not have to import this package's
internals (or even agree on its version) to plug in.

Discovery
---------
``importlib.metadata.entry_points(group="scheda.plugins")`` — the standard
packaging mechanism.  Anything installed under that group is a candidate:

.. code-block:: toml

    [project.entry-points."scheda.plugins"]
    acme-governance = "acme_scheda.governance"

The loaded object may be a module or any object; whichever protocols it
implements get wired into the matching slot.  First provider wins: with two
audit sinks installed, the one that sorts first in the entry-point list is the
one that receives records — deterministic, and a second sink is a configuration
mistake rather than a feature.

Failure is harmless, by contract
--------------------------------
A plugin that raises on import is *reported and skipped*: one line on ``stderr``
(``scheda: plugin <name> failed to load: <err>``) and the built-in behaviour for
that slot.  A governance layer that is half-installed must not take the
knowledge base down with it — the same reasoning as "a full disk must not stop
a search" in :mod:`scheda.audit`.

``SCHEDA_PLUGINS=off`` (also ``0`` / ``false`` / ``no``) disables discovery
entirely, including the import attempt.  That is the troubleshooting switch and
the byte-for-byte-compatibility switch: with it set, this module returns
:data:`EMPTY` and ``service`` behaves exactly as it did before extensions
existed.
"""
from __future__ import annotations

import importlib.metadata as _metadata
import os
import sys
import threading
from dataclasses import dataclass
from typing import Protocol

#: The entry-point group.  Stable public name: rename it and every installed
#: governance package silently stops loading.
ENTRY_POINT_GROUP = "scheda.plugins"

#: Process-wide off switch (``off`` / ``0`` / ``false`` / ``no``).
SWITCH_ENV = "SCHEDA_PLUGINS"

#: Pre-rename spelling, still honoured for deployments configured before 0.9.0
#: (same policy as :func:`scheda.config._env`).
LEGACY_SWITCH_ENV = "DSH_KB_PLUGINS"

#: Values that mean "off".  Matches :func:`scheda.config._env_flag`, so the
#: environment variable and ``Config.plugins_enabled`` cannot disagree.
_OFF_VALUES = ("0", "false", "no", "off")


# --------------------------------------------------------------------------- #
# The three protocols
# --------------------------------------------------------------------------- #
class AuthProvider(Protocol):
    """Who is calling.

    ``identify`` receives at least ``{"transport", "path", "action"}`` and
    returns the actor name that lands in the audit record.  An empty string (or
    a non-string) means "no opinion" and the built-in default (``local``) stands,
    so a provider that only knows about ``web`` callers does not have to invent
    a name for CLI reads.
    """

    def identify(self, context: dict) -> str:
        ...


class AuditSink(Protocol):
    """Where audit records are forwarded.

    ``record`` is the **exact** dict written to ``audit.jsonl`` — same keys, same
    order, same values (including the second-resolution ``ts``).  A sink is a
    forwarder: the local file is still written, because existing tooling
    (``jq``, log shipping, the tests that assert the file exists) depends on it.
    """

    def emit(self, record: dict) -> None:
        ...


class ZonePolicy(Protocol):
    """Whether a bundle-relative path may take part in an action.

    ``action`` is one of ``mirror`` / ``index`` / ``query``.  Return ``False``
    to deny, ``True`` to allow, ``None`` to hand the decision back to the
    built-in :class:`scheda.config.Config` logic.

    Only a literal ``False`` denies.  ``None`` (and anything else) falls
    through, so a policy can be written as "I only care about these paths"
    without reimplementing zone handling for the rest.
    """

    def allow(self, rel: str, action: str) -> bool | None:
        ...


# --------------------------------------------------------------------------- #
# The bundle
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Plugins:
    """What discovery found.  Every slot is ``None`` in a stock install."""

    auth: AuthProvider | None = None
    audit: AuditSink | None = None
    zone: ZonePolicy | None = None
    #: Entry points that loaded successfully, in discovery order.
    names: tuple[str, ...] = ()
    #: Entry points that raised while loading (reported on stderr, then skipped).
    failed: tuple[str, ...] = ()


#: The bundle used when nothing is installed or discovery is switched off.
EMPTY = Plugins()

_CACHE: Plugins | None = None
_LOCK = threading.Lock()


# --------------------------------------------------------------------------- #
# Discovery
# --------------------------------------------------------------------------- #
def disabled() -> bool:
    """True when ``SCHEDA_PLUGINS`` (or its pre-rename spelling) says "off"."""
    for name in (SWITCH_ENV, LEGACY_SWITCH_ENV):
        raw = os.environ.get(name)
        if raw is None:
            continue
        return raw.strip().lower() in _OFF_VALUES
    return False


def load() -> Plugins:
    """Load (and cache) the installed extensions.

    Never raises: a broken entry point costs one stderr line and that plugin's
    slot only.  The result is cached for the process lifetime — discovery hits
    the filesystem and imports third-party code, which is not something to
    repeat per search.  Call :func:`reset_cache` after installing or removing a
    plugin (tests do this; a long-running process would need a restart).
    """
    global _CACHE
    cached = _CACHE
    if cached is not None:
        return cached
    with _LOCK:
        if _CACHE is None:
            _CACHE = EMPTY if disabled() else _discover()
        return _CACHE


def reset_cache() -> None:
    """Forget the cached bundle.  Intended for tests and for tooling that
    installs plugins at runtime."""
    global _CACHE
    with _LOCK:
        _CACHE = None


def _discover() -> Plugins:
    found: dict[str, object] = {"auth": None, "audit": None, "zone": None}
    names: list[str] = []
    failed: list[str] = []

    for entry in _candidates():
        name = str(getattr(entry, "name", "") or "?")
        try:
            target = entry.load()
        except Exception as exc:  # noqa: BLE001 - a plugin must not take the app down
            _report(name, exc)
            failed.append(name)
            continue
        names.append(name)
        # Structural detection on purpose: `isinstance(obj, AuditSink)` would
        # require runtime_checkable + an import of this module's internals.
        for slot, attr in (("auth", "identify"), ("audit", "emit"), ("zone", "allow")):
            if found[slot] is None and callable(getattr(target, attr, None)):
                found[slot] = target

    return Plugins(
        auth=found["auth"],          # type: ignore[arg-type]
        audit=found["audit"],        # type: ignore[arg-type]
        zone=found["zone"],          # type: ignore[arg-type]
        names=tuple(names),
        failed=tuple(failed),
    )


def _candidates() -> list:
    """The entry points in this group, or ``[]`` when the lookup itself fails."""
    try:
        return list(_metadata.entry_points(group=ENTRY_POINT_GROUP))
    except Exception as exc:  # noqa: BLE001 - a broken metadata cache is not fatal
        _report(ENTRY_POINT_GROUP, exc)
        return []


def _report(name: str, exc: BaseException) -> None:
    """One line on stderr, never an exception.  Format is a contract:
    ``scheda: plugin <name> failed to load: <err>``."""
    detail = str(exc).strip() or type(exc).__name__
    try:
        sys.stderr.write(f"scheda: plugin {name} failed to load: {detail}\n")
        sys.stderr.flush()
    except Exception:  # noqa: BLE001 - stderr unavailable: nowhere left to report
        pass


__all__ = [
    "AuditSink",
    "AuthProvider",
    "EMPTY",
    "ENTRY_POINT_GROUP",
    "LEGACY_SWITCH_ENV",
    "Plugins",
    "SWITCH_ENV",
    "ZonePolicy",
    "disabled",
    "load",
    "reset_cache",
]
