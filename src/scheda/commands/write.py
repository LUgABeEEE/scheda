# SPDX-License-Identifier: AGPL-3.0-or-later
"""``scheda push`` and ``scheda import`` — the only commands that write to the source."""
from __future__ import annotations

import hashlib
import json
from datetime import datetime
from pathlib import Path

from .. import supersede
from ..config import Config
from ..service import open_configured_source as _open_configured_source
from ..sources import SourceError

from .common import _config, _emit, _log_to, cli_seam


def open_configured_source(cfg):
    """Late-bound wrapper: keeps ``mock.patch("scheda.cli...")`` working.

    The real opener is the fallback; when :mod:`scheda.cli` is importable its
    (possibly patched) attribute wins — see :func:`scheda.commands.common.cli_seam`.
    """
    return cli_seam("open_configured_source", _open_configured_source)(cfg)


#: 推送回执账。<home>/state/ 放的都是"可复核的状态"，跟 manifest 同一层。
PUSH_LOG_NAME = "push-log.jsonl"


#: 回执里 result 的全部取值；``refused`` 专指"我们主动不写"。
PUSH_RESULTS = ("ok", "forced", "unchanged", "refused", "failed")


#: 这两个结果说明本次推送没做成，命令因此返回 1。
PUSH_BAD_RESULTS = ("refused", "failed")


def _push_log_path(cfg: Config) -> Path:
    return cfg.home / "state" / PUSH_LOG_NAME


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _timestamp() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _append_push_receipt(cfg: Config, record: dict) -> None:
    """一次尝试恰好追加一行 JSON。

    单次 ``write`` 到 ``O_APPEND`` 打开的文件：并发 push 最多让**行序**交错，
    不会把两行拼成一行。写失败只告警、不中断推送 —— 回执是审计副本，
    不该因为 home 满了就把"文件其实已经写上去"这件事报成失败。
    """
    path = _push_log_path(cfg)
    line = json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(line)
    except OSError as exc:
        _emit(f"scheda: cannot append push receipt to {path}: {exc} — "
              f"check that {path.parent} is writable")


def _normalise_expect(raw: str | None) -> tuple[str | None, str | None]:
    """校验 ``--expect``，返回 ``(小写哈希, 错误说明)``。

    接受大写与 ``sha256:`` 前缀：人从 ``sha256sum`` 或上一次 ``push --json``
    的 ``sha256`` 字段复制过来时，大小写/前缀不该成为拒绝的理由。
    """
    if raw is None:
        return None, None
    text = raw.strip().lower()
    if text.startswith("sha256:"):
        text = text[len("sha256:"):]
    if len(text) != 64 or any(char not in "0123456789abcdef" for char in text):
        return None, (f"--expect needs a 64-hex-digit sha256 (got {raw!r}) — take it "
                      f"from `sha256sum <file>` or the `sha256` field of a previous `push --json`")
    return text, None


def _remote_files(source) -> dict:
    """远端文件清单 ``rel -> Entry``，一次 ``walk()`` 覆盖所有待推文件。

    为什么用 ``walk()`` 而不是 ``read()`` 判存在：``read()`` 在"文件不存在"和
    "文件存在但读不了"上抛的是同一个 ``SourceUnavailable``，拿它判存在会把权限
    问题误判成"远端没有 → 可以覆盖"，正是要堵的那个洞。代价是一次全量列举
    （本库几千个文件；push 是低频人工动作，且换来默认路径上不可能静默覆盖）。
    """
    return {entry.rel: entry for entry in source.walk() if not getattr(entry, "is_dir", False)}


def _plain_incoming_name(name: str) -> bool:
    """``name`` 必须是 ``_incoming/`` 下的**单个文件名**，而不是一条路径。

    这是写入目标的组成部分，所以它属于"能不能写"的一部分：``push ../index.md``
    会让 rel 变成 ``_incoming/../index.md``，落点直接跑到 bundle 顶层 —— 越过
    ``_incoming/`` 的写入不属于这个命令的职责。更糟的是存在性检查查的是**字面**
    路径，于是永远判"远端不存在"：既不比对内容、也不要 ``--force``，还回一句 ok，
    而回执里的 path 与实际落点并不一致（误报）。所以宁可拒绝，也不去"修正"名字。
    绝对路径与 Windows 分隔符一并挡掉：同一份配置在两种系统上都会用。
    """
    if not name or name in (".", ".."):
        return False
    if name.startswith(("/", "\\")):
        return False
    if "/" in name or "\\" in name:
        return False
    # POSIX 上 Path("C:x").is_absolute() 是 False，盘符要单独认。
    if Path(name).is_absolute() or (len(name) > 1 and name[1] == ":"):
        return False
    return True


def _push_one(source, incoming: Path, name: str, remote: dict, *,
              force: bool, expect: str | None) -> dict:
    """推一个文件，返回它的回执记录（不打印、不记账，便于单独测试）。

    判定顺序是有意的：先名字合法性（不许越出 ``_incoming/``），再本地完整性
    （能否读、``--expect`` 是否相符），再远端存在性，最后才谈写入。任何一步不
    成立都直接返回，绝不产生半次写入。
    """
    record: dict = {
        "ts": _timestamp(), "name": name, "path": None, "sha256": None,
        "bytes": None, "remoteExisted": False, "result": "failed",
    }
    if not _plain_incoming_name(name):
        # 越界写入不修不猜，直接拒；failed ⇒ 退出码 1（2 是"源不可用"，不是这个）。
        record["error"] = (f"name must be a plain file name inside _incoming/ "
                           f"(got {name!r}) — 提案名只能是文件名，不能带目录")
        return record
    rel = f"_incoming/{name}"
    record["path"] = rel
    local = incoming / name
    if not local.is_file():
        record["error"] = f"missing local file {local}"
        return record
    try:
        data = local.read_bytes()
    except OSError as exc:
        record["error"] = f"cannot read {local}: {exc}"
        return record

    digest = _sha256(data)
    record.update(sha256=digest, bytes=len(data))

    if expect is not None and digest != expect:
        # 不写、不算失败 —— 调用方声明的版本不对，最可能是推错文件了。
        record.update(result="refused", reason="expect-mismatch", expected=expect)
        return record

    entry = remote.get(rel)
    record["remoteExisted"] = entry is not None
    if entry is not None:
        try:
            existing = source.read(rel)
        except SourceError as exc:
            # 列得出来却读不出来：不猜、不覆盖。
            record.update(result="failed", error=f"cannot read remote copy: {exc}")
            return record
        record["remoteSha256"] = _sha256(existing)
        record["remoteBytes"] = len(existing)
        if record["remoteSha256"] == digest:
            record["result"] = "unchanged"
            return record
        if not force:
            record.update(result="refused", reason="exists")
            return record

    try:
        source.write(rel, data)
    except SourceError as exc:
        # Actionable: read-only mount, stale share, unreachable host.
        record.update(result="failed", error=str(exc))
        return record
    except Exception as exc:  # noqa: BLE001 - report and continue with the rest
        record.update(result="failed", error=f"{type(exc).__name__}: {exc}")
        return record

    # forced 时 remoteSha256/remoteBytes 记的是**被替换掉的那版**，回执因此能回答
    # "覆盖了什么"，而不只是"写了什么"。
    record["result"] = "forced" if entry is not None else "ok"
    return record


def _report_push_record(record: dict, emit) -> None:
    """把一条回执翻成人话；每条都带"下一步怎么办"。"""
    name = record["name"]
    short = (record.get("sha256") or "?")[:12]
    result = record["result"]
    if result == "ok":
        emit(f"  ↑ _incoming/{name}  {record['bytes']} B  sha256={short}")
    elif result == "forced":
        emit(f"  ↑ _incoming/{name} (forced)  {record['bytes']} B  sha256={short}  "
             f"replaced sha256={(record.get('remoteSha256') or '?')[:12]} "
             f"({record.get('remoteBytes')} B)")
    elif result == "unchanged":
        emit(f"  = _incoming/{name} (unchanged)  {record['bytes']} B  sha256={short}")
    elif record.get("reason") == "expect-mismatch":
        emit(f"  ! {name}: --expect mismatch — local sha256={short} ({record['bytes']} B) "
             f"vs expected {record['expected'][:12]}; nothing written")
    elif result == "refused":
        emit(f"  ! {name}: remote _incoming/{name} already exists with different content\n"
             f"      local  sha256={short} ({record['bytes']} B)\n"
             f"      remote sha256={(record.get('remoteSha256') or '?')[:12]} "
             f"({record.get('remoteBytes')} B)\n"
             f"      refused — pass --force to overwrite, or rename the proposal")
    else:
        emit(f"  ! {name}: {record.get('error') or 'failed'}")


def _push_payload(records: list[dict], cfg: Config) -> dict:
    """``--json`` 的结构化结果。

    每个文件一条放在 ``results``；只有一个文件时把这些字段再平铺到顶层 ——
    agent 侧最常见的是 `jq .result` / `jq .sha256`，不该逼它先取数组下标。
    """
    counts = {key: 0 for key in PUSH_RESULTS}
    for record in records:
        counts[record["result"]] = counts.get(record["result"], 0) + 1
    payload: dict = {
        "ok": not any(r["result"] in PUSH_BAD_RESULTS for r in records),
        "results": records,
        "counts": counts,
        "log": str(_push_log_path(cfg)),
    }
    if len(records) == 1:
        only = records[0]
        for key in ("name", "sha256", "bytes", "result", "remoteExisted",
                    "remoteSha256", "remoteBytes", "reason", "expected", "error"):
            if key in only:
                payload[key] = only[key]
    return payload


def _push_show_log(cfg: Config, args) -> int:
    """``push --log [N]``：打印最近 N 条回执。"""
    limit = args.log if args.log is not None else 10
    if limit < 0:
        _emit(f"scheda: --log takes a non-negative count (got {limit})")
        return 1
    path = _push_log_path(cfg)
    if not path.is_file():
        if getattr(args, "json", False):
            print("[]")
        else:
            _emit(f"scheda: no push receipts yet ({path})")
        return 0
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        _emit(f"scheda: cannot read {path}: {exc} — check permissions on {path.parent}")
        return 1

    records, unparsable = [], 0
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            records.append(json.loads(line))
        except ValueError:
            # 半行只可能来自被中断的写入；跳过并说出来，不能让 --log 自己崩掉。
            unparsable += 1
    if unparsable:
        _emit(f"scheda: skipped {unparsable} unparsable line(s) in {path}")
    tail = records[-limit:] if limit else []
    if getattr(args, "json", False):
        print(json.dumps(tail, ensure_ascii=False, indent=2))
        return 0
    if not tail:
        if limit == 0 and records:
            # "no receipts" 会是假话 —— 账里有记录，只是被要求看 0 条。
            _emit(f"scheda: --log 0 shows nothing ({len(records)} receipts on file)")
        else:
            _emit(f"scheda: no push receipts yet ({path})")
        return 0
    print(f"--- last {len(tail)} of {len(records)} push receipts · {path} ---")
    for record in tail:
        size = record.get("bytes")
        print(f"{record.get('ts', '?')}  {str(record.get('result', '?')):<9}  "
              f"{str(record.get('name', '?')):<28} "
              f"{size if size is not None else '?'} B  "
              f"sha256={str(record.get('sha256') or '?')[:12]}  "
              f"remote={'yes' if record.get('remoteExisted') else 'no'}")
    return 0


def cmd_push(args) -> int:
    """Upload local proposal files to the bundle's ``_incoming/`` directory.

    这是本工具**唯一**的写动作，所以按"宁可拒绝，也不覆盖"设计：

    1. 推前先看远端有没有同名文件（一次 ``walk()``）；
    2. 有且字节相同 → 幂等跳过（``unchanged``），不发起写入；
    3. 有且字节不同 → **默认拒绝**，两端 sha256 前 12 位与字节数都打出来，
       要 ``--force`` 才覆盖；
    4. ``--expect`` 让调用方声明本地哈希，不符即不写（防"以为推的是这一版"）；
    5. 无论写入、跳过还是拒绝，都往 ``<home>/state/push-log.jsonl`` 记一行。

    另外在**任何写入之前**跑一次取代关系闸门（R1）：与某张现行页同 type、
    tags 有交集、标题 Dice ≥0.80 的提案，必须写 ``supersedes:`` 或
    ``not_superseding: "<理由>"``，否则整批拒绝。这是唯一写入口 ⇒ 拦在这里比
    事后从库里删一条谁也退不掉的渐进页便宜得多。``--allow-undeclared`` 是
    明确的绕过开关（用于"确实无关"而作者不想写理由的场景）。
    """
    cfg = _config(args)
    if args.log is not None:
        if args.files:
            _emit("scheda: --log only reads receipts — it cannot push files in the "
                  "same call; run the two separately")
            return 1
        return _push_show_log(cfg, args)

    expect, expect_error = _normalise_expect(args.expect)
    if expect_error:
        _emit(f"scheda: {expect_error}")
        return 1

    source = open_configured_source(cfg)
    incoming = cfg.mirror_dir / "_incoming"
    # 这两种"没东西可推"是正常状态（调度器会按天跑），提示语与退出码保持原样。
    if not incoming.is_dir():
        _emit("scheda: mirror has no _incoming/ directory — nothing to push")
        return 0
    names = args.files or sorted(p.name for p in incoming.iterdir() if p.is_file())
    if not names:
        _emit("scheda: _incoming/ is empty")
        return 0

    if expect is not None and len(names) > 1:
        # 一个哈希对多个文件没有意义；静默选取哪一个都等于没校验。
        shown = ", ".join(names[:3]) + ("…" if len(names) > 3 else "")
        _emit(f"scheda: --expect only works with a single file (got {len(names)}: {shown}) — "
              f"verify them one by one, otherwise the check is meaningless")
        return 1

    if not hasattr(source, "write"):
        _emit(f"scheda: source {getattr(source, 'label', '?')} is read-only")
        return 2
    # ---- R1 提案闸门：写之前拦，且一次报全（不要挤牙膏式一个个报） ----
    if not args.allow_undeclared:
        blocked: list[tuple[str, dict]] = []
        for name in names:
            local = incoming / name
            if local.suffix != ".md" or not local.is_file():
                continue
            try:
                text = local.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            verdict = supersede.gate(cfg, name, text)
            if not verdict["ok"]:
                blocked.append((name, verdict))
        if blocked:
            for name, verdict in blocked:
                _emit(f"scheda: 提案闸门拦下 {name} —— {verdict['reason']}")
            _emit(f"scheda: {len(blocked)} 份提案与现行页撞题却没声明 ⇒ 一份都没推。"
                  f"补 supersedes:/not_superseding: 后重试；确认无关可用 --allow-undeclared")
            return 1

    mount = getattr(source, "mount", None) or {}
    if mount.get("readonly"):
        # 提前一次性拒绝，比逐个文件在 write 里失败更好懂（doctor 也报同一条）。
        _emit(f"scheda: source {getattr(source, 'label', '?')} is a read-only mount "
              f"({mount.get('mountpoint')}) — remount it rw, or push via another "
              f"source (e.g. --source webdav)")
        return 2

    remote = _remote_files(source)

    def emit(message: str) -> None:
        _emit(message, quiet=getattr(args, "quiet", False))

    records = []
    for name in names:
        record = _push_one(source, incoming, name, remote,
                          force=args.force, expect=expect)
        _append_push_receipt(cfg, record)
        _report_push_record(record, emit)
        records.append(record)

    if args.json:
        print(json.dumps(_push_payload(records, cfg), ensure_ascii=False, indent=2))
    return 1 if any(r["result"] in PUSH_BAD_RESULTS for r in records) else 0


def cmd_import(args) -> int:
    """Import records from another tool into the bundle (dry-run by default).

    Reads one export through an adapter, renders pages, then compares them with
    what the source already holds.  Writing needs ``--write``; nothing here ever
    deletes, because a generated view that can delete is a loaded gun.
    """
    from handrolled import pages as importer_mod
    from handrolled.importers import load as load_records

    cfg = _config(args)
    # Deterministic by default: no wall-clock stamp, so re-importing the same
    # records renders the same bytes and writes nothing.  --as-of exists for the
    # case where a page really should record a date.
    at = args.as_of or ""
    generator = f"scheda import/{args.format}"
    try:
        records = load_records(args.format, Path(args.source_path), wikilinks=args.wikilinks)
        planned = importer_mod.plan(
            cfg, records,
            prefix=args.prefix, zone=args.zone, group_by=args.group_by,
            title_prefix=args.title_prefix or "", generator=generator,
            resource=str(Path(args.source_path).resolve()), at=at,
            max_page_bytes=args.max_page_bytes, allow_public=args.allow_public,
        )
    except ValueError as exc:
        if args.json:
            print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False, indent=2))
        else:
            _emit(f"scheda: {exc}")
        return 1

    try:
        source = open_configured_source(cfg)
        outcome = importer_mod.apply(source, planned, write=bool(args.write))
    except Exception as exc:                      # noqa: BLE001 - report, do not traceback
        if args.json:
            print(json.dumps({"ok": False, "error": f"{type(exc).__name__}: {exc}"},
                             ensure_ascii=False, indent=2))
        else:
            _emit(f"scheda: 源不可用或写入失败：{exc}")
        return 11

    payload = {
        "ok": outcome.failed == 0,
        "format": args.format,
        "source": str(Path(args.source_path).resolve()),
        "prefix": planned.prefix,
        "zone": planned.zone,
        "records": planned.items,
        "pages": len(planned.pages),
        "new": outcome.new, "changed": outcome.changed,
        "same": outcome.same, "failed": outcome.failed,
        "written": outcome.written,
        "dry_run": not args.write,
    }
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        mode = "dry-run（加 --write 才真写）" if not args.write else f"已写入 {outcome.written} 页"
        print(f"import · {args.format} → {planned.zone}/{planned.prefix}")
        print(f"  records : {planned.items}")
        print(f"  pages   : {len(planned.pages)}  "
              f"(new {outcome.new} · changed {outcome.changed} · same {outcome.same}"
              + (f" · failed {outcome.failed}" if outcome.failed else "") + ")")
        print(f"  mode    : {mode}")
        if args.write and outcome.written:
            print("  next    : scheda refresh   # mirror + index the new pages")
    return 0 if outcome.failed == 0 else 10
