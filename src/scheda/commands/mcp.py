# SPDX-License-Identifier: AGPL-3.0-or-later
"""``scheda mcp`` — run the MCP server on stdio."""
from __future__ import annotations

from .common import _config


def cmd_mcp(args) -> int:
    """Run the MCP server on stdio.

    Intended for an agent host (the host speaks JSON-RPC on this process's stdin
    and stdout), not for a human.  Nothing may be printed to stdout here.
    """
    from ..mcp_server import serve

    return serve(_config(args))
