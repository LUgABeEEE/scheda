# SPDX-License-Identifier: AGPL-3.0-or-later
"""``scheda init`` — lay out the state directory and a config skeleton."""
from __future__ import annotations

import json
import sys

from ..config import source_available

from .common import _config, _write_json_atomic


_INIT_README = """# scheda state directory

`scheda` keeps everything derived from your bundle here — nothing in this
directory is authoritative, and everything in it can be rebuilt from the source
directory named in `config.json`.

    config.json   what to read, where to cache, which zones are private
    mirror/       a local copy of the bundle (never the source of truth)
    index/        the SQLite FTS5 index (delete it and re-run `scheda index`)
    logs/         text log, structured events, and the audit trail

The privacy zones in `config.json` are enforced in code: a zone with
`"mirror": false` is never copied here at all, so its text cannot leak through
the mirror, the index, or a search result.

Next: `scheda sync` → `scheda index` → `scheda search "keyword"`.
"""


def cmd_init(args) -> int:
    """Lay out the home directory and write a config skeleton.

    Deliberately pulls nothing: `init` is what a stranger runs before deciding
    which source to point at, so it has to be safe on a machine that has no
    share, no NAS and no credentials.  A second run refuses to overwrite an
    existing `config.json` unless `--force` is passed — silently replacing a
    working configuration is the kind of help nobody wants.
    """
    cfg = _config(args)
    home = cfg.home
    cfg_path = cfg.config_path if hasattr(cfg, "config_path") else home / "config.json"
    if cfg_path.is_file() and not getattr(args, "force", False):
        print(f"scheda: {cfg_path} already exists; nothing changed "
              f"(pass --force to rewrite it)", file=sys.stderr)
        return 3

    created = []
    for directory in (home, cfg.mirror_dir, cfg.index_db.parent, cfg.log_path.parent):
        if not directory.exists():
            directory.mkdir(parents=True, exist_ok=True)
            created.append(str(directory))

    payload: dict = {}
    for key in ("source", "mount", "local_root", "smb_share", "webdav_root"):
        value = getattr(args, key, None)
        if value:
            payload[key] = value
    if not payload:
        # `auto` tries mount → local → smbclient → webdav in that order, which is
        # the right default when the caller has not decided yet.
        payload["source"] = "auto"
    _write_json_atomic(cfg_path, payload)

    readme = home / "README.md"
    if not readme.is_file():
        readme.write_text(_INIT_README, encoding="utf-8")

    kind, location = source_available(cfg)
    result = {
        "home": str(home),
        "config": str(cfg_path),
        "config_source": payload.get("source", "auto"),
        "created": created,
        "mirror": str(cfg.mirror_dir),
        "index": str(cfg.index_db),
        "logs": str(cfg.log_path.parent),
        "source": {"kind": kind, "location": location,
                   "available": bool(kind)},
    }
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0

    print(f"scheda: initialized {home}")
    print(f"  config   : {cfg_path}  (source={payload.get('source', 'auto')})")
    print(f"  mirror   : {cfg.mirror_dir}")
    print(f"  index    : {cfg.index_db}")
    print(f"  logs     : {cfg.log_path.parent}")
    if kind:
        print(f"  source   : {kind} → {location}")
    else:
        print("  source   : not resolved yet — set `local_root`, `mount` or "
              "`webdav_root` in config.json (or pass --local-root/--mount)")
    print("next:")
    print("  1. scheda sync           # copy the bundle into the local mirror")
    print("  2. scheda index          # build the FTS5 index")
    print('  3. scheda search "word"  # verify a hit, then wire the MCP server')
    print("  4. scheda web            # optional read-only UI on 127.0.0.1:8765")
    print("     MCP: scheda mcp  (stdio; see docs/agent-integration.md)")
    return 0
