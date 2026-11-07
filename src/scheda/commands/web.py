# SPDX-License-Identifier: AGPL-3.0-or-later
"""``scheda web`` — serve the read-only UI."""
from __future__ import annotations

import sys

from .common import _config


def cmd_web(args) -> int:
    """Serve the read-only UI.

    No page content is ever written; ``search`` and ``read`` still leave one
    metadata-only record each in the audit trail (``logs/audit.jsonl``), which is
    the whole point of having an audit trail.
    """
    try:
        from .. import webui
    except ImportError:  # pragma: no cover - only in a stripped build
        print("scheda: this build has no web UI module", file=sys.stderr)
        return 4
    if args.no_rewrite:
        # A browser tab cannot wait out a 12-second local-model rewrite; the CLI
        # default stays untouched because this switch is per process.
        webui._disable_query_rewrite()
    # The non-loopback warning lives in ok = webui.serve(): one place, and it is
    # printed even under --quiet.
    ok = webui.serve(_config(args), host=args.bind, port=args.port, quiet=args.quiet)
    return 0
