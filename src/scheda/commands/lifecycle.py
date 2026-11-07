# SPDX-License-Identifier: AGPL-3.0-or-later
"""``scheda diff`` — the read-only health report."""
from __future__ import annotations

import json
import sys

from ..service import DiffUnavailable, diff_payload

from .common import _config


# --------------------------------------------------------------------------- #
# diff (read-only health report)
# --------------------------------------------------------------------------- #
#: attention 代码 → 中文短语。JSON 里保持稳定的英文代码，人读时翻译。
ATTENTION_LABELS = {
    "source-unavailable": "源不可用",
    "mirror-gap": "镜像缺口",
    "index-drift": "索引漂移",
    "incoming-backlog": "提案积压",
    "conflict-copy": "冲突副本",
    "mirror-escape": "镜像越界",
}


def _fmt_bytes(value) -> str:
    if not isinstance(value, (int, float)):
        return "大小未知"
    if value < 1024:
        return f"{int(value)} B"
    if value < 1048576:
        return f"{value / 1024:.1f} KB"
    return f"{value / 1048576:.1f} MB"


def _fmt_age(seconds) -> str:
    """把秒数说成"人能换算"的量级；未知就说未知，不猜。"""
    if not isinstance(seconds, (int, float)):
        return "未知"
    if seconds < 90:
        return f"{seconds:.0f} 秒"
    if seconds < 5400:
        return f"{seconds / 60:.0f} 分钟"
    if seconds < 172800:
        return f"{seconds / 3600:.1f} 小时"
    return f"{seconds / 86400:.1f} 天"


def _signed_age(seconds) -> str:
    if not isinstance(seconds, (int, float)):
        return "mtime 未知"
    sign = "+" if seconds >= 0 else "-"
    return f"mtime {sign}{_fmt_age(abs(seconds))}"


def _describe_change(mark: str, item: dict) -> str:
    path = item["path"]
    if mark == "+":
        return f"{path}  {_fmt_bytes(item.get('size'))}"
    if mark == "-":
        return f"{path}  上次 {_fmt_bytes(item.get('previous_size'))}"
    delta = item.get("size_delta")
    delta_text = f"{delta:+d} B" if isinstance(delta, int) else "大小未知"
    return (f"{path}  {_fmt_bytes(item.get('previous_size'))} → "
            f"{_fmt_bytes(item.get('size'))}（{delta_text}），"
            f"{_signed_age(item.get('mtime_delta'))}")


def _describe_gap(mark: str, item: dict) -> str:
    """镜像侧的一条差异：``size`` 是本地大小，``previous_size`` 是快照记录的大小。

    与 :func:`_describe_change` 分开是因为 mtime 在这边不可比（镜像是本机写入
    时间），写"mtime 未知"只会是噪音。
    """
    path = item["path"]
    if mark == "+":
        return f"{path}  本地 {_fmt_bytes(item.get('size'))}"
    if mark == "-":
        return f"{path}  快照记录 {_fmt_bytes(item.get('previous_size'))}"
    return (f"{path}  快照 {_fmt_bytes(item.get('previous_size'))} → "
            f"本地 {_fmt_bytes(item.get('size'))}")


def _emit_diff(payload: dict) -> None:
    """人读版 diff：每组一小节，异常组后面直接跟"下一步做什么"。"""
    src = payload["source"]
    print(f"scheda diff · {payload['home']}")
    print(f"  源     : {src['kind'] or '—'} {src['location']}  "
          f"[{'可用' if src['available'] else '不可用'}]")
    if src["note"]:
        print(f"           ! {src['note']}")

    content = payload["content"]
    last_sync = content.get("last_sync")
    if last_sync:
        print(f"  快照   : {content['manifest']}（上次同步 {last_sync}，"
              f"{_fmt_age(content.get('manifest_age_seconds'))}前）")
    else:
        print(f"  快照   : {content['manifest']}（还没有同步过）")

    # 1. 内容差异
    print("\n内容差异（远端 vs 上次快照）")
    counts = content["counts"]
    if content["note"]:
        print(f"  ! {content['note']}")
    elif not any(counts.values()):
        print("  ok 没有差异：远端与上次快照一致")
    else:
        # Markdown 与非 Markdown 分开列：日常的图片/JSON 变动不该把 .md 的
        # 差异挤出屏幕。
        for kind, label in (("markdown", "Markdown（.md，会进索引）"),
                            ("other", "其他文件（只镜像，不进索引）")):
            group = content.get(kind) or {}
            kind_counts = group.get("counts") or {}
            if not any(kind_counts.values()):
                continue
            print(f"  ── {label}: 新增 {kind_counts.get('added', 0)} / "
                  f"消失 {kind_counts.get('removed', 0)} / "
                  f"变更 {kind_counts.get('changed', 0)}")
            for mark, key, name in (("+", "added", "新增"), ("-", "removed", "消失"),
                                    ("~", "changed", "变更")):
                items = group.get(key) or []
                if not items:
                    continue
                total = kind_counts.get(key, len(items))
                tail = f"（样例 {len(items)}/{total}，--json 有全量列表）" if total > len(items) else ""
                print(f"    {mark} {name} {total} 个{tail}")
                for item in items:
                    print(f"        {_describe_change(mark, item)}")
        print("  提示：内容差异本身不算异常，下一次 `scheda sync` 就会收敛")

    # 2. 镜像副本（本地 vs 快照）：t7 盲点 B —— 删掉已同步的文件必须看得见
    gaps = payload["mirror_gaps"]
    gap_counts = gaps["counts"]
    print(f"\n镜像副本（本地镜像 vs 上次快照，快照记录 {gaps['snapshot_files']} 个文件，"
          f"本地 {gaps['mirror_files']} 个）")
    if gaps.get("note"):
        print(f"  ! {gaps['note']}")
    if not any(gap_counts.values()):
        print("  ok 本地镜像没有缺口")
    else:
        def _gap_block(code: str, mark: str, heading: str, hint: str) -> None:
            total = gap_counts.get(code, 0)
            if not total:
                return
            md = (gaps.get("markdown") or {}).get(code) or []
            other = (gaps.get("other") or {}).get(code) or []
            print(f"  {'!!' if code != 'added' else '~~'} {heading} {total} 个"
                  f"（Markdown {len(md)} / 其他 {len(other)}）")
            for item in (md + other)[:8]:
                print(f"      {mark} {_describe_gap(mark, item)}")
            if total > 8:
                print(f"      … 其余 {total - 8} 个（--json 看样例列表）")
            if hint:
                print(f"     {hint}")

        _gap_block("removed", "-", "本地缺文件（快照里有、镜像里没有）",
                   "建议：`scheda sync` 会把这些文件补回来")
        _gap_block("changed", "~", "本地文件大小与快照不符（可能被截断/改坏）",
                   "建议：`scheda sync --full`（增量 sync 认为远端没变，不会重取）")
        _gap_block("added", "+", "本地多出文件（快照与源都不认识）",
                   "建议：确认无用后手工删除 —— `sync --prune` 只删「快照有、远端没有」"
                   "的文件，删不掉这些")

    # 3. 索引漂移
    idx = payload["index"]
    print("\n索引")
    if not idx["present"] and not idx["drift"]:
        # 全新 home：没索引是因为压根没有可索引的内容，别报成异常。
        print(f"  ok 还没有索引，镜像里也没有可索引的 .md（{idx['path']}）")
    elif not idx["present"]:
        print(f"  !! 索引不存在：{idx['path']}")
        print(f"     建议：`scheda index` 建一次（镜像里现有 {idx['mirror_pages']} 个可索引 .md）")
    elif idx["drift"]:
        print(f"  !! 漂移：索引 meta 里 {idx['meta_paths']} 个路径，镜像里可索引 .md "
              f"{idx['mirror_pages']} 个（索引 {_fmt_age(idx['age_seconds'])}前）")
        print("     建议：`scheda index` 重建（或 `scheda refresh` 同步+重建一次）")
    else:
        print(f"  ok 一致：{idx['meta_paths']} 个路径，索引 {_fmt_age(idx['age_seconds'])}前")

    # 4. _incoming 积压
    inc = payload["incoming"]
    basis = "共享盘" if inc["basis"] == "source" else "本地镜像"
    print(f"\n_incoming 积压（来源 {basis}，阈值 {inc['threshold_seconds'] / 86400:.1f} 天）")
    if inc["note"]:
        print(f"  ! {inc['note']}")
    if not inc["files"]:
        print("  ok 没有待处理的提案")
    elif inc["stale"]:
        print(f"  !! {inc['files']} 份提案，最老的一份 {_fmt_age(inc['oldest_seconds'])}前"
              f"（{inc['oldest']}）")
        print(f"     建议：`scheda read {inc['oldest']}` 看内容，转正到 z0/z1 或删掉")
    else:
        print(f"  ok {inc['files']} 份提案，最老的一份 {_fmt_age(inc['oldest_seconds'])}前（阈值内）")

    # 5. 冲突副本
    conf = payload["conflicts"]
    print(f"\n冲突副本（源上 {conf['pattern']}）")
    if conf["note"]:
        print(f"  ! {conf['note']}")
    elif not conf["count"]:
        print("  ok 没有冲突副本")
    else:
        print(f"  !! {conf['count']} 个：")
        for rel in conf["files"][:8]:
            print(f"      {rel}")
        if conf["count"] > 8:
            print(f"      … 其余 {conf['count'] - 8} 个")
        print("     建议：人工比对后只留一份，删掉带 .conflict- 的那份（别让它被当成正式文档）")

    # 6. 镜像越界
    esc = payload["escapes"]
    print("\n镜像越界（当前 zones/never 策略排除、却仍在镜像里的路径）")
    if not esc["count"]:
        print("  ok 没有越界文件")
    else:
        print(f"  !! {esc['count']} 个：")
        for item in esc["files"][:8]:
            why = ("策略收紧前同步进来的" if item["in_manifest"]
                   else "不是 sync 放进来的（手工拷贝？）")
            print(f"      {item['path']}  {_fmt_bytes(item['bytes'])}  {why}")
        print("     建议：确认无用后删掉；若是策略误伤，改 config.json 的 zones/never 再 "
              "`scheda sync --prune`")

    # 结论
    attention = payload["attention"]
    if not src["available"] and "source-unavailable" not in attention:
        # 全新 home + 没有源：没有快照可对比，也就没有"差异"可报。配置问题由
        # doctor 负责喊（它返回 1），这里说清楚为什么不重复报警。
        print("\n注：源不可用，但本地还没有任何快照/镜像 —— 配置问题请跑 `scheda doctor`"
              "（它返回 1），这里不重复报警")
    if attention:
        names = "、".join(ATTENTION_LABELS.get(code, code) for code in attention)
        print(f"\n结论：{len(attention)} 项需要注意（{names}）—— 退出码 10")
    else:
        print("\n结论：没有需要注意的项 —— 退出码 0")


def cmd_diff(args) -> int:
    """Read-only health diff: what changed, what is drifting, what is piling up.

    只读是硬约束：不写源、不写镜像、不触发 ``sync``、不重建索引 —— 这个命令会
    被巡检脚本按天调用，任何写动作都会把"看一眼"变成风险源。（它**读**源清单：
    源可用时会遍历远端列出差异，但只读。）

    两个方向、五组结果：

    * ``content``（远端 vs 上次快照）—— 源上变了什么。只报告，**不算异常**：
      下一次 ``sync`` 就会收敛。``markdown`` / ``other`` 把 Markdown 与非
      Markdown 的增/删/改分开计数。
    * ``mirror_gaps``（本地镜像 vs 快照/源清单）—— 本地缺了/多了/大小不符。
      ``removed`` / ``changed`` 算异常：索引里指向的页面可能已经读不到了。
      比对域覆盖**全部文件**，不只 ``.md``（2026-10-19 t7 审查：删掉
      ``mirror/z0-public/pic.png`` 后旧版 diff 报 0，而 sync 立刻 fetched=1）。
    * ``index`` / ``incoming`` / ``conflicts`` / ``escapes`` —— 与原来一致。

    退出码 0 / 10 / 11 三档，语义未变：0 干净；10 有需要人处理的项（源不可用、
    镜像缺口、索引漂移、积压、冲突副本、越界）；11 本地状态读不出来
    （home/mirror 形状不对、manifest 坏了）。10 与 11 分开，监控才能区分"有活
    要干"和"这台机器坏了"。
    """
    cfg = _config(args)
    days = float(getattr(args, "incoming_days", 7.0))
    threshold = max(0.0, days) * 86400
    try:
        payload = diff_payload(cfg, incoming_threshold=threshold)
    except DiffUnavailable as exc:
        print(f"scheda: {exc}", file=sys.stderr)
        return 11
    except Exception as exc:  # noqa: BLE001 - 诊断命令不该以 traceback 收场
        print(f"scheda: diff failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 11

    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        _emit_diff(payload)
    return 10 if payload["attention"] else 0
