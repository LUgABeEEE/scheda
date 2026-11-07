# SPDX-License-Identifier: AGPL-3.0-or-later
"""增量索引：快，但必须先正确。

`index.build()` 从 0.9.0 起默认只应用变化（`reused` / `rebuilt` / `removed`），
全量重建仍是回退路径。这条优化唯一能被接受的理由是**结果与全量逐字节相同**，
所以这里的主线不是「快了多少」，而是「增量库 == 全量库」。

四条会被下一个人踩到的坑，各有断言：

* ``chunks`` 是索引里的**总数**，不是本次写入数（写 delta 进去会让 status 和
  state 文件同时撒谎 —— 曾经真的发生过）；
* 分块策略一变，chunk_id 的内容寻址就失效，必须整库重嵌；
* state 文件损坏不能让索引变红，只能让它变慢（回退全量）；
* 删除的块不能变成 FTS5 里的墓碑继续被 MATCH 命中。
"""
from __future__ import annotations

import sqlite3
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from scheda import index  # noqa: E402
from scheda.config import Config  # noqa: E402

PAGES = {
    "z0-public/alpha.md": "# alpha\n\ncanary ALPHA-ONLY-11111 about 挂载点失效\n",
    "z0-public/bravo.md": "# bravo\n\ncanary BRAVO-ONLY-22222 about reports\n",
    "z1-restricted/charlie.md": "# charlie\n\ncanary CHARLIE-ONLY-33333 about 交付偏好\n",
}


def make_cfg(root: Path) -> Config:
    cfg = Config(home=root / "home")
    cfg.mirror_dir.mkdir(parents=True, exist_ok=True)
    for rel, text in PAGES.items():
        path = cfg.mirror_dir / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    return cfg


def write_page(cfg: Config, rel: str, text: str) -> None:
    path = cfg.mirror_dir / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def chunk_rows(cfg: Config) -> set[tuple]:
    """`(path, chunk_id, startline, text)` — the whole content of the index."""
    with sqlite3.connect(cfg.index_db) as con:
        return set(con.execute("select path, chunk_id, startline, text from meta").fetchall())


def full_rebuild(cfg: Config):
    """A from-scratch build in a separate home, for comparison."""
    other = Config(home=cfg.home.parent / "full")
    fresh = Config(home=other.home)
    fresh.mirror_dir.mkdir(parents=True, exist_ok=True)
    for path in cfg.mirror_dir.rglob("*.md"):
        rel = path.relative_to(cfg.mirror_dir)
        target = fresh.mirror_dir / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(path.read_text(encoding="utf-8"), encoding="utf-8")
    return fresh, index.build(fresh, incremental=False)


class TestIncrementalEquivalence(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="scheda-inc-"))
        self.cfg = make_cfg(self.tmp)

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_reports_the_total_chunk_count_not_the_delta(self):
        first = index.build(self.cfg)
        self.assertEqual(first.chunks, first.pages, "one chunk per page in this fixture")
        write_page(self.cfg, "z0-public/bravo.md", "# bravo\n\nedited BRAVO-2\n")
        again = index.build(self.cfg)
        self.assertEqual(again.chunks, again.pages,
                         "`chunks` must stay the index total after a delta build")
        self.assertEqual(again.written, 1, "exactly one page was rewritten")

    def test_second_build_with_no_change_rewrites_nothing(self):
        index.build(self.cfg)
        started = time.monotonic()
        stats = index.build(self.cfg)
        elapsed = time.monotonic() - started
        self.assertEqual(stats.rebuilt, 0)
        self.assertEqual(stats.reused, stats.pages)
        self.assertLess(elapsed, 0.3, "an unchanged index must not be rewritten")

    def test_incremental_matches_a_full_rebuild(self):
        index.build(self.cfg)
        write_page(self.cfg, "z0-public/alpha.md", "# alpha\n\nnew ALPHA-EDIT-44444\n")
        (self.cfg.mirror_dir / "z1-restricted" / "charlie.md").unlink()
        write_page(self.cfg, "z0-public/delta.md", "# delta\n\nDELTA-ONLY-55555\n")
        index.build(self.cfg)

        fresh, _ = full_rebuild(self.cfg)
        self.assertEqual(chunk_rows(self.cfg), chunk_rows(fresh),
                         "a delta build must produce the same chunks as a fresh one")
        for query in ("ALPHA-EDIT-44444", "DELTA-ONLY-55555", "BRAVO-ONLY-22222"):
            mine = [(h.path, h.line) for h in index.search(self.cfg, query, zone="all").hits]
            theirs = [(h.path, h.line) for h in index.search(fresh, query, zone="all").hits]
            self.assertEqual(mine, theirs, query)

    def test_removed_page_leaves_no_tombstone(self):
        index.build(self.cfg)
        (self.cfg.mirror_dir / "z0-public" / "bravo.md").unlink()
        stats = index.build(self.cfg)
        self.assertEqual(stats.removed, 1)
        hits = index.search(self.cfg, "BRAVO-ONLY-22222", zone="all").hits
        self.assertEqual(hits, [], "a deleted chunk must not answer MATCH any more")

    def test_a_changed_page_drops_the_old_chunk(self):
        index.build(self.cfg)
        write_page(self.cfg, "z0-public/alpha.md", "# alpha\n\nreplaced body ALPHA-NEW-66666\n")
        index.build(self.cfg)
        self.assertEqual(index.search(self.cfg, "ALPHA-ONLY-11111", zone="all").hits, [])
        self.assertTrue(index.search(self.cfg, "ALPHA-NEW-66666", zone="all").hits)

    def test_chunking_change_forces_a_full_rebuild(self):
        index.build(self.cfg)
        cfg = Config(home=self.cfg.home, chunk_strategy="structural")
        events: list[str] = []
        stats = index.build(cfg, on_event=events.append)
        self.assertEqual(stats.rebuilt, stats.pages)
        self.assertTrue(any("full rebuild" in line for line in events), events)

    def test_corrupt_state_falls_back_to_a_full_rebuild(self):
        index.build(self.cfg)
        state = index.index_state_path(self.cfg)
        state.write_text("{not json", encoding="utf-8")
        stats = index.build(self.cfg)          # must not raise
        self.assertEqual(stats.pages, len(PAGES))
        self.assertTrue(index.search(self.cfg, "ALPHA-ONLY-11111", zone="all").hits)

    def test_incremental_false_rebuilds_everything(self):
        index.build(self.cfg)
        stats = index.build(self.cfg, incremental=False)
        self.assertEqual(stats.rebuilt, stats.pages)
        self.assertEqual(stats.reused, 0)

    def test_state_file_survives_and_describes_the_result(self):
        index.build(self.cfg)
        import json
        payload = json.loads(index.index_state_path(self.cfg).read_text(encoding="utf-8"))
        self.assertEqual(payload["counts"]["pages"], len(PAGES))
        self.assertEqual(payload["counts"]["chunks"], len(PAGES),
                         "the state must record totals, or the next no-op build lies")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
