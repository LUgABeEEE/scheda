# SPDX-License-Identifier: AGPL-3.0-or-later
"""``scheda search``, ``scheda read`` and the ``scheda skills`` actions."""
from __future__ import annotations

import json
import sys

from ..service import read_payload, search_with_refresh

from .common import _config, _emit, _log_to


def cmd_search(args) -> int:
    cfg = _config(args)
    allow_refresh = not args.no_auto_refresh
    payload = search_with_refresh(
        cfg,
        args.query,
        zone=args.zone,
        limit=args.number,
        snippet_chars=args.snippet,
        allow_refresh=allow_refresh or args.refresh,
        on_event=_log_to(cfg, args.quiet),
    )
    if args.refresh and not payload["hits"]:
        pass  # force-refresh already happened; nothing more to try

    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0

    _emit(f"query: {payload['query']}  zones={','.join(payload['zones'])}  {payload['note']}",
          quiet=args.quiet)
    if not payload["hits"]:
        print("(no hits)")
        return 1
    for index, hit in enumerate(payload["hits"], 1):
        print(f"[{index}] {hit['path']}:{hit['line']}  score={hit['score']}")
        print(f"    {hit['snippet']}")
    return 0


def cmd_read(args) -> int:
    cfg = _config(args)
    try:
        payload = read_payload(cfg, args.path, start=args.start, lines=args.lines)
    except FileNotFoundError as exc:
        _emit(f"scheda: {exc}")
        return 2
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0
    print(f"--- {payload['path']}  L{payload['start']}-{payload['end']} / {payload['total']} ---")
    for entry in payload["lines"]:
        print(f"{entry['n']:5d}| {entry['text']}")
    return 0


def cmd_skills_search(args) -> int:
    """搜索 `z3-skills/` 资产区里的 skill 总库（转发库自带检索）。"""
    from ..skills import SkillsUnavailable, search as skills_search

    try:
        rows = skills_search(" ".join(args.query), top=args.number, kind=args.kind,
                             verdict=args.verdict, risky_only=args.risky,
                             cfg=_config(args))
    except SkillsUnavailable as exc:
        print(f"scheda: {exc}", file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(rows, ensure_ascii=False, indent=1))
        return 0
    if not rows:
        print("  库里没有匹配的 skill")
        return 0
    print(f"  命中 {len(rows)} 条")
    for row in rows:
        print(f"  ▸ {row.get('name')}  [{row.get('verdict')}·{row.get('kind')}]  "
              f"{row.get('score')}分  {row.get('size_kb')}KB  {row.get('license') or '—'}")
        brief = row.get("brief") or row.get("zh") or ""
        if brief:
            print(f"      {str(brief)[:96]}")
        if row.get("deps"):
            print(f"      依赖: {str(row['deps'])[:70]}")
    return 0


def cmd_skills_show(args) -> int:
    """打印一条 skill 的开头若干行（正文在资产区的 `z3-skills/` 里）。"""
    from ..skills import SkillsUnavailable, read_skill

    try:
        rel, text = read_skill(args.name, lines=args.lines, cfg=_config(args))
    except SkillsUnavailable as exc:
        print(f"scheda: {exc}", file=sys.stderr)
        return 2
    print(f"# {rel}\n")
    print(text)
    return 0


def cmd_skills_stats(args) -> int:
    """库规模、verdict 分布与许可风险（无模型调用）。"""
    from ..skills import SkillsUnavailable, stats as skills_stats

    try:
        info = skills_stats(cfg=_config(args))
    except SkillsUnavailable as exc:
        print(f"scheda: {exc}", file=sys.stderr)
        return 2
    print(f"library : {info['library']}")
    print(f"count   : {info['count']} skills · {info['bytes'] / 1048576:.1f} MB")
    print(f"verdict : {info['verdicts']}")
    print(f"kinds   : {info['kinds']}")
    print(f"许可风险: {info['license_risk']} 条（manifest 标 license_risk；含「有声明缺许可文件」档，详见库内 LICENSE-RISK.md）")
    if info.get("updated"):
        print(f"updated : {info['updated']}")
    return 0
