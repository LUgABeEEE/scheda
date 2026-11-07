# SPDX-License-Identifier: AGPL-3.0-or-later
"""``scheda rag-check`` and ``scheda supersede-check`` — consistency diagnostics."""
from __future__ import annotations

import json
import sys
from pathlib import Path

from .. import ragcheck, supersede

from .common import _config, _emit


# --------------------------------------------------------------------------- #
# supersede-check (取代关系 + 提案闸门)
def cmd_supersede_check(args) -> int:
    """取代关系体检 / 提案闸门（纯只读）。

    两个用途共一个入口，因为它们读的是同一张 `pages` 表、回答的是同一个问题的
    两面：**"库里现在的取代关系自洽吗"**（A1–A5 + A7）与**"这份新提案要不要
    先声明一句"**（R1）。默认只报告；闸门用 ``--proposal``。
    """
    cfg = _config(args)
    if args.proposal:
        target = Path(args.proposal)
        if not target.is_file():
            _emit(f"scheda: 找不到提案文件：{target}")
            return 2
        rel = target.name
        try:
            raw = target.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            _emit(f"scheda: 读不了 {target}: {exc}")
            return 2
        verdict = supersede.gate(cfg, rel, raw)
        if args.json:
            print(json.dumps(verdict, ensure_ascii=False, indent=1))
        else:
            mark = "ok" if verdict["ok"] else "必须声明"
            _emit(f"提案闸门 {target.name}：{mark} — {verdict['reason']}")
        return 0 if verdict["ok"] else 10

    report = supersede.check(cfg)
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=1))
        return 0 if report["ok"] else 10
    _emit(f"supersede: {report['note']}")
    for kind, rows in sorted(report.get("problems", {}).items()):
        for row in rows[:5]:
            _emit(f"  ✗ {kind}: {row}")
    return 0 if report["ok"] else 10


def _emit_rag_check(payload: dict) -> None:
    """人读版：先给计数与差异，再给结论。dry-run 与真删必须一眼可分；
    拒绝时把原因、比例、两条路径与解锁方式都摆出来（不许静默 no-op）。"""
    print(f"向量库一致性 · {payload['vectors_db']}")
    print(f"  home      : {payload['home']}")
    print(f"  模型/维度 : {payload['model']} / {payload['dim']}"
          f"{'' if payload['enabled'] else '（配置里未启用）'}")
    if payload["store_missing"]:
        print("  !! 向量库不存在")
        print("     建议：用你自己的向量构建器生成（见 docs/semantic-arm.md），"
              "或把 SCHEDA_RAG_VECTORS_DB 指到真实的 vectors.sqlite")
    else:
        print(f"  索引 chunks（meta 行数）: {payload['chunks']}")
        print(f"  向量行数               : {payload['vectors']}")
        print(f"  missing（有页无向量）  : {payload['missing']}")
        print(f"  orphan（有向量无页）   : {payload['orphan']}")
        if payload.get("chunk_level"):
            # 块级口径：页级"这页有向量"不等于"每一块都有向量"（一页 5 块坏 3 块
            # 在页级计数里是 0）。老库没有 startline 列时这里整个不打印，退化
            # 原因在结论 note 里。
            print(f"  块级 missing（有块无向量）: {payload['chunks_missing']}")
            print(f"  块级 orphan（有向量无块） : {payload['chunks_orphan']}")
            if payload.get("chunks_missing_sample"):
                print("  缺向量的块样本（最多 10 个，path:line）:")
                for rel in payload["chunks_missing_sample"]:
                    print(f"      {rel}")
        print(f"  模型不符 {payload['model']:<6}: {payload['model_mismatch']}")
        print(f"  维度不符 {payload['dim']:<6}: {payload['dim_mismatch']}")
        if payload.get("bad_path"):
            # 坏行（path 为 NULL/空/非文本）单独报一行：它们不参与 orphan 计数，
            # 只在 note 里出现太容易被跳过。
            print(f"  坏行（path 为 NULL/空）: {payload['bad_path']}")
        if payload["orphan_sample"]:
            print("  孤儿样本（最多 10 个）:")
            for rel in payload["orphan_sample"]:
                print(f"      {rel}")

    prune = payload["prune"]
    if prune["refused"]:
        _emit_prune_refusal(prune)
    elif not prune["removed"]:
        print("  → 没有孤儿行可删" + ("（dry-run）" if prune["dry_run"] else "（幂等：二次调用无变化）"))
    elif prune["dry_run"]:
        print(f"  → dry-run：待删 {prune['removed']} 行孤儿（数据库未改动），"
              f"保留 {prune['kept']} 行；确认后加 --prune 真删")
    else:
        print(f"  → 已删 {prune['removed']} 行孤儿，保留 {prune['kept']} 行")
    if prune["bypassed"]:
        print(f"  ! --force 越过了保护：{', '.join(prune['bypassed'])}（请确认这是同一个知识库）")

    # 块级换代残留（重分块后旧 chunk 的向量）：页级 prune 管不到，单独一段。
    chunks = payload.get("prune_chunks")
    if chunks and (chunks["refused"] or chunks["removed"]):
        if chunks["refused"]:
            print(f"  !! 块级清理拒绝执行（refused={chunks['reason']}）")
            print(f"     原因：{chunks['note']}")
            print(f"     解锁：{PRUNE_UNLOCK.get(chunks['reason'], '见上面的原因')}")
        elif chunks["dry_run"]:
            print(f"  → 块级 dry-run：待删 {chunks['removed']} 行换代残留"
                  f"（数据库未改动，仅 {chunks['identity']} 口径），保留 {chunks['kept']} 行")
            print("     确认后加 --prune-chunks 真删")
        else:
            print(f"  → 已删 {chunks['removed']} 行换代残留（{chunks['identity']} 口径），"
                  f"保留 {chunks['kept']} 行")
        if chunks["bypassed"]:
            print(f"  ! --force 越过了保护：{', '.join(chunks['bypassed'])}")

    print(f"  结论：{payload['note']}")
    if prune["refused"]:
        print("  退出码 11（拒绝执行：一行都没删）")
    else:
        print("  退出码 " + ("0（一致）" if payload["ok"] else "10（有需要注意项）"))


#: 拒绝原因 → 解锁方式。拒绝必须告诉人怎么继续，否则下一个人只会绕过整条命令。
PRUNE_UNLOCK = {
    ragcheck.REFUSED_EXTERNAL:
        "把 SCHEDA_RAG_VECTORS_DB 指到 home 里的向量库；确实要操作外部库就显式设 "
        "rag.allow_external_vectors_db=true（外部库仍受 50% 大批量保护）",
    ragcheck.REFUSED_BULK:
        "先确认 home 与 vectors_db 是同一个知识库，再带 --force 重跑",
    ragcheck.REFUSED_EMPTY_HOME:
        "先 `scheda index` 建出索引；确认无误后也可以带 --force",
    ragcheck.REFUSED_INDEX:
        "先 `scheda index` 重建索引（索引读不出来时没有任何可比对的对照面）",
    ragcheck.REFUSED_IDENTITY:
        "两侧块身份不一致（一侧还是 (path, startline)，另一侧已是 chunk_id）；"
        "先完成 CAS 迁移重建向量库，再重试 —— 跨身份比对会把每一行都判成孤儿",
}


def _emit_prune_refusal(prune: dict) -> None:
    """拒绝块：原因、比例、两条路径、解锁方式 —— 缺一项人就只能猜。"""
    print(f"  !! 拒绝执行（refused={prune['reason']}）"
          + ("：本次为 dry-run，未删除任何行" if prune["dry_run"] else "：一行都没有删"))
    print(f"     待删 {prune['orphan_rows']}/{prune['total']} 行"
          f"（{prune['ratio']:.0%}）· {prune['orphan_paths']} 个 path"
          f"（阈值 {ragcheck.BULK_DELETE_LIMIT:.0%}）")
    print("     疑似 home 与 vectors_db 不属于同一知识库：")
    print(f"       home       = {prune['home']}")
    print(f"       vectors_db = {prune['vectors_db']}"
          + ("  ← 在 home 之外" if prune["external"] else ""))
    print(f"     原因：{prune['note']}")
    print(f"     解锁：{PRUNE_UNLOCK.get(prune['reason'], '见上面的原因')}")


def cmd_rag_check(args) -> int:
    """向量库 ↔ 索引一致性：默认只报告（dry-run），``--prune`` 才真删孤儿行。

    为什么默认 dry-run：孤儿向量是**证据链**问题（外部检索器会引用已经删掉的页），
    删之前人该先看一眼 orphan_sample；而 ``--prune`` 只删「页面已不在 meta」的行，
    ``kb.sqlite`` 永远只读（见 ragcheck 的模块说明）。

    报告有两个粒度：页级 ``missing``/``orphan``（历史口径，CLI 与文档都在引用）
    与块级 ``chunks_missing``/``chunks_orphan``（按 ``(path, startline)`` 比对）。
    块级存在的原因很具体：一页 5 块只嵌了 2 块时，页级口径看到"这页有向量"，
    ``missing`` 是 0，而检索会静默漏掉 3 块。向量库/索引缺 ``startline`` 列时
    自动退化回页级，并在 ``note`` 里说明（不崩、也不假装比对过）。

    dry-run 下 ``prune()`` 返回的 removed/kept 是**预估值**：那正是 dry-run 的价值
    （"如果删会删几行"），所以文本里写"待删"、JSON 里用 ``prune.dry_run`` 与
    ``prune.label`` 标明，绝不把预估数字伪装成已删。

    ``--prune`` 只清理"整页都不在了"的行；页面还在、只是少了几块的**块级孤儿
    它删不掉**，那要靠重新嵌入（note 里给了建议）。

    与之相对的是**换代残留**：同一页被重新分块后，新块各自嵌入、旧块的 chunk_id
    不再出现在索引里，那些向量行既没人读、也永远不会被页级 prune 碰到（页面确实
    还在）。``--prune-chunks`` 干这件事，它认的身份与 ``check`` 完全一致（两侧都是
    chunk_id 才动手，混用直接拒绝）；三道防线照旧；不带该开关时只做 dry-run 预估。

    ``--force`` 只用于越过"大批量删除"与"空索引"两道保护；**归属校验**（向量库
    必须在 home 之下）只能靠 ``rag.allow_external_vectors_db=true`` 显式放行 ——
    2026-10-19 的事故（临时 home + 默认生产向量库 → 删掉 5,521 行）就是这么发生的。

    退出码四档：

    * ``0``  一致；
    * ``1``  **用法错误**：``--force`` 没有搭配 ``--prune`` / ``--prune-chunks``（它单独存在什么也
      不强制，所以拒绝执行 —— 保持 1 而不是并进 10/11：那是"命令敲错了"，
      不是"库有问题"）；
    * ``10`` 有需要注意（缺失/损坏/页级或块级 missing/orphan/模型维度不符）；
    * ``11`` 拒绝执行或硬失败（``prune.refused`` / ``prune.ok=false`` / 本地状态
      读不出来）。
    """
    cfg = _config(args)
    if args.force and not (args.prune or args.prune_chunks):
        # --force 只影响"真删"；单独用它说明调用方以为自己能强制什么，其实什么都没发生。
        _emit("scheda: --force 只在 --prune / --prune-chunks 时有意义"
              "（它解的是大批量/空索引两道保护）")
        return 1
    try:
        report = ragcheck.check(cfg)
        # 默认也走 prune(dry_run=True)：只读，但能给出"待删几行"的准确预估，
        # 并把三条防线（归属 / 空索引 / 大批量）的判定一并带出来。
        prune_result = ragcheck.prune(cfg, dry_run=not args.prune, force=args.force)
        # 块级换代残留：与页级 prune 分开跑，因为它认的是 chunk 身份而不是页。
        # 没带 --prune-chunks 时只做 dry-run 预估（只读），带才真删。
        chunk_result = ragcheck.prune_chunks(
            cfg, dry_run=not args.prune_chunks, force=args.force)
        if (args.prune and prune_result["removed"]) or (
                args.prune_chunks and chunk_result["removed"]):
            # 真删过就再取一次状态：输出的是"现在的样子"，删了多少另由 prune 说明。
            report = ragcheck.check(cfg)
    except Exception as exc:  # noqa: BLE001 - 诊断命令不该以 traceback 收场
        print(f"scheda: rag-check failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 11

    vectors_path = ragcheck.vectors_db(cfg)
    store_missing = not vectors_path.is_file()
    refused = bool(prune_result["refused"])
    # 块级拒绝只有在明确要求 --prune-chunks 时才算硬失败：身份不一致的旧库上
    # check 仍然有价值，不该因为"没让删"就整体退出 11。
    chunk_refused = bool(chunk_result["refused"]) and bool(args.prune_chunks)
    hard_failure = (refused or not prune_result["ok"] or chunk_refused
                    or (bool(args.prune_chunks) and not chunk_result["ok"]))
    payload = dict(report)
    payload.update({
        "ok": bool(report["ok"]) and not store_missing and not hard_failure,
        "vectors_db": str(vectors_path),
        "home": str(cfg.home),
        "model": cfg.rag.model,
        "dim": cfg.rag.dim,
        # 用 ragcheck 的口径（rag.enabled 为 None 时"文件存在即为真"），不再自己判一次。
        "enabled": ragcheck.enabled(cfg),
        "store_missing": store_missing,
        "prune": {
            **prune_result,
            "requested": bool(args.prune),
            # 脚本判态用 label/refused/reason；dry-run 与真删、拒绝与执行一眼可分。
            "label": ("refused" if refused
                      else "no-orphans" if not prune_result["removed"]
                      else "removed" if not prune_result["dry_run"]
                      else "would remove"),
        },
        "prune_chunks": {
            **chunk_result,
            "requested": bool(args.prune_chunks),
            "label": ("refused" if chunk_result["refused"]
                      else "no-orphans" if not chunk_result["removed"]
                      else "removed" if not chunk_result["dry_run"]
                      else "would remove"),
        },
    })
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        _emit_rag_check(payload)
    if hard_failure:
        return 11
    return 0 if payload["ok"] else 10
