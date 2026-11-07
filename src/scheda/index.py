# SPDX-License-Identifier: AGPL-3.0-or-later
"""Local full-text index over the mirror (SQLite FTS5).

Layout
------
``meta``   — one row per chunk: ``(rid, chunk_id, path, startline, text)``
``chunks`` — a **contentless** FTS5 table holding only the inverted index
``vocab``  — ``fts5vocab(chunks, 'row')``: term → (documents, occurrences)

Storing the text *once* (in ``meta``) instead of inline in FTS5 cut the index
from 8.1× the corpus to about 1.9× in measurement, with no latency change.
Search joins ``chunks`` back to ``meta`` on ``rid``.

Chunk identity is **content-addressed**
---------------------------------------
``chunk_id = sha256(path + 0x1f + text)[:16]`` names a chunk by what it
contains, so the same block keeps its identity across a rebuild, a re-chunk of
a neighbouring page, or a re-embed run — which is what lets ``vectors.sqlite``
and ``embed_cache`` be keyed by it (see ``tools/reindex-vectors.py``).  FTS5
only takes an integer rowid, so ``rid = (int(chunk_id, 16) >> 4) + 1`` derives a
60-bit join key from it.  Both formulas are pinned verbatim against
``tools/migrate-cas.py``, which produces the same schema from an older index:
the two must stay interchangeable, or a migrated index and a rebuilt one stop
being the same thing.

Two schemas are readable
------------------------
:func:`schema_kind` tells the two apart by looking for the ``chunk_id`` column,
and :func:`search` follows it rather than assuming one.  A pre-CAS database
still answers queries (deploy order is "code first, then data", so the window
between the two steps must not break retrieval), and a database whose ``meta``
table has neither key is **refused with an actionable note** instead of being
queried with the wrong join.

The index is always a **local, disposable artifact**: it lives next to the
mirror, never on the share (SQLite is not safe on network filesystems), and can
be rebuilt from the mirror at any time.

Incremental builds
------------------
:func:`build` rebuilds from scratch only when it has to.  Otherwise it copies
the live database, drops the pages that changed or vanished, re-chunks only
those, and swaps the copy in — the same ``.tmp`` + :func:`os.replace` contract,
so a crash still leaves the old index queryable.

What makes that safe is that the state file (``<home>/state/index-state.json``)
records exactly one thing per page: ``[mtime_ns, size]``, the same pair
``mirror.sync`` compares, at nanosecond resolution.  A page whose pair is
unchanged is *not read at all*; its chunks stay as they are, which is what turns
a no-change rebuild into a directory walk (measured: 0.13 s at 10 000 pages
against 5.4 s for the full build).  When nothing changed at all the database is
not even copied.

Two things force a full rebuild instead: a chunking setting that differs from
the one the state records (a chunk is named by its content, so a different cut
renames every chunk of every page — see ``docs/design.md`` §5.5), and any
database the state does not describe (deleted, replaced, different FTS5 storage
version, non-CAS schema).  The rule is "an incremental result must be
indistinguishable from a full one"; when that cannot be *proved*, the full path
runs.

``bm25()`` needs one repair afterwards.  FTS5 caches the corpus statistics it
normalises by — row count and total token count — in a single ``%_data`` record,
and a delete from a ``contentless_delete`` table does not update that record
(measured, SQLite 3.45.1: one row deleted out of three left the record saying
three rows, and every score in the database moved by 2–3 %).  :func:`build`
therefore recomputes the record from the surviving rows — see
:func:`_repair_fts_averages`, which also explains why ``optimize`` and
``rebuild`` cannot do it for us.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path

from .config import DEFAULT_CHUNK_MAX_CHARS, Config
from .supersede import page_meta
from handrolled.text import (allows_relaxation, expand_for_index, make_snippet,
                   match_expression, query_tokens, split_frontmatter)

SEARCH_OVERFETCH = 4

#: Byte between path and text in the ``chunk_id`` pre-image, and the two columns
#: the CAS schema keys a chunk by.  ``\x1f`` cannot occur in a text file we
#: index, so ``(path, text)`` maps to exactly one id.
CHUNK_ID_SEP = b"\x1f"
CHUNK_ID_HEX = 16

#: Fraction of the indexed **chunk** corpus at (or above) which a token counts as
#: generic and is dropped from the relaxed OR pass.  ``0.5`` = "half the corpus".
#: The threshold is ``max(1, int(total * ratio))`` — integer-truncated, so the
#: effective line sits just under the ratio, exactly as the old ``total // 2`` did.
#: Raise it to keep more generic words (broader recall), lower it to be stricter.
#: At the default this yields the same integer threshold as the historical
#: ``total // 2``; only the unit of ``total`` was ever wrong (see below).
#: Deliberately a module constant rather than a ``Config`` field: this fix must
#: stay a single variable, and an ablation can monkeypatch it (see the tests).
SELECTIVE_CUT_RATIO = 0.5

#: Corpus size in the **same unit** as ``vocab.doc``.  ``vocab`` is
#: ``fts5vocab(chunks, 'row')``, so ``doc`` counts *chunks*; ``meta`` holds
#: exactly one row per chunk, so this counts that identical universe.  Pages are
#: *not* interchangeable here — a page is several chunks (measured when the fix
#: landed: 1,321 pages vs 6,133 chunks, i.e. 4.6 chunks/page; the ratio, not the
#: absolute counts, is what matters and it is what the old code ignored).
#: One-line rollback to the pre-fix (page-level, ~4.6x more aggressive) behaviour
#: -- do not, it silently discards mid-frequency terms such as ``目录``/``kb``:
#:     _CORPUS_SIZE_SQL = "select count(distinct path) from meta"
_CORPUS_SIZE_SQL = "select count(*) from meta"


@dataclass
class IndexStats:
    pages: int = 0
    chunks: int = 0
    bytes: int = 0
    duration: float = 0.0
    #: Pages whose frontmatter says they are replaced (``superseded_by``), and
    #: the number of ``supersedes`` edges found.  Reported so a corpus that
    #: starts using the fields says so in one line instead of silently.
    superseded_pages: int = 0
    supersede_edges: int = 0
    #: Incremental accounting.  ``reused`` — pages whose chunks were kept
    #: verbatim because their ``[mtime_ns, size]`` had not moved; ``rebuilt`` —
    #: pages that were re-chunked (a full rebuild reports every page here);
    #: ``removed`` — pages dropped because they left the mirror.  The three are
    #: a partition of what happened, so ``reused + rebuilt`` is the page count of
    #: the resulting index and ``removed`` is *not* part of it.
    reused: int = 0
    rebuilt: int = 0
    removed: int = 0
    #: Blocks actually written by this run.  ``chunks`` above is the total in the
    #: resulting index; reporting the delta there was a bug that made
    #: `index --json` and the state file say "1 chunk" after a one-page rebuild.
    written: int = 0

    @property
    def incremental(self) -> bool:
        """Whether this build reused anything (i.e. was not a full rebuild)."""
        return self.reused > 0

    @property
    def summary(self) -> str:
        extra = ""
        if self.superseded_pages or self.supersede_edges:
            extra = f" · {self.superseded_pages} 已取代 / {self.supersede_edges} 取代边"
        return (f"{self.pages} pages / {self.chunks} chunks / "
                f"{self.bytes / 1048576:.1f} MB / {self.duration:.2f}s{extra}")


@dataclass
class Hit:
    path: str
    line: int
    score: float
    snippet: str
    zone: str = ""
    #: Supersede annotation.  ``superseded`` is the machine-readable bit;
    #: ``superseded_by`` / ``valid_until`` are what the snippet prefix shows.
    superseded: bool = False
    superseded_by: str = ""
    valid_until: str = ""


@dataclass
class SearchResult:
    query: str
    tokens: list[str] = field(default_factory=list)
    hits: list[Hit] = field(default_factory=list)
    zones: tuple[str, ...] = ()
    stale: bool = False
    index_age: float | None = None
    note: str = ""
    relaxed: bool = False
    dropped: list[str] = field(default_factory=list)
    #: How many of ``hits`` are marked superseded (0 when the index predates the
    #: pages table).  Callers can surface "N of these are outdated" in one line.
    superseded_shown: int = 0


def _has_table(con: sqlite3.Connection, name: str) -> bool:
    """Whether ``name`` exists — used to keep a query working on an older index."""
    try:
        return con.execute(
            "select count(*) from sqlite_master where type in ('table','view') and name=?",
            (name,)).fetchone()[0] > 0
    except sqlite3.Error:                              # pragma: no cover - unreadable db
        return False


def _document_frequency(con, tokens: list[str]) -> dict[str, int]:
    """How many indexed documents contain each token (0 when absent)."""
    lowered = [t.lower() for t in tokens]
    if not lowered:
        return {}
    placeholders = ",".join("?" * len(lowered))
    try:
        rows = con.execute(
            f"select term, doc from vocab where term in ({placeholders})", lowered
        ).fetchall()
    except sqlite3.OperationalError:
        return {}
    return {term: int(doc) for term, doc in rows}


def _selective_tokens(con, tokens: list[str]) -> list[str]:
    """Keep only the tokens that actually discriminate.

    The unit of "corpus" is a **chunk**, not a page.  ``vocab`` is
    ``fts5vocab(chunks, 'row')``, so ``vocab.doc`` counts the *chunks* that
    contain a term, and the threshold is computed over that same chunk universe
    (``_CORPUS_SIZE_SQL``).  The two must stay in one unit: mixing them means the
    cut silently describes something other than what this docstring says.

    A token that appears in **at least ``SELECTIVE_CUT_RATIO`` of all indexed
    chunks** (default 50% — "half the corpus") carries almost no signal; if it
    survives into the relaxed pass it drags in unrelated documents.  When every
    token is generic the original list is returned, so the caller still has a
    usable (if broad) query.

    Regression this guards against: the threshold used to be
    ``count(distinct path) // 2`` — half the *pages* — while ``doc`` counted
    *chunks*.  With ~4.6 chunks per page the effective line was ~10.8% of chunks
    instead of 50%: about 4.6x more aggressive than advertised, and it discarded
    useful mid-frequency terms (measured when the fix landed: ``目录`` doc=771,
    ``kb`` doc=1450, ``dsh`` doc=2944, all above the bogus cut of 660).
    See ``tests/test_index_zone.py`` for the fixture that pins the unit.
    """
    freq = _document_frequency(con, tokens)
    try:
        total = con.execute(_CORPUS_SIZE_SQL).fetchone()[0]
    except sqlite3.DatabaseError:
        return tokens
    if not freq or total <= 1:
        return tokens
    cut = max(1, int(total * SELECTIVE_CUT_RATIO))
    kept = [t for t in tokens if freq.get(t.lower(), 0) < cut]
    return kept or tokens


def _index_files(cfg: Config):
    """Yield ``(rel_path, absolute_path)`` for every indexable markdown file."""
    root = cfg.mirror_dir
    if not root.is_dir():
        return
    for path in sorted(root.rglob("*.md")):
        rel = path.relative_to(root).as_posix()
        if cfg.index_allows(rel):
            yield rel, path


#: Schema ids returned by :func:`schema_kind`.
SCHEMA_CAS = "cas"              # (rid, chunk_id, …) — what :func:`build` writes
SCHEMA_LEGACY = "legacy"        # (id, …) — pre-2026-10 indexes, still readable
SCHEMA_UNKNOWN = "unknown"      # a meta table with neither key
SCHEMA_ABSENT = ""              # no index (or no meta table) at all

#: Note for a database whose key column is not recognised.  Written as the
#: operator's next command: this state is reachable (a half-finished migration,
#: a hand-edited file) and guessing a join would return wrong rows silently.
_UNUSABLE_NOTE = (
    "index schema not recognised (meta table has neither `chunk_id` nor `id`) — "
    "run `scheda index` to rebuild it, or migrate an older index with "
    "tools/migrate-cas.py"
)


def chunk_id_of(path: str, text: str) -> str:
    """Content-addressed chunk key: ``sha256(path + 0x1f + text)[:16]``.

    Must stay byte-for-byte identical to ``tools/migrate-cas.py``'s
    ``chunk_id_of``; the two producers of the CAS schema have to agree, because
    ``vectors.sqlite`` and the embed cache are keyed by the result.
    """
    digest = hashlib.sha256(
        path.encode("utf-8") + CHUNK_ID_SEP + text.encode("utf-8")
    ).hexdigest()
    return digest[:CHUNK_ID_HEX]


def rid_of(chunk_id: str) -> int:
    """FTS5 join key derived from a ``chunk_id`` (60 bits, never 0).

    ``>> 4`` keeps the top 60 bits and ``+ 1`` moves the result off rowid 0;
    collisions are astronomically unlikely (64-bit prefix of a SHA-256) and are
    handled explicitly by :class:`_ChunkIds` rather than silently overwritten —
    a unique-index violation here would abort the whole rebuild.
    """
    return (int(chunk_id, 16) >> 4) + 1


class _ChunkIds:
    """Allocate ``chunk_id`` / ``rid`` for one build without ever colliding.

    Same policy as ``migrate-cas.py``'s ``IdAllocator`` (truncate-and-retry for
    a colliding id, linear probe for a colliding rid) but scoped to a single
    build: the FTS join key only has to be unique *within* one database, and a
    capture-the-whole-corpus allocator would double peak memory on a corpus
    this size for a case that cannot happen.

    Recorded collisions are logged by :func:`build` — a *silent* adjustment
    would make the rebuild irreproducible for no visible reason.
    """

    def __init__(self) -> None:
        self._ids: set[str] = set()
        self._rids: set[int] = set()
        self.id_collisions: list[tuple[str, str]] = []
        self.rid_collisions: list[tuple[int, int]] = []

    def allocate(self, path: str, text: str) -> tuple[int, str]:
        """Return ``(rid, chunk_id)`` for one chunk, resolving any collision."""
        cid = chunk_id_of(path, text)
        if cid in self._ids:
            base = cid
            for attempt in range(1, 0x100):
                candidate = base[:CHUNK_ID_HEX - 2] + f"{attempt:02x}"
                if candidate not in self._ids:
                    cid = candidate
                    self.id_collisions.append((base, candidate))
                    break
            else:                                   # pragma: no cover - needs 255 colliding ids
                raise RuntimeError(f"chunk_id collision storm around {base}; refusing to guess")
        self._ids.add(cid)

        rid = rid_of(cid)
        if rid in self._rids:
            base_rid = rid
            while rid in self._rids:                # pragma: no cover - needs a 64-bit collision
                rid += 1
                if rid >= (1 << 62):                # pragma: no cover - defensive
                    raise RuntimeError(f"rid probe exhausted for {cid}")
            self.rid_collisions.append((base_rid, rid))
        self._rids.add(rid)
        return rid, cid


def schema_kind(cfg: Config) -> str:
    """Which schema ``cfg.index_db`` uses — see ``SCHEMA_*``.

    Opened **read-only** (``mode=ro``): probing must never create a database,
    and the answer has to come from the file even while another process is
    rebuilding it.  An unreadable or absent file is ``SCHEMA_ABSENT`` — the
    "no index" note already covers that, and this function's job is to describe
    a database that exists.
    """
    db = cfg.index_db
    if not db.exists():
        return SCHEMA_ABSENT
    try:
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    except sqlite3.Error:                           # pragma: no cover - connect rarely fails
        return SCHEMA_ABSENT
    try:
        return _schema_kind(con)
    except sqlite3.Error:                           # pragma: no cover - unreadable file
        return SCHEMA_ABSENT
    finally:
        con.close()


def _schema_kind(con: sqlite3.Connection) -> str:
    """:func:`schema_kind` on an open connection (shared with ``search``)."""
    try:
        columns = {row[1] for row in con.execute("pragma table_info(meta)")}
    except sqlite3.Error:                           # pragma: no cover - unreadable file
        return SCHEMA_ABSENT
    if not columns:
        return SCHEMA_ABSENT                        # no meta table at all
    if "chunk_id" in columns:
        return SCHEMA_CAS
    if "id" in columns:
        return SCHEMA_LEGACY
    return SCHEMA_UNKNOWN


#: The CAS DDL, verbatim from ``tools/migrate-cas.py`` (``KB_DDL_CAS``).  The
#: `meta_path` index is part of it: `zone` filters and the doctor both ask
#: ``count(distinct path)`` / ``like`` questions that would otherwise scan.
_CAS_DDL = """
create table meta(
    rid       integer primary key,
    chunk_id  text    not null unique,
    path      text    not null,
    startline integer not null,
    text      text    not null
);
create index meta_path on meta(path);
create virtual table chunks using fts5(
    toks, content='', contentless_delete=1, tokenize='unicode61'
);
-- term -> (documents containing it, occurrences). Lets search drop
-- generic words when relaxing, instead of matching on "UNIQ" alone.
create virtual table vocab using fts5vocab(chunks, 'row');
-- one row per page: the frontmatter fields a *machine* needs (``supersede.py``
-- explains why ``status`` and ``superseded_by`` are separate questions).
create table pages(
    path          text primary key,
    type          text,
    title         text,
    description   text,
    tags          text,                -- space separated; small, never queried by term
    status        text,                -- draft | stable | deprecated
    superseded_by text,                -- null/'' = still in force
    valid_until   text,                -- ISO8601 | null
    fm_at         text,                -- generated.at: "who is newer"
    indexed_at    text
);
create index pages_live on pages(status, valid_until);
create index pages_succ on pages(superseded_by);
create index pages_type on pages(type, status);
-- A page may supersede many pages, so this direction is its own table.  Edge
-- direction: ``child`` declares ``supersedes: [parent]``, i.e. child is the
-- newer page; the parent's ``superseded_by`` is the inverse, and
-- ``supersede.check()`` (A1) asserts the two agree.
create table supersedes(
    child  text not null,
    parent text not null,
    primary key(child, parent)
) without rowid;
create index supersedes_parent on supersedes(parent);
-- (path, startline) is what search tie-breaks on; let SQLite walk the index.
create index meta_path_line on meta(path, startline);
"""


#: ATX headings that start a new chunk under ``structural``: ``#``, ``##``,
#: ``###`` and only those.  ``####`` and deeper deliberately do **not** split —
#: the strategy is specified as #/##/###, and a level-4 heading normally names a
#: sub-item inside the section that already started at level 1–3.
_HEADING_RE = re.compile(r"^ {0,3}(#{1,3})(?:[ \t]|$)")

#: Fence delimiters: three or more backticks or tildes, indented at most three
#: spaces.  Anchored at the line start on purpose — an inline ```code``` span
#: never opens a fence.
_FENCE_RE = re.compile(r"^ {0,3}(`{3,}|~{3,})")


def _fence_marker(line: str) -> tuple[str, int] | None:
    """``(char, length)`` when the line is a code-fence delimiter, else ``None``."""
    match = _FENCE_RE.match(line)
    if not match:
        return None
    run = match.group(1)
    return run[0], len(run)


def _fence_flags(lines: list[str]) -> tuple[list[bool], list[int]]:
    """Mark fenced code blocks: ``(in_fence, fence_end)``, both per line.

    ``in_fence[i]`` is True for the opening delimiter, the body **and** the
    closing one, so "this cut would tear the fence apart" is exactly
    ``in_fence[i - 1] and in_fence[i]``.  ``fence_end[i]`` is the index just past
    the closing delimiter of the fence containing line ``i`` (``len(lines)`` for
    an unterminated fence), which lets the length splitter step over a fence
    that is itself longer than the cap without cutting into it.
    """
    n = len(lines)
    in_fence = [False] * n
    fence_end = [n] * n
    i = 0
    while i < n:
        marker = _fence_marker(lines[i])
        if marker is None:
            i += 1
            continue
        char, length = marker
        end = n                                  # unterminated fence: runs to EOF
        for j in range(i + 1, n):
            closing = _fence_marker(lines[j])
            if closing and closing[0] == char and closing[1] >= length:
                end = j + 1
                break
        for k in range(i, end):
            in_fence[k] = True
            fence_end[k] = end
        i = end
    return in_fence, fence_end


def _is_heading(line: str) -> bool:
    return _HEADING_RE.match(line) is not None


def _splits_fence(boundary: int, in_fence: list[bool]) -> bool:
    """True when cutting *before* line ``boundary`` would cut a fence in half."""
    return 0 < boundary < len(in_fence) and in_fence[boundary - 1] and in_fence[boundary]


def _block_chars(lines: list[str], start: int, end: int) -> int:
    """Characters in ``"\\n".join(lines[start:end])`` — the unit of ``chunk_max_chars``.

    Python's ``len`` on a ``str`` counts code points, so CJK text is measured as
    characters rather than as UTF-8 bytes (a 3-byte CJK character would otherwise
    cost three times what the cap suggests).
    """
    if end <= start:
        return 0
    return sum(len(lines[i]) for i in range(start, end)) + (end - start - 1)


def _content_blocks(lines: list[str], cuts: list[int]) -> list[tuple[int, int]]:
    """Turn candidate cut positions into contiguous blocks that all carry content.

    A cut is kept only when at least one non-blank line sits between it and the
    previous kept cut; otherwise the blank run stays with the block that follows
    it.  Dropping the *cut* instead of the lines is what keeps the page
    reconstructible: the blocks remain an ordered, gapless partition of the page.
    A blank tail is folded into the last block for the same reason.
    """
    n = len(lines)
    prefix = [0] * (n + 1)                       # non-blank lines before index i
    for i, line in enumerate(lines):
        prefix[i + 1] = prefix[i] + (1 if line.strip() else 0)
    kept: list[int] = []
    prev = 0
    for cut in sorted({c for c in cuts if 0 < c < n}):
        if cut <= prev:
            continue
        if prefix[cut] - prefix[prev]:
            kept.append(cut)
            prev = cut
    bounds = [0] + kept + [n]
    if len(bounds) > 2 and not (prefix[n] - prefix[bounds[-2]]):
        del bounds[-2]                           # blank tail joins the last block
    return [(bounds[i], bounds[i + 1]) for i in range(len(bounds) - 1)]


def _split_long_block(lines: list[str], start: int, end: int, max_chars: int,
                      in_fence: list[bool], fence_end: list[int]) -> list[tuple[int, int]]:
    """Split ``[start, end)`` into blocks of at most ``max_chars`` characters.

    Cut preference, in order:

    1. a blank-line boundary (the paragraph break) — the only cut that does not
       tear a sentence in half;
    2. any plain line boundary;
    3. no cut available: the run is one fenced block, or one single line, that
       is itself over the cap.  Both are atomic — they are emitted whole, since
       cutting them would contradict "fences are never cut" and "no line is
       ever lost or split".
    """
    out: list[tuple[int, int]] = []
    s = start
    while s < end:
        # Largest prefix [s, e) that still fits the cap.
        e = s
        chars = 0
        while e < end:
            need = len(lines[e]) + (1 if e > s else 0)
            if chars + need > max_chars:
                break
            chars += need
            e += 1
        if e >= end:
            out.append((s, end))
            break
        cut = None
        if e > s:
            for cand in range(e, s, -1):
                if lines[cand - 1].strip() == "" and not _splits_fence(cand, in_fence):
                    cut = cand
                    break
            if cut is None:
                for cand in range(e, s, -1):
                    if not _splits_fence(cand, in_fence):
                        cut = cand
                        break
        if cut is None:
            cut = fence_end[s] if in_fence[s] else s + 1
            cut = min(cut, end)
        out.append((s, cut))
        s = cut
    return out


def _merge_blank_blocks(lines: list[str], blocks: list[tuple[int, int]]) -> list[tuple[int, int]]:
    """Fold whitespace-only blocks into a neighbour instead of dropping their lines."""
    out: list[tuple[int, int]] = []
    for a, b in blocks:
        if out and not any(lines[i].strip() for i in range(a, b)):
            out[-1] = (out[-1][0], b)            # blank run joins the block before it
        else:
            out.append((a, b))
    if len(out) > 1 and not any(lines[i].strip() for i in range(*out[0])):
        out[0] = (out[0][0], out[1][1])          # ... or the one after, at page start
        del out[1]
    if out and not any(lines[i].strip() for i in range(*out[0])):
        return []                                # nothing but whitespace: nothing to index
    return out


def _chunk_structural(raw: str, cfg: Config) -> list[tuple[int, str]]:
    """Structure-first chunking — see :func:`_chunk_page` for the rules."""
    lines = raw.splitlines()
    if not lines:
        return []
    try:
        max_chars = int(getattr(cfg, "chunk_max_chars", DEFAULT_CHUNK_MAX_CHARS))
    except (TypeError, ValueError):              # pragma: no cover - Config coerces ints
        max_chars = DEFAULT_CHUNK_MAX_CHARS
    max_chars = max(1, max_chars)

    in_fence, fence_end = _fence_flags(lines)
    # A heading starts a new block — but only outside a fence: a line that looks
    # like a heading inside a code block is code, not structure.
    heading_cuts = [i for i in range(1, len(lines))
                    if not in_fence[i] and _is_heading(lines[i])]
    blocks: list[tuple[int, int]] = []
    for a, b in _content_blocks(lines, heading_cuts):
        if _block_chars(lines, a, b) <= max_chars:
            blocks.append((a, b))
        else:
            blocks.extend(_split_long_block(lines, a, b, max_chars, in_fence, fence_end))
    return [(a + 1, "\n".join(lines[a:b])) for a, b in _merge_blank_blocks(lines, blocks)]


def _chunk_page(raw: str, cfg: Config) -> list[tuple[int, str]]:
    """Split one page into ``(startline_1based, block_text)`` chunks.

    The strategy comes from ``cfg.chunk_strategy``:

    ``lines`` (default — the behaviour that predates this function)
        Fixed ``chunk_lines``-line sliding window, kept verbatim on purpose.  A
        chunk is named by its content (:func:`chunk_id_of`), so any change here
        renames every chunk of every page and forces a full re-embed; that has
        to be an explicit decision, never a side effect of an upgrade.  Any
        value other than ``structural`` lands here too, so a typo cannot switch
        the new behaviour on.

    ``structural``
        Structure first: a ``#``/``##``/``###`` heading starts a new chunk, a
        fenced code block (```` ``` ```` or ``~~~``) is never cut, an over-long
        section falls back to blank-line (paragraph) boundaries, and each chunk
        stays within ``chunk_max_chars`` **characters**.  A fenced block and a
        single line are atomic: if one of them alone exceeds the cap it is
        emitted whole rather than torn apart.  A blank line is atomic too, which
        is why a block already sitting on the cap may end one or two characters
        over it — the alternative is dropping a line (measured on the production
        corpus: 2 blocks out of 131 028).

    Both strategies skip whitespace-only blocks.  The ``structural`` partition is
    exact — an ordered, gapless run of line slices — so joining the blocks with
    ``"\\n"`` rebuilds the page line for line.
    """
    if getattr(cfg, "chunk_strategy", "lines") == "structural":
        return _chunk_structural(raw, cfg)
    lines = raw.splitlines()
    out: list[tuple[int, str]] = []
    for start in range(0, len(lines), cfg.chunk_lines):
        block = "\n".join(lines[start:start + cfg.chunk_lines])
        if not block.strip():
            continue
        out.append((start + 1, block))
    return out


#: Schema id of ``<home>/state/index-state.json``.  A different id means "no
#: state": the file describes what the *index* already holds, so a shape this
#: version does not understand must never be guessed at — a full rebuild is the
#: safe direction.
STATE_SCHEMA = 1

#: FTS5 shadow-table layout whose corpus-statistics record this module knows how
#: to recompute (:func:`_repair_fts_averages`).  Anything else forces a full
#: rebuild rather than writing a record we cannot read back.
_FTS_STORAGE_VERSION = 4


def index_state_path(cfg: Config) -> Path:
    """``<home>/state/index-state.json`` — next to the mirror's manifest."""
    return cfg.home / "state" / "index-state.json"


def _chunking_signature(cfg: Config) -> dict:
    """Every setting that changes *how* a page is cut into chunks.

    A chunk is named by its content, so changing any of these renames every
    chunk of every page and invalidates ``vectors.sqlite`` wholesale.  Recording
    the triple (not just ``chunk_strategy``) is what keeps that from happening
    silently when someone edits ``chunk_lines``.
    """
    return {
        "strategy": getattr(cfg, "chunk_strategy", "lines"),
        "lines": int(getattr(cfg, "chunk_lines", 0) or 0),
        "max_chars": int(getattr(cfg, "chunk_max_chars", DEFAULT_CHUNK_MAX_CHARS) or 0),
    }


def _stat_signature(path: Path) -> list[int]:
    """``[mtime_ns, size]`` — what ``mirror.sync`` compares, at nanosecond resolution.

    The mirror is written by :func:`os.replace` from a freshly written temp file,
    so a page that changed content always has a new ``mtime_ns``: the pair only
    has to be finer-grained than the writes, not smarter than them.  (The one
    edit this cannot see is the one ``sync`` cannot see either — a share whose
    timestamps are truncated to a second, edited in that same second with the
    byte size unchanged.  ``sync --full`` is the documented answer there.)
    """
    st = path.stat()
    return [int(st.st_mtime_ns), int(st.st_size)]


def _db_signature(db: Path) -> list[int] | None:
    """``[size, mtime_ns]`` of the index file, or ``None`` when it is gone."""
    try:
        st = db.stat()
    except OSError:
        return None
    return [int(st.st_size), int(st.st_mtime_ns)]


def load_index_state(cfg: Config) -> dict | None:
    """Read the incremental state, or ``None`` when absent, damaged or foreign.

    Corruption has exactly one answer — "rebuild everything" — because this file
    is a description of what the index already contains: trusting a half-written
    one would leave pages that changed while it was broken permanently stale.
    """
    try:
        data = json.loads(index_state_path(cfg).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    if data.get("schema") != STATE_SCHEMA:
        return None
    if not isinstance(data.get("files"), dict) or not isinstance(data.get("chunking"), dict):
        return None
    return data


def _save_index_state(cfg: Config, state: dict) -> None:
    """Write the state atomically (``.tmp`` + :func:`os.replace`), as ``mirror`` does."""
    path = index_state_path(cfg)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, separators=(",", ":")),
                   encoding="utf-8")
    os.replace(tmp, path)


def _state_payload(cfg: Config, files: dict, pages: int, chunks: int) -> dict:
    """Assemble the state file. Written *after* the database is in place."""
    return {
        "schema": STATE_SCHEMA,
        "chunking": _chunking_signature(cfg),
        "db": _db_signature(cfg.index_db),
        "counts": {"pages": pages, "chunks": chunks},
        "files": files,
        "builtAt": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
    }


def _put_varint(value: int) -> bytes:
    """Encode one integer exactly as SQLite's ``sqlite3PutVarint`` does."""
    v = int(value)
    if v < 0:
        raise ValueError(f"varint needs a non-negative value, got {v}")
    if v <= 0x7F:
        return bytes((v,))
    groups: list[int] = []
    while v:
        groups.append(v & 0x7F)
        v >>= 7
    groups.reverse()
    n = len(groups)
    if n > 8:                                       # pragma: no cover - needs 2**56 tokens
        raise ValueError("varint overflow")
    groups[0] |= (0xFF << (9 - n)) & 0xFF
    return bytes(groups)


def _first_varint(blob: bytes) -> int:
    """Decode the first varint of an FTS5 ``%_docsize.sz`` blob.

    That blob is one varint per column holding the row's token count for the
    column; with the single ``toks`` column this module indexes, the first
    varint *is* the row length.
    """
    if not blob:
        return 0
    lead = blob[0]
    if lead <= 0x7F:
        return lead
    length = 1
    mask = 0x80
    while mask and (lead & mask):
        length += 1
        mask >>= 1
    value = lead & (mask - 1) if mask else 0
    for byte in blob[1:length]:
        value = (value << 7) | (byte & 0x7F)
    return value


def _repair_fts_averages(db: Path) -> bool:
    """Recompute FTS5's corpus-statistics record; ``False`` = unknown layout.

    ``bm25()`` normalises by two numbers — how many rows the table holds, and how
    many tokens they hold in total — and FTS5 keeps both in one record:
    ``chunks_data`` row 1, ``varint(nRow)`` followed by one ``varint(total)`` per
    column.  A ``DELETE`` against a ``contentless_delete`` table does **not**
    update that record.  Measured on SQLite 3.45.1 (three rows, one deleted):

    ===========================================  =========  =========
    database                                     record     truth
    ===========================================  =========  =========
    built fresh with 2 rows                      (2, 113)   (2, 113)
    built with 3 rows, then one deleted          (3, 130)   (2, 113)
    ===========================================  =========  =========

    ... and every score in the second database is 2–3 % off the fresh one, which
    is enough to reorder hits.  Neither documented repair works: ``optimize``
    leaves the record alone (verified), and ``rebuild`` is refused outright on a
    contentless table (``'rebuild' may not be used with a contentless fts5
    table``).  So the record is recomputed here from the rows that survive —
    ``meta`` for the count, ``%_docsize.sz`` for the lengths — which reproduces
    the fresh build's bytes exactly (the equivalence test compares the record
    itself, not just the scores).

    Returns ``False`` when the storage version or column layout is not the one
    this was verified against; :func:`build` then rebuilds from scratch instead
    of writing a record it does not understand.
    """
    con = sqlite3.connect(str(db))
    try:
        row = con.execute("select v from chunks_config where k = 'version'").fetchone()
        if row is None or int(str(row[0])) != _FTS_STORAGE_VERSION:
            return False
        columns = [r[1] for r in con.execute("pragma table_info(chunks)")]
        if columns != ["toks"]:
            return False                            # multi-column record, not handled
        rows = 0
        total = 0
        for (blob,) in con.execute(
                "select d.sz from chunks_docsize d join meta m on m.rid = d.id"):
            total += _first_varint(blob)
            rows += 1
        record = _put_varint(rows) + _put_varint(total)
        current = con.execute("select block from chunks_data where rowid = 1").fetchone()
        if current is None or current[0] != record:
            con.execute("insert or replace into chunks_data(rowid, block) values (1, ?)",
                        (record,))
            con.commit()
        return True
    except sqlite3.Error:                           # pragma: no cover - unreadable shadow table
        return False
    finally:
        con.close()


def _drop_page(con: sqlite3.Connection, rel: str) -> int:
    """Delete every row belonging to one page; returns how many chunks went.

    FTS first, then ``meta`` — the order pinned by ``tests/test_index_cas.py``:
    ``meta`` first leaves a row that still MATCHes but is invisible through the
    join, and ``integrity-check`` does not notice it.  ``supersedes`` only holds
    edges *declared by this page*; an edge pointing *at* it belongs to another
    page's frontmatter and stays, exactly as a full rebuild would leave it.
    """
    rids = [rid for (rid,) in con.execute("select rid from meta where path = ?", (rel,))]
    for rid in rids:
        con.execute("delete from chunks where rowid = ?", (rid,))
        con.execute("delete from meta where rid = ?", (rid,))
    con.execute("delete from pages where path = ?", (rel,))
    con.execute("delete from supersedes where child = ?", (rel,))
    return len(rids)


class _FullRebuild(Exception):
    """Internal: this incremental attempt cannot be trusted — rebuild instead.

    Never escapes the module: :func:`build` catches it, says why on the event
    stream, and runs the full path.  An incremental result that cannot be shown
    to equal a full one must not be shipped quietly.
    """


def _build_full(cfg: Config, emit) -> IndexStats:
    """Rebuild every page from scratch, in the CAS schema (the historical path).

    The rebuild is written to ``<db>.tmp`` and swapped in with :func:`os.replace`,
    then the previous database is kept as ``<db>.prev``.  Two reasons:

    * **crash safety** — the old implementation deleted the live database first,
      so a crash mid-build left an empty index and search returned nothing
      (2026-10-24 post-mortem);
    * **size** — ``DROP TABLE`` does not return pages to the filesystem, so a
      rebuild after a shrink would otherwise keep the file at its historical
      maximum (measured: 271 MB that would not come down); a fresh temp file
      is inherently compact.

    Chunks are keyed by :func:`chunk_id_of`; the FTS rowid is :func:`rid_of`
    applied to it, so the same corpus always yields the same keys.  Collisions
    are resolved by :class:`_ChunkIds` and reported through ``on_event`` —
    they are a "this should be impossible" diagnostic, not a normal path.

    The chunking itself lives in :func:`_chunk_page`; which shape the rebuild
    used is logged **only** when it is not the historical one, so a default
    deployment's log stays byte-identical.
    """
    db = cfg.index_db
    db.parent.mkdir(parents=True, exist_ok=True)
    tmp = Path(str(db) + ".tmp")
    for suffix in ("", "-wal", "-shm"):
        stale = Path(str(tmp) + suffix)
        if stale.exists():
            stale.unlink()

    if getattr(cfg, "chunk_strategy", "lines") == "structural":
        emit(f"chunk strategy: structural (max {cfg.chunk_max_chars} chars)")

    stats = IndexStats()
    ids = _ChunkIds()
    files: dict[str, list[int]] = {}
    con = sqlite3.connect(str(tmp))
    try:
        con.executescript(_CAS_DDL)
        for rel, path in _index_files(cfg):
            stats.pages += 1
            try:
                signature = _stat_signature(path)
                raw = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            # One frontmatter parse per page: the chunks below still index the
            # whole file (header included) — changing that would change every
            # chunk_id and force a full re-embed, so it is its own decision.
            page = page_meta(rel, raw)
            con.execute(
                "insert or replace into pages(path, type, title, description, tags,"
                " status, superseded_by, valid_until, fm_at, indexed_at)"
                " values (?,?,?,?,?,?,?,?,?,?)",
                (rel, page.type, page.title, page.description, page.tags_text,
                 page.status, page.superseded_by, page.valid_until, page.fm_at,
                 time.strftime("%Y-%m-%dT%H:%M:%S%z")),
            )
            for parent in page.supersedes:
                con.execute("insert or ignore into supersedes(child, parent) values (?,?)",
                            (rel, parent))
                stats.supersede_edges += 1
            if page.superseded_by:
                stats.superseded_pages += 1
            for startline, block in _chunk_page(raw, cfg):
                rid, cid = ids.allocate(rel, block)
                con.execute(
                    "insert into meta(rid, chunk_id, path, startline, text) values (?,?,?,?,?)",
                    (rid, cid, rel, startline, block),
                )
                con.execute(
                    "insert into chunks(rowid, toks) values (?,?)",
                    (rid, expand_for_index(block)),
                )
                stats.chunks += 1
            files[rel] = signature
        con.execute("insert into chunks(chunks) values ('optimize')")
        con.commit()
    finally:
        con.close()

    if stats.pages == 0 or stats.chunks == 0:
        for suffix in ("", "-wal", "-shm"):
            junk = Path(str(tmp) + suffix)
            if junk.exists():
                junk.unlink()
        raise RuntimeError("rebuild produced an empty index; previous index kept")

    if db.exists():
        os.replace(db, Path(str(db) + ".prev"))
    for suffix in ("-wal", "-shm"):
        junk = Path(str(db) + suffix)
        if junk.exists():
            junk.unlink()
    os.replace(tmp, db)

    if ids.id_collisions:
        emit(f"!! {len(ids.id_collisions)} chunk_id collision(s) resolved by suffix: "
             f"{ids.id_collisions[:3]}")
    if ids.rid_collisions:                          # pragma: no cover - needs a 64-bit collision
        emit(f"!! {len(ids.rid_collisions)} rid collision(s) resolved by probing: "
             f"{ids.rid_collisions[:3]}")

    # Every page was re-chunked, so this is what `reused`/`rebuilt` have to say
    # about a full build: nothing was kept, everything was made again.
    stats.rebuilt = stats.pages
    _save_index_state(cfg, _state_payload(cfg, files, len(files), stats.chunks))
    return stats


def _build_incremental(cfg: Config, emit) -> IndexStats | None:
    """Bring the live index up to date in place — ``None`` means "do a full one".

    ``None`` is returned (not raised) for the cases where incrementality is
    simply not on offer — no state, a state written under different chunking
    settings, a database that is not the one the state describes, a leftover
    journal file — because the caller's response is the same in all of them.
    :class:`_FullRebuild` is reserved for "incremental was attempted and its
    result could not be verified".
    """
    db = cfg.index_db
    state = load_index_state(cfg)
    if state is None or not db.exists():
        return None
    if state.get("chunking") != _chunking_signature(cfg):
        emit("incremental: chunking settings differ from the state — full rebuild")
        return None
    if state.get("db") != _db_signature(db):
        emit("incremental: index file is not the one the state describes — full rebuild")
        return None
    for suffix in ("-wal", "-shm"):
        if Path(str(db) + suffix).exists():
            emit("incremental: journal file beside the index — full rebuild")
            return None

    files: dict[str, list[int]] = {}
    for rel, path in _index_files(cfg):
        try:
            files[rel] = _stat_signature(path)
        except OSError:                             # vanished mid-walk: not ours to index
            continue
    known = state.get("files") or {}
    changed = [rel for rel, signature in files.items() if known.get(rel) != signature]
    removed = [rel for rel in known if rel not in files]

    if not changed and not removed:
        # Nothing to write.  The live database already *is* what a full rebuild
        # would produce, so rewriting it would only burn I/O — this is the case
        # the state file exists for (measured: 0.13 s at 10 000 pages).
        counts = state.get("counts") or {}
        stats = IndexStats(pages=int(counts.get("pages", len(files))),
                           chunks=int(counts.get("chunks", 0)),
                           reused=len(files))
        emit(f"index unchanged: {stats.pages} pages reused, nothing rewritten")
        return stats

    return _apply_incremental(cfg, state, files, changed, removed, emit)


def _apply_incremental(cfg: Config, state: dict, files: dict[str, list[int]],
                       changed: list[str], removed: list[str], emit) -> IndexStats:
    """Copy the index, drop what went away, re-chunk what moved, swap it in."""
    db = cfg.index_db
    tmp = Path(str(db) + ".tmp")
    for suffix in ("", "-wal", "-shm"):
        stale = Path(str(tmp) + suffix)
        if stale.exists():
            stale.unlink()
    # The live file is never opened for writing: it has to stay readable (and
    # swappable back) if anything below goes wrong.
    shutil.copyfile(db, tmp)

    stats = IndexStats()
    ids = _ChunkIds()
    dropped = 0
    old_total = 0
    # Blocks written *by this run*.  ``stats.chunks`` is the total once the delta
    # is applied — the two are different numbers, and conflating them made the
    # state file (and every caller of `index --json`) report 1 chunk for a
    # 10 000-page index that happened to rebuild one page.
    written = 0
    con = sqlite3.connect(str(tmp))
    try:
        if _schema_kind(con) != SCHEMA_CAS:
            raise _FullRebuild("index schema is not the CAS one")
        old_total = con.execute("select count(*) from meta").fetchone()[0]
        for rel in removed:
            dropped += _drop_page(con, rel)
            stats.removed += 1
        for rel in changed:
            dropped += _drop_page(con, rel)
            path = cfg.mirror_dir / rel
            try:
                raw = path.read_text(encoding="utf-8", errors="replace")
            except OSError as exc:
                raise _FullRebuild(f"cannot read {rel}: {exc}") from exc
            page = page_meta(rel, raw)
            con.execute(
                "insert or replace into pages(path, type, title, description, tags,"
                " status, superseded_by, valid_until, fm_at, indexed_at)"
                " values (?,?,?,?,?,?,?,?,?,?)",
                (rel, page.type, page.title, page.description, page.tags_text,
                 page.status, page.superseded_by, page.valid_until, page.fm_at,
                 time.strftime("%Y-%m-%dT%H:%M:%S%z")),
            )
            for parent in page.supersedes:
                con.execute("insert or ignore into supersedes(child, parent) values (?,?)",
                            (rel, parent))
                stats.supersede_edges += 1
            if page.superseded_by:
                stats.superseded_pages += 1
            for startline, block in _chunk_page(raw, cfg):
                rid, cid = ids.allocate(rel, block)
                con.execute(
                    "insert into meta(rid, chunk_id, path, startline, text) values (?,?,?,?,?)",
                    (rid, cid, rel, startline, block),
                )
                con.execute(
                    "insert into chunks(rowid, toks) values (?,?)",
                    (rid, expand_for_index(block)),
                )
                written += 1
            stats.rebuilt += 1
        if dropped:
            # A deleted chunk in a contentless table is a tombstone until a merge
            # drops it, and until then it still answers MATCH and is still
            # counted by `vocab`.  Merging is what makes `(path, chunk_id, …)`
            # *and* the inverted index agree with a fresh build.
            con.execute("insert into chunks(chunks) values ('optimize')")
        con.commit()
        pages_in_db = con.execute("select count(distinct path) from meta").fetchone()[0]
        chunks_in_db = con.execute("select count(*) from meta").fetchone()[0]
    except sqlite3.Error as exc:
        raise _FullRebuild(f"{type(exc).__name__}: {exc}") from exc
    finally:
        con.close()

    def discard() -> None:
        for suffix in ("", "-wal", "-shm"):
            junk = Path(str(tmp) + suffix)
            if junk.exists():
                junk.unlink()

    expected_chunks = old_total - dropped + written
    if pages_in_db != len(files) or chunks_in_db != expected_chunks:
        discard()
        raise _FullRebuild(
            f"delta reproduced {pages_in_db} pages / {chunks_in_db} chunks, "
            f"a full build would have {len(files)} / {expected_chunks}")
    if chunks_in_db == 0:
        discard()
        raise RuntimeError("rebuild produced an empty index; previous index kept")
    if not _repair_fts_averages(tmp):
        discard()
        raise _FullRebuild("cannot recompute the FTS5 statistics record")

    if db.exists():
        os.replace(db, Path(str(db) + ".prev"))
    for suffix in ("-wal", "-shm"):
        junk = Path(str(db) + suffix)
        if junk.exists():
            junk.unlink()
    os.replace(tmp, db)

    if ids.id_collisions:                           # pragma: no cover - needs a colliding id
        emit(f"!! {len(ids.id_collisions)} chunk_id collision(s) resolved by suffix: "
             f"{ids.id_collisions[:3]}")
    if ids.rid_collisions:                          # pragma: no cover - needs a 64-bit collision
        emit(f"!! {len(ids.rid_collisions)} rid collision(s) resolved by probing: "
             f"{ids.rid_collisions[:3]}")

    stats.pages = pages_in_db
    stats.chunks = chunks_in_db          # total in the index, not this run's writes
    stats.written = written
    stats.reused = pages_in_db - stats.rebuilt
    # `superseded_pages` counts this run's pages only; report the table's truth.
    with sqlite3.connect(f"file:{db}?mode=ro", uri=True) as check:
        try:
            stats.superseded_pages = check.execute(
                "select count(*) from pages where coalesce(superseded_by,'') <> ''"
            ).fetchone()[0]
            stats.supersede_edges = check.execute("select count(*) from supersedes").fetchone()[0]
        except sqlite3.Error:                       # pragma: no cover - unreadable pages table
            pass
    _save_index_state(cfg, _state_payload(cfg, files, stats.pages, stats.chunks))
    emit(f"incremental: {stats.reused} reused / {stats.rebuilt} rebuilt / "
         f"{stats.removed} removed")
    return stats


def build(cfg: Config, on_event=None, *, incremental: bool = True) -> IndexStats:
    """Bring the index in line with the mirror — incrementally when it can be.

    ``incremental=True`` (the default) applies only what changed: a page whose
    ``[mtime_ns, size]`` matches the state file is kept as it is, a page that
    changed is re-chunked, a page that left the mirror is dropped, and the whole
    thing is applied to a *copy* that is then swapped in with
    :func:`os.replace` — the same crash-safe ``.tmp`` + ``.prev`` contract as a
    full rebuild (:func:`_build_full`, which is also where the chunking
    invariants are documented).

    The result must be indistinguishable from a full rebuild, and that is
    enforced rather than assumed: the delta is applied, the resulting counts are
    compared with what a full build would produce, FTS5's corpus-statistics
    record is recomputed (:func:`_repair_fts_averages`), and *any* doubt — a
    missing or damaged state file, a different chunking setting, a database the
    state does not describe, an unreadable page, an unexpected count — falls
    back to the full path with the reason on the event stream.

    ``incremental=False`` forces a full rebuild, as does ``SCHEDA_INDEX_FULL=1``
    in the environment (the CLI's ``index`` subcommand has no ``--full`` switch
    yet; this is the reachable equivalent from a shell or a timer).

    Returns :class:`IndexStats`, whose ``reused`` / ``rebuilt`` / ``removed``
    counters say what happened: a full rebuild reports ``rebuilt == pages``.
    """
    emit = on_event or (lambda _m: None)
    started = time.perf_counter()
    if os.environ.get("SCHEDA_INDEX_FULL", "").strip().lower() not in ("", "0", "false", "no"):
        incremental = False

    stats = None
    if incremental:
        try:
            stats = _build_incremental(cfg, emit)
        except _FullRebuild as why:
            emit(f"incremental build refused ({why}); rebuilding from scratch")
            stats = None
    if stats is None:
        stats = _build_full(cfg, emit)

    stats.bytes = cfg.index_db.stat().st_size if cfg.index_db.exists() else 0
    stats.duration = time.perf_counter() - started
    emit(f"indexed {stats.summary}")
    return stats


def index_age(cfg: Config) -> float | None:
    """Seconds since the index was written, or ``None`` if it does not exist."""
    db = cfg.index_db
    if not db.exists():
        return None
    return max(0.0, time.time() - db.stat().st_mtime)


def _zone_clause(cfg: Config, prefixes: tuple[str, ...]) -> tuple[str, list[str]]:
    """SQL fragment restricting results to the selected zones.

    Root-level documents (``index.md``, ``log.md``) are always allowed: they are
    the bundle's front door and carry no private content by construction.
    """
    if not prefixes:
        return "1=1", []
    placeholders = " or ".join("m.path like ?" for _ in prefixes)
    return f"(m.path not like '%/%' or {placeholders})", [f"{p}%" for p in prefixes]


def search(
    cfg: Config,
    query: str,
    *,
    zone: str = "z01",
    limit: int | None = None,
    snippet_chars: int | None = None,
    fresh_seconds: float | None = None,
    include_superseded: bool = True,
    deep: bool = False,
) -> SearchResult:
    """Run a ranked full-text query against the local index.

    ``fresh_seconds`` marks the result ``stale`` when the mirror is older than
    that many seconds, so a caller can decide to refresh and retry.
    """
    wanted = limit or cfg.default_limit
    _shift = int(getattr(cfg.rag, "rerank_max_shift", 0) or 0)
    _deep = bool(deep) and bool(getattr(cfg.rag, "deep_enabled", False))
    # 限幅重排要能看到「更多候选」才有意义：内部多取 2σ 条，末尾再截回 wanted。
    _pool_extra = int(getattr(cfg.rag, "rerank_shift_pool", 5) or 0)
    limit = wanted + (_pool_extra if _shift > 0 else 0)
    if _deep:
        limit = max(limit, int(getattr(cfg.rag, "deep_candidates", 40) or 40))
    width = snippet_chars or cfg.snippet_chars
    zones = cfg.zones_for_query(zone)
    prefixes = cfg.query_prefixes(zone)
    result = SearchResult(query=query, zones=zones, index_age=index_age(cfg))

    if not cfg.index_db.exists():
        result.note = "index missing — run `scheda index`"
        return result

    tokens = query_tokens(query)
    result.tokens = tokens
    clause, params = _zone_clause(cfg, prefixes)
    ceiling = max(limit * SEARCH_OVERFETCH, 16)

    con = sqlite3.connect(f"file:{cfg.index_db}?mode=ro", uri=True)
    try:
        # The join key follows the schema on disk.  A CAS database keys chunks
        # by `rid` (the integer derived from `chunk_id`, which is what the FTS
        # rowid holds) and has no `id` at all, so `m.id = chunks.rowid` raises
        # "no such column: m.id"; an older database has `id` and no `rid`.
        # `chunk_id` is text and can never equal a rowid, so joining on it would
        # silently return nothing — the failure mode this whole change is about.
        kind = _schema_kind(con)
        if kind == SCHEMA_UNKNOWN:
            result.note = _UNUSABLE_NOTE
            return result
        key = "rid" if kind == SCHEMA_CAS else "id"
        # Supersede data lives in `pages`/`supersedes`.  A database built before
        # those tables existed must keep answering queries — the whole point of
        # the "code first, data later" deploy order — so the join is conditional
        # instead of assumed.  `superseded` is computed in SQL because it has to
        # drive the ORDER BY, not just decorate the row.
        has_pages = _has_table(con, "pages")
        now = time.strftime("%Y-%m-%dT%H:%M:%S%z")
        select_ = "m.path, m.startline, m.text, bm25(chunks) as score"
        join_ = f"from chunks join meta m on m.{key} = chunks.rowid"
        lead_params: tuple = ()
        if has_pages:
            select_ += (
                ", coalesce(p.superseded_by,'') as succ"
                ", coalesce(p.valid_until,'') as vu"
                ", case when coalesce(p.superseded_by,'') <> ''"
                "     or (coalesce(p.valid_until,'') <> '' and p.valid_until <= ?)"
                "   then 1 else 0 end as superseded"
            )
            join_ += " left join pages p on p.path = m.path"
            lead_params = (now,)
        sql = (
            f"select {select_} {join_} "
            f"where chunks match ? and {clause} "
            # 全序：bm25 完全相同的行（同内容的两份拷贝、同模板页）必须有稳定的
            # 先后。只按 score 排时，并列行的顺序由 FTS5 的 rowid 顺序决定 ——
            # 那既随重建而变（插入顺序变了），又在两种 schema 之间不同（CAS 的
            # rowid 由 chunk_id 派生），于是"同一批题、重建一次、答案换个位置"。
            # (path, startline) 是内容里的天然次序，两个 schema 都能给出，
            # 所以它同时买到「跨重建稳定」与「迁移前后逐条一致」。
            # 现行页永远排在已被取代的页之前（spec：标注 + 降权，不隐藏）。
            + ("order by superseded asc, score, m.path, m.startline limit ?"
               if has_pages else
               "order by score, m.path, m.startline limit ?")
        )

        def run(expression: str, ceiling_: int | None = None):
            return con.execute(
                sql, (*lead_params, expression, *params, ceiling_ or ceiling)).fetchall()

        # Pass 1 — precision: every expanded token must be present, so an exact
        # identifier cannot be answered by a document that merely shares "UNIQ".
        expression_used = match_expression(tokens, "AND")
        try:
            rows = run(expression_used)
        except sqlite3.OperationalError as exc:
            result.note = f"query failed: {exc}"
            return result

        # Pass 2 — recall: relax to OR, but only for vague queries, and only
        # over *selective* tokens. Words appearing in at least half the chunks
        # (SELECTIVE_CUT_RATIO of the chunk corpus) are dropped first; otherwise
        # a single generic word would drag in unrelated documents (measured). A
        # lone latin identifier is never relaxed.
        if not rows and len(tokens) > 1 and allows_relaxation(query):
            selective = _selective_tokens(con, tokens)
            if selective != tokens:
                result.dropped = [t for t in tokens if t not in selective]
            expression_used = match_expression(selective or tokens, "OR")
            try:
                rows = run(expression_used)
            except sqlite3.OperationalError:
                rows = []
                expression_used = ""
            result.relaxed = bool(rows)
    finally:
        con.close()

    seen: set[str] = set()
    first_cov = None
    for row in rows:
        path, start_line, text, score = row[:4]
        succ = row[4] if len(row) > 4 else ""
        valid_until = row[5] if len(row) > 5 else ""
        superseded = bool(row[6]) if len(row) > 6 else False
        if include_superseded is False and superseded:
            continue
        if path in seen:
            continue
        seen.add(path)
        # The first chunk of a page carries its YAML header, and indexing the
        # header is a deliberate open question (it holds title/description, which
        # *are* useful terms) — but *showing* it is not: a snippet that starts
        # with `generated: { by: ... }` is noise.  So strip it for the snippet
        # and add back the lines it occupied, which keeps reported line numbers
        # pointing at real file lines.
        body, header_lines = split_frontmatter(text)
        fragment, relative_line = make_snippet(body, tokens, width, phrase=query)
        relative_line += header_lines
        if superseded:
            # Annotate, do not hide: the model — not the retriever — decides
            # which of two conflicting conclusions still holds.  Hiding a
            # historically-true page recreates "I wrote that down and search
            # cannot find it", which is the complaint this field must not cause.
            mark = f"[已被 {succ or '新页'} 取代" + (f"，有效至 {valid_until}" if valid_until else "") + "] "
            fragment = mark + fragment
            result.superseded_shown += 1
        own_zone = cfg.zone_for(path)
        if first_cov is None:
            _m = len(set(tokens) & set(query_tokens(text)))
            first_cov = (_m / len(tokens)) if tokens else 1.0
        result.hits.append(
            Hit(
                path=path,
                line=start_line + relative_line - 1,
                score=round(float(score), 2),
                snippet=fragment,
                zone=own_zone.name if own_zone else "",
                superseded=superseded,
                superseded_by=succ,
                valid_until=valid_until,
            )
        )
        if len(result.hits) >= limit:
            break

    # Hidden mode must still return `limit` live hits: the over-fetched window
    # can be entirely superseded, in which case widening it is the only honest
    # fix (filtering after ranking always has this shape — see the spec §3).
    if include_superseded is False and len(result.hits) < limit:
        for factor in (4, 16, 64):
            with sqlite3.connect(f"file:{cfg.index_db}?mode=ro", uri=True) as con2:
                if _schema_kind(con2) != kind or not has_pages:
                    break
                wider = con2.execute(
                    sql, (*lead_params, expression_used or match_expression(tokens, "AND"),
                          *params, ceiling * factor)).fetchall()
            for row in wider:
                if len(result.hits) >= limit:
                    break
                path, start_line, _, score = row[:4]
                if row[6] or path in seen:
                    continue
                seen.add(path)
                body2, header2 = split_frontmatter(row[2])
                fragment, relative_line = make_snippet(body2, tokens, width, phrase=query)
                relative_line += header2
                own_zone = cfg.zone_for(path)
                result.hits.append(Hit(path=path, line=start_line + relative_line - 1,
                                      score=round(float(score), 2), snippet=fragment,
                                      zone=own_zone.name if own_zone else ""))
            if len(result.hits) >= limit:
                break
    if result.superseded_shown:
        result.note = (f"{result.superseded_shown} 条命中已被取代（片段带 [已被 … 取代] 前缀，"
                       f"排序已降到现行页之后）")
    if fresh_seconds is not None:
        age = index_age(cfg)
        result.stale = age is None or age > fresh_seconds
    # 可选向量臂：只在关键词命中不足 limit 时补一路（默认关闭）。
    # 位置刻意放在所有关键词逻辑之后 —— 关闭时行为与旧版逐字节一致；
    # 任何异常都只降级成 note，绝不影响关键词结果。
    trigger = str(getattr(cfg.rag, "vector_trigger", "auto") or "auto")
    weak_ratio = float(getattr(cfg.rag, "vector_weak_ratio", 0.35) or 0.0)
    weak = (first_cov is not None and first_cov < weak_ratio)
    reserve = int(getattr(cfg.rag, "vector_weak_reserve", 2) or 0) if (weak and trigger == "auto") else 0
    keyword_slots = max(1, wanted - reserve)
    need_vector = cfg.rag.vector_enabled and (
        bool(getattr(cfg.rag, 'vector_always', False))
        or len(result.hits) < wanted or reserve > 0)
    if need_vector:
        try:
            from .ragvec import augment, hyde_answer
            seed = result.hits[:keyword_slots] if reserve else result.hits
            hyde = None
            if weak and getattr(cfg.rag, "hyde_enabled", False):
                import os as _os
                if _os.environ.get("SCHEDA_HYDE_PROVIDER") in ("remote", "http"):
                    from .ragvec import hyde_remote
                    hyde = hyde_remote(query)
                else:
                    hyde = hyde_answer(query, getattr(cfg.rag, "hyde_model", "qwen2.5:7b"), int(getattr(cfg.rag, "hyde_prompt_tokens", 100) or 100))
            merged, note = augment(cfg, query, limit, seed, width, zone=zone,
                                   mode=getattr(cfg.rag, "vector_mode", "fill"), hyde=hyde, rerank=weak or bool(getattr(cfg.rag, 'rerank_always', False)))
            if merged:
                result.hits = merged
                tag = "weak-match " if weak else ""
                result.note = (result.note + " | " if result.note else "") + tag + note
        except Exception as exc:  # noqa: BLE001 - 语义路永不影响关键词路径
            extra = f"vector arm skipped: {type(exc).__name__}: {exc}"
            result.note = (result.note + " | " if result.note else "") + extra
    # 限幅重排（maxshift）：最终顺序上再跑一次 cross-encoder，但每条位移不超过 σ。
    # 默认 0 = 关闭，行为与旧版逐字节一致；异常只降级成 note。
    shift = _shift
    if shift > 0 and getattr(cfg.rag, "rerank_enabled", False) and len(result.hits) > 1:
        try:
            from handrolled import rerank as _rr
            from .ragvec import _snippet as _snp
            _chars = int(getattr(cfg.rag, "rerank_passage_chars", 480) or 480)
            _passages = [_snp(cfg.mirror_dir, h.path, h.line, _chars) for h in result.hits]
            _scores = _rr.score_pairs(query, _passages)
            if _scores is not None and len(_scores) == len(result.hits):
                before = [(h.path, h.line) for h in result.hits]
                result.hits = _clamp_shift(result.hits, _scores, shift)
                if before != [(h.path, h.line) for h in result.hits]:
                    result.note = (result.note + " | " if result.note else "") + f"maxshift ±{shift}"
        except Exception:
            pass
    if _deep and len(result.hits) > 1:
        try:
            from handrolled import deeprank as _dr
            from .ragvec import _snippet as _snp
            _seen: set = set()
            _uniq: list = []
            for h in result.hits:
                if h.path in _seen:
                    continue
                _seen.add(h.path)
                _uniq.append(h)
            _chars = int(getattr(cfg.rag, "deep_passage_chars", 340) or 340)
            _cap = int(getattr(cfg.rag, "deep_candidates", 40) or 40)
            _pages = []
            for h in _uniq[:_cap]:
                b = (getattr(h, "snippet", "") or "").strip()
                if len(b) < min(_chars, 200):
                    b = _snp(cfg.mirror_dir, h.path, h.line, _chars)
                _pages.append((h.path, b[:_chars]))
            _model = str(getattr(cfg.rag, "deep_model", "") or "")
            _order = _dr.pick_pages(
                query, _pages, model=_model,
                pick=int(getattr(cfg.rag, "deep_pick", 3) or 3),
                timeout=float(getattr(cfg.rag, "deep_timeout_s", 25) or 25),
            )
            if _order:
                _picked = [_pages[i - 1][0] for i in _order if 0 < i <= len(_pages)]
                _by = {}
                for h in _uniq:
                    _by.setdefault(h.path, h)
                _head = [_by[x] for x in _picked if x in _by]
                _pset = set(_picked)
                result.hits = _head + [h for h in _uniq if h.path not in _pset]
                result.note = (result.note + " | " if result.note else "") + "deep=true：LLM 挑 %d 页（%s）" % (len(_head), _model)
            else:
                result.note = (result.note + " | " if result.note else "") + "deep=true：LLM 不可用，回退普通排序"
        except Exception as _exc:
            result.note = (result.note + " | " if result.note else "") + "deep=true 跳过：%s" % type(_exc).__name__
    if len(result.hits) > wanted:
        result.hits = result.hits[:wanted]
    return result


def _clamp_shift(hits: list, scores: list, sigma: int) -> list:
    """按 cross-encoder 分排，但每条落位相对原名次位移不超过 sigma（贪心指派）。

    为什么不是直接采用重排顺序：池内多数题的正确答案只与前后几名差 0.1~0.2，
    全量重排会把 p-06 这类「并列页」挤出前 5；限幅只允许「近邻上移」，
    2026-10-24 实测 +2 题 / −0（33 题口径）。
    """
    n = len(hits)
    if n < 2 or sigma <= 0:
        return hits
    desired = [h for _s, h in sorted(zip(scores, hits), key=lambda t: -t[0])]
    orig = {id(h): i for i, h in enumerate(hits)}
    slots: list = [None] * n
    placed: set = set()
    for h in desired:
        o = orig[id(h)]
        lo, hi = max(0, o - sigma), min(n - 1, o + sigma)
        for pos in range(lo, hi + 1):
            if slots[pos] is None:
                slots[pos] = h
                placed.add(id(h))
                break
    for h in hits:
        if id(h) not in placed:
            for pos in range(n):
                if slots[pos] is None:
                    slots[pos] = h
                    break
    return [h for h in slots if h is not None]

@dataclass
class ReadResult:
    path: str
    start: int
    end: int
    total: int
    lines: list[str] = field(default_factory=list)


def read(cfg: Config, rel: str, *, start: int = 1, lines: int = 80) -> ReadResult:
    """Read a line window from the mirror, refusing paths outside it."""
    root = cfg.mirror_dir.resolve()
    target = (root / rel).resolve()
    if root not in target.parents or not target.is_file():
        raise FileNotFoundError(f"not in mirror: {rel}")

    body = target.read_text(encoding="utf-8", errors="replace").splitlines()
    start = max(1, start)
    window = max(1, min(lines, cfg.max_read_lines))
    end = min(len(body), start + window - 1)
    return ReadResult(path=rel, start=start, end=end, total=len(body), lines=body[start - 1:end])


def stats(cfg: Config) -> IndexStats:
    """Describe the current index without rebuilding it."""
    out = IndexStats()
    if not cfg.index_db.exists():
        return out
    out.bytes = cfg.index_db.stat().st_size
    con = sqlite3.connect(f"file:{cfg.index_db}?mode=ro", uri=True)
    try:
        out.chunks = con.execute("select count(*) from chunks").fetchone()[0]
        out.pages = con.execute("select count(distinct path) from meta").fetchone()[0]
    except sqlite3.DatabaseError:
        pass
    finally:
        con.close()
    return out
