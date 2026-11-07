# SPDX-License-Identifier: AGPL-3.0-or-later
"""``scheda refresh`` — sync + index, the unit a scheduler should call."""
from __future__ import annotations

import json

from ..service import refresh as service_refresh

from .common import _config, _log_to


def cmd_refresh(args) -> int:
    """sync + index in one shot — the unit a scheduler should call.

    刻意走用例层：那里有"内容没变就不重建索引"的判定。此前这里直接 `sync()+build()`，
    于是**每次定时刷新都要重写整个索引**（本库 3.4 MB），与"按需调用的轻量补充"
    这个定位相悖。同一件事只应有一处实现。
    """
    cfg = _config(args)
    emit = _log_to(cfg, args.quiet)
    result, built = service_refresh(cfg, prune=args.prune,
                                    full=getattr(args, "full", False), on_event=emit)
    if result.fetched or result.removed:
        emit(f"refresh: {result.summary} → {built.summary}")
    else:
        emit(f"refresh: {result.summary} → index unchanged (rebuild skipped)")
    if args.json:
        print(json.dumps({"sync": {"fetched": result.fetched, "unchanged": result.skipped,
                                   "excluded": len(result.excluded), "seconds": round(result.duration, 3)},
                          "index": {"pages": built.pages, "chunks": built.chunks,
                                    "bytes": built.bytes, "seconds": round(built.duration, 3)}},
                         ensure_ascii=False, indent=2))
    return 0
