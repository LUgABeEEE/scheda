# SPDX-License-Identifier: AGPL-3.0-or-later
"""Compatibility shim for the rename to :mod:`scheda` (0.9.0).

Old code says ``import dsh_kb`` and ``from dsh_kb.cli import main``.  Both keep
working: the names are mapped onto the real modules instead of being copied, so
there is exactly one implementation and no chance of the shim drifting from it.

New code should import ``scheda``.  Deprecation is a warning, not an error — a
rename should not break a deployment mid-flight.
"""
from __future__ import annotations

import importlib
import sys
import warnings

import scheda as _scheda

__version__ = _scheda.__version__
__all__ = getattr(_scheda, "__all__", ["__version__"])

#: Every submodule worth importing by its old path.  ``sys.modules`` is the only
#: way to make ``from dsh_kb.cli import main`` resolve without shipping copies.
_SUBMODULES = (
    "cli", "config", "service", "index", "mirror", "sources", "mcp_server",
    "webui", "audit", "platform_fs", "skills", "expansion", "ragvec",
    "ragcheck", "supersede", "image_embed",
)

for _name in _SUBMODULES:
    try:
        sys.modules[f"{__name__}.{_name}"] = importlib.import_module(f"scheda.{_name}")
    except ImportError:  # an optional module missing from this build
        pass


def __getattr__(name: str):
    # Anything else the old package exposed (constants, helper functions).
    return getattr(_scheda, name)


warnings.warn(
    "dsh_kb was renamed to scheda in 0.9.0; `import dsh_kb` still works but new "
    "code should `import scheda`",
    DeprecationWarning,
    stacklevel=2,
)
