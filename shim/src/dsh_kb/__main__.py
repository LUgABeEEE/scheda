# SPDX-License-Identifier: AGPL-3.0-or-later
"""``python -m dsh_kb`` — the old module entry point, forwarded to ``scheda``.

This matters more than it looks: real deployments wire the MCP server as
``python3 -m dsh_kb mcp --home …`` inside a host config file, and that string is
not updated by ``pip install -U``.  Without this module the command fails with
``No module named dsh_kb.__main__`` — an upgrade that breaks an agent's tool
server is exactly the failure a rename is not allowed to cause.
"""
from scheda.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
