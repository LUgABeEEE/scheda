# SPDX-License-Identifier: AGPL-3.0-or-later
"""``scheda sync`` and ``scheda index`` — bring the mirror and the index up to date."""
from __future__ import annotations

import json

from ..index import build
from ..mirror import sync
from ..service import open_configured_source as _open_configured_source

from .common import _config, _log_to, cli_seam


def open_configured_source(cfg):
    """Late-bound wrapper: keeps ``mock.patch("scheda.cli...")`` working.

    The real opener is the fallback; when :mod:`scheda.cli` is importable its
    (possibly patched) attribute wins — see :func:`scheda.commands.common.cli_seam`.
    """
    return cli_seam("open_configured_source", _open_configured_source)(cfg)


def cmd_sync(args) -> int:
    cfg = _config(args)
    source = open_configured_source(cfg)
    emit = _log_to(cfg, args.quiet)
    started_source = getattr(source, "label", "?")
    if args.dry_run:
        entries = source.walk()
        files = [e for e in entries if not e.is_dir]
        allowed = [e for e in files if cfg.mirror_allows(e.rel)]
        excluded = [e.rel for e in files if not cfg.mirror_allows(e.rel)]
        print(json.dumps({
            "source": started_source, "files": len(files), "would_mirror": len(allowed),
            "excluded": excluded[:50], "excluded_count": len(excluded),
        }, ensure_ascii=False, indent=2) if args.json else
            f"{started_source}: {len(files)} files, {len(allowed)} mirrorable, {len(excluded)} excluded")
        return 0

    result = sync(cfg, source, prune=args.prune, full=getattr(args, "full", False), on_event=emit)
    emit(f"sync: {result.summary}")
    if args.json:
        print(json.dumps({
            "source": result.source, "dirs": result.dirs, "files": result.files,
            "fetched": result.fetched, "unchanged": result.skipped,
            "excluded": result.excluded, "pruned": result.removed,
            "changed": result.changed, "seconds": round(result.duration, 3),
        }, ensure_ascii=False, indent=2))
    else:
        for rel in result.changed:
            print(f"  + {rel}")
        print(result.summary)
    return 0


def cmd_index(args) -> int:
    cfg = _config(args)
    emit = _log_to(cfg, args.quiet)
    # Incremental is the default; `--full` is the escape hatch for the cases the
    # state file cannot describe (a hand-edited index, a suspected bad delta).
    result = build(cfg, on_event=emit, incremental=not getattr(args, "full", False))
    if args.json:
        print(json.dumps({"pages": result.pages, "chunks": result.chunks,
                          "bytes": result.bytes, "seconds": round(result.duration, 3),
                          "reused": result.reused, "rebuilt": result.rebuilt,
                          "removed": result.removed, "written": result.written},
                         ensure_ascii=False, indent=2))
    return 0
