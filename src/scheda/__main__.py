# SPDX-License-Identifier: AGPL-3.0-or-later
"""``python -m scheda`` entry point."""

from .cli import main

if __name__ == "__main__":
    raise SystemExit(main())
