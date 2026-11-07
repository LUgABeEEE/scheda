# SPDX-License-Identifier: AGPL-3.0-or-later
"""``scheda status`` — configuration, mirror and index state."""
from __future__ import annotations

import json

from .. import __version__
from ..config import source_available
from ..index import index_age, stats
from ..mirror import manifest_age, mirror_files, mirror_size
from ..platform_fs import describe_path

from .common import _config


def cmd_status(args) -> int:
    cfg = _config(args)
    kind, location = source_available(cfg)
    m_files = mirror_files(cfg)
    idx = stats(cfg)
    m_age, i_age = manifest_age(cfg), index_age(cfg)

    payload = {
        "version": __version__,
        "home": str(cfg.home),
        "source": {"kind": kind, "location": location, "note": "" if kind else location},
        "mirror": {"path": str(cfg.mirror_dir), "files": len(m_files), "bytes": mirror_size(cfg),
                   "age_seconds": m_age},
        "index": {"path": str(cfg.index_db), "pages": idx.pages, "chunks": idx.chunks,
                  "bytes": idx.bytes, "age_seconds": i_age},
        "zones": [{"name": z.name, "index": z.index, "inject": z.inject, "mirror": z.mirror}
                  for z in cfg.zones],
        "never": list(cfg.never),
        "stale_minutes": cfg.stale_minutes,
        "mtime_tolerance": cfg.mtime_tolerance,
    }
    # Where does the bundle actually live?  A Samba/CIFS mount behaves
    # differently from a local directory, so surface that explicitly.
    if kind in ("local", "mount"):
        payload["source"].update(describe_path(location))
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0

    def age(value):
        return "never" if value is None else f"{value:.0f}s ago"

    print(f"scheda {__version__}")
    print(f"  home    : {cfg.home}")
    print(f"  source  : {kind or '—'}  {location}")
    if payload["source"].get("mountpoint"):
        flags = []
        if payload["source"].get("network"):
            flags.append(payload["source"].get("fstype", "network"))
        if payload["source"].get("readonly"):
            flags.append("read-only")
        suffix = f"  [{', '.join(flags)}]" if flags else ""
        print(f"  mount   : {payload['source'].get('device', '')} at "
              f"{payload['source']['mountpoint']}{suffix}")
    print(f"  mirror  : {len(m_files)} files, {mirror_size(cfg)} B, synced {age(m_age)}")
    print(f"  index   : {idx.pages} pages / {idx.chunks} chunks, {idx.bytes} B, built {age(i_age)}")
    print(f"  zones   : " + ", ".join(f"{z.name}{'' if z.mirror else ' (remote-only)'}" for z in cfg.zones))
    return 0
