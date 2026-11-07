# SPDX-License-Identifier: AGPL-3.0-or-later
"""``scheda doctor`` — probe the environment and print an actionable report."""
from __future__ import annotations

import json
from pathlib import Path

from .. import ragcheck, supersede
from ..config import source_available
from ..mirror import mirror_files
from ..sources import SourceError, open_source

from .common import _config, _ollama_reachable


def cmd_doctor(args) -> int:
    """Probe the environment and print an actionable report.

    Severity is explicit: reads are the primary function, writes are optional.
    A share that is read-only is a **warning** (search works, ``push`` does
    not), not a failure — reporting it as a failure would train people to
    ignore the check.

    **Not strictly read-only** (this is deliberate, and documented so nobody
    uses it as one): two writability probes are performed on every run —

    * ``_writable(home)``: ``mkdir -p`` the home directory if missing, then
      create and delete ``<home>/.scheda-write-probe``;
    * ``source.probe_writable("_incoming")``: when the source exposes it, write
      and delete a probe file under ``_incoming/`` on the **share** (needed to
      answer "can ``push`` work at all?").

    The probe files are removed again; nothing else is written and no document
    is touched.  Every other check here is a plain read.
    """
    cfg = _config(args)
    kind, location = source_available(cfg)
    checks: list[tuple[str, str, str]] = []

    def add(name: str, status: str, detail: str) -> None:
        checks.append((name, status, detail))

    add("home writable", "ok" if _writable(cfg.home) else "fail", str(cfg.home))
    add("source resolved", "ok" if kind else "fail", f"{kind or '—'} {location}")

    source, note = open_source(cfg)
    if source is None:
        add("source usable", "fail", note)
    else:
        info = source.describe() if hasattr(source, "describe") else {}
        where = info.get("device") or info.get("root") or location
        add("source usable", "ok", f"{kind} ({info.get('fstype', '?')}) {where}")
        if info.get("network"):
            target = info.get("mountpoint") or info.get("root") or where
            ro = " (read-only)" if info.get("readonly") else ""
            add("network share detected", "ok", f"{info.get('fstype')} at {target}{ro}")
        try:
            source.ensure_available()
            add("source reachable", "ok", "listing succeeded")
        except SourceError as exc:
            add("source reachable", "fail", str(exc))
        except Exception as exc:  # noqa: BLE001 - reported, not raised
            add("source reachable", "fail", f"{type(exc).__name__}: {exc}")

        if hasattr(source, "probe_writable"):
            ok, why = source.probe_writable("_incoming")
            add("source writable (_incoming)", "ok" if ok else "warn", f"{why} — needed by `push`")
        elif kind == "webdav":
            probe = source.client.probe()
            add("webdav reachable", "ok" if probe.get("reachable") else "fail",
                probe.get("error") or f"HTTP {probe.get('status')}")

    if cfg.unknown_keys:
        add("config keys", "warn",
            "unknown in config.json: " + ", ".join(cfg.unknown_keys) + " (ignored)")
    add("mirror present", "ok" if cfg.mirror_dir.is_dir() else "warn", str(cfg.mirror_dir))
    add("index present", "ok" if cfg.index_db.exists() else "warn", str(cfg.index_db))

    # RAG 一致性：**计数级**检查（count/distinct，不遍历向量 blob），所以能放进
    # 每天被调用的 doctor 里而不会让耗时数量级恶化。
    # 向量库缺失、或 ollama 不可达（能查但暂时不能重建），都只报 warn：
    # 检索照常工作，报 fail 会训练人忽略这个检查（同 source writable 的处置）。
    rag_vectors = ragcheck.vectors_db(cfg)
    if not ragcheck.enabled(cfg):
        add("rag consistency", "warn",
            f"absent — no vector store at {rag_vectors} "
            f"(set SCHEDA_RAG_VECTORS_DB, or build the vectors)")
    else:
        try:
            rag = ragcheck.check(cfg)
        except Exception as exc:  # noqa: BLE001 - reported, not raised
            add("rag consistency", "warn", f"{type(exc).__name__}: {exc}")
        else:
            detail = (f"{rag['chunks']} chunks / {rag['vectors']} vectors"
                      f" · missing {rag['missing']} · orphan {rag['orphan']}"
                      f" · model≠{cfg.rag.model} {rag['model_mismatch']}"
                      f" · dim≠{cfg.rag.dim} {rag['dim_mismatch']}")
            status = "ok" if rag["ok"] else "warn"
            if not rag["ok"]:
                detail = f"{detail} — {rag['note']}"
            probe_ok, probe_why = _ollama_reachable()
            if not probe_ok:
                status = "warn"
                detail = f"{detail} — ollama 不可达（{probe_why}）：暂时无法重新嵌入"
            add("rag consistency", status, detail)

    # Supersede relations (取代关系).  Not built yet is a *warn* like an absent
    # vector store: the fields are optional, and a corpus that never used them
    # is not broken — it just has no答"which conclusion is current" yet.
    try:
        rel = supersede.check(cfg)
    except Exception as exc:  # noqa: BLE001 - reported, not raised
        add("supersede relations", "warn", f"{type(exc).__name__}: {exc}")
    else:
        if not rel["built"]:
            add("supersede relations", "warn", rel["note"])
        else:
            bad = {k: len(v) for k, v in rel["problems"].items() if v}
            add("supersede relations", "ok" if rel["ok"] else "fail",
                rel["note"] if not bad else
                rel["note"] + " — " + ", ".join(f"{k}={n}" for k, n in bad.items()))

    # the privacy invariant that matters most
    mirror_rels = [str(p.relative_to(cfg.mirror_dir)) for p in mirror_files(cfg)]
    leaked = [rel for rel in mirror_rels if not cfg.mirror_allows(rel)]
    add("no excluded paths in mirror", "ok" if not leaked else "fail", ", ".join(leaked[:5]))

    # 索引排除项也要可见：否则"某个目录突然搜不到了"只能靠猜（2026-10-19 加，
    # 与 _incoming / 暂存目录这几次决定直接相关）。
    excluded = [rel for rel in mirror_rels
                if rel.endswith(".md") and cfg.is_index_never(rel) and not cfg.is_never(rel)]
    if cfg.index_never:
        detail = (f"{len(cfg.index_never)} 条模式（{', '.join(cfg.index_never)}）→ "
                  f"{len(excluded)} 页不进索引，仍可 read")
        add("index exclusions", "ok", detail)
    else:
        add("index exclusions", "ok", "未配置（所有 .md 都进索引）")

    failed = any(status == "fail" for _, status, _ in checks)
    if args.json:
        print(json.dumps(
            {"ok": not failed,
             "checks": [{"name": n, "status": s, "detail": d} for n, s, d in checks]},
            ensure_ascii=False, indent=2))
        return 1 if failed else 0
    marks = {"ok": "ok", "warn": "~~", "fail": "!!"}
    for name, status, detail in checks:
        print(f"  [{marks[status]}] {name:28s} {detail}")
    return 1 if failed else 0


def _writable(path: Path) -> bool:
    import os

    probe = path / ".scheda-write-probe"
    try:
        path.mkdir(parents=True, exist_ok=True)
        probe.write_text("", encoding="utf-8")
        probe.unlink()
        return True
    except OSError:
        return False
