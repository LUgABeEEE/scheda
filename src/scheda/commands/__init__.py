# SPDX-License-Identifier: AGPL-3.0-or-later
"""Sub-command implementations for :mod:`scheda.cli`.

Every command lives in its own module and exposes ``cmd_<name>(args) -> int``.
``cli.build_parser`` wires them into the argument parser through the registry
below.  The dependency arrow points one way only: ``cli`` imports ``commands``,
never the reverse, and command modules import shared helpers from
:mod:`scheda.commands.common`.
"""
from __future__ import annotations

import argparse
from typing import Callable

#: command name (as typed on the command line) -> its implementation.
REGISTRY: dict[str, Callable[[argparse.Namespace], int]] = {}


def register(name: str, func) -> None:
    """Bind ``name`` to ``func``; registering the same name again replaces it."""
    REGISTRY[name] = func
