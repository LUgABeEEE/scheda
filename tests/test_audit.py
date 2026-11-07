"""结构化事件日志与审计日志的测试。

覆盖的是**承诺**，不是实现细节：一行一个 JSON、轮转不丢数据、保留份数有上限、
不可写目录不静默、审计只记元数据、并发写不损坏。

``chmod`` 那几个用例在 root 下没有意义（root 无视权限位），所以它们显式跳过
而非静默通过 —— 一个"永远绿"的权限测试比没有测试更坏。
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import sys
import tempfile
import threading
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from scheda import audit  # noqa: E402
from scheda.config import Config  # noqa: E402
from scheda.index import search  # noqa: E402
from scheda.service import audit_event, file_logger, log_event, refresh  # noqa: E402

#: 审计里**绝不能**出现的正文标记。造一个真实的 bundle，搜它，然后断言审计文件
#: 里只有"查过"这件事，没有查到的内容。
SENSITIVE_BODY = "SENSITIVE-CANARY-42"

#: 轮转用例用的填充长度：够长以致阈值可以被精确算出来，够短以致不会触发默认 5 MiB。
PAD = "x" * 150


def _lines(path: Path) -> list[str]:
    if not path.exists():
        return []
    return [line for line in path.read_text(encoding="utf-8").split("\n") if line]


def _records(path: Path) -> list[dict]:
    return [json.loads(line) for line in _lines(path)]


class AuditTestBase(unittest.TestCase):
    """每个用例一个干净 home，并保证 ``SCHEDA_LOG_*`` 不串味。"""

    ENV_KEYS = ("SCHEDA_LOG_MAX_BYTES", "SCHEDA_LOG_BACKUPS")

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.home = Path(self._tmp.name) / "home"
        self.cfg = Config(home=self.home)
        saved = {key: os.environ.pop(key, None) for key in self.ENV_KEYS}
        self.addCleanup(lambda: [os.environ.pop(k, None) for k in self.ENV_KEYS])
        for key, value in saved.items():
            if value is not None:
                self.addCleanup(lambda k=key, v=value: os.environ.__setitem__(k, v))

    def set_rotation(self, max_bytes: int | None = None, backups: int | None = None):
        env = {}
        if max_bytes is not None:
            env["SCHEDA_LOG_MAX_BYTES"] = str(max_bytes)
        if backups is not None:
            env["SCHEDA_LOG_BACKUPS"] = str(backups)
        patcher = mock.patch.dict(os.environ, env)
        patcher.start()
        self.addCleanup(patcher.stop)


class TestEvents(AuditTestBase):
    """A. 结构化事件日志。"""

    def test_one_event_is_exactly_one_valid_json_line_with_every_field(self):
        log_event(self.cfg, "info", "index-build", "built the index",
                  pages=3, chunks=42, duration_ms=7)

        path = audit.events_path(self.cfg)
        self.assertTrue(path.exists())
        self.assertEqual(path.read_text(encoding="utf-8").count("\n"), 1,
                         "一条记录 = 一行，且以换行结束")

        (record,) = _records(path)
        self.assertEqual(set(record), {"ts", "level", "event", "msg", "extra"})
        self.assertEqual(record["level"], "info")
        self.assertEqual(record["event"], "index-build")
        self.assertEqual(record["msg"], "built the index")
        self.assertEqual(record["extra"], {"pages": 3, "chunks": 42, "duration_ms": 7})

        stamp = datetime.fromisoformat(record["ts"])
        self.assertIsNotNone(stamp.tzinfo, "ts 必须带时区")
        self.assertNotIn(".", record["ts"], "秒级：不要微秒")

    def test_newlines_in_msg_become_spaces_so_the_line_stays_one_line(self):
        log_event(self.cfg, "warn", "mirror-sync", "first line\nsecond\r\nthird")

        path = audit.events_path(self.cfg)
        self.assertEqual(len(_lines(path)), 1)
        (record,) = _records(path)
        self.assertEqual(record["msg"], "first line second third")
        self.assertNotIn("\n", record["msg"])

    def test_unknown_level_and_messy_event_name_are_normalised(self):
        log_event(self.cfg, "WARNING", "Index Build!", "x")
        log_event(self.cfg, "verbose", "  ", "y")

        first, second = _records(audit.events_path(self.cfg))
        self.assertEqual(first["level"], "warn")
        self.assertEqual(first["event"], "index-build")
        self.assertEqual(second["level"], "info", "不认识的级别退到 info，不丢记录")
        self.assertEqual(second["event"], "log")

    def test_extra_is_a_fixed_key_set(self):
        """``extra`` 是契约：陌生键丢弃，免得下游 ``jq`` 靠猜字段。"""
        log_event(self.cfg, "error", "mirror-sync", "boom", error="EACCES", bogus="ignored")
        (record,) = _records(audit.events_path(self.cfg))
        self.assertEqual(record["extra"], {"error": "EACCES"})

    def test_extra_absent_when_there_is_nothing_to_say(self):
        log_event(self.cfg, "info", "search", "looked")
        (record,) = _records(audit.events_path(self.cfg))
        self.assertNotIn("extra", record)


class TestRotation(AuditTestBase):
    """B. 轮转：改名位移、不截断、份数有上限。"""

    def _line_size(self) -> int:
        """先写一条量出真实行宽，再据此设阈值 —— 不靠手算 JSON 开销。"""
        log_event(self.cfg, "info", "search", "record-0 " + PAD)
        return audit.events_path(self.cfg).stat().st_size

    def test_threshold_rotates_and_the_old_file_keeps_every_byte(self):
        size = self._line_size()
        self.set_rotation(max_bytes=size * 2 + 1, backups=3)
        for index in range(1, 6):
            log_event(self.cfg, "info", "search", f"record-{index} " + PAD)

        path = audit.events_path(self.cfg)
        first = path.with_name(path.name + ".1")
        second = path.with_name(path.name + ".2")
        self.assertTrue(first.exists(), "超阈值必须产生 .1")
        self.assertTrue(second.exists(), "6 条 / 每代 3 条 ⇒ 第二代也满了")

        seen = []
        for candidate in (second, first, path):
            for record in _records(candidate):
                seen.append(record["msg"])
        self.assertEqual(sorted(seen), sorted(f"record-{i} " + PAD for i in range(6)),
                         "轮转是改名，不是截断：一条都不能少")
        self.assertEqual(_records(second)[0]["msg"], "record-0 " + PAD)

    def test_only_three_history_files_are_kept(self):
        self.set_rotation(max_bytes=1, backups=3)
        for index in range(8):
            log_event(self.cfg, "info", "search", f"record-{index}")

        path = audit.events_path(self.cfg)
        generations = sorted(p.name for p in path.parent.iterdir()
                             if p.name.startswith(path.name + "."))
        self.assertEqual(generations, [f"{path.name}.1", f"{path.name}.2", f"{path.name}.3"])
        self.assertFalse(path.with_name(path.name + ".4").exists(), "第 4 份必须被覆盖")

        survivors = {record["msg"] for gen in generations for record in _records(path.parent / gen)}
        self.assertEqual(survivors, {"record-5", "record-6", "record-7"},
                         "留下最新的三份，最老的被覆盖")

    def test_zero_backups_discards_instead_of_keeping_history(self):
        self.set_rotation(max_bytes=1, backups=0)
        for index in range(3):
            log_event(self.cfg, "info", "search", f"record-{index}")
        path = audit.events_path(self.cfg)
        self.assertEqual([p.name for p in path.parent.iterdir()], [], "backups=0 = 不留历史")

    def test_events_and_audit_rotate_independently(self):
        self.set_rotation(max_bytes=200, backups=3)
        log_event(self.cfg, "info", "search", "e " + PAD)
        log_event(self.cfg, "info", "search", "e " + PAD)

        audit_event(self.cfg, "read", path="z0-public/page.md")

        self.assertTrue(audit.events_path(self.cfg).with_name(
            audit.events_path(self.cfg).name + ".1").exists(), "事件日志转过了")
        self.assertFalse(audit.audit_path(self.cfg).with_name(
            audit.audit_path(self.cfg).name + ".1").exists(), "审计日志按自己的大小走")
        self.assertEqual(len(_lines(audit.audit_path(self.cfg))), 1)


class TestFailureVisibility(AuditTestBase):
    """A/D 的失败面：写不进去要出声。"""

    def setUp(self):
        super().setUp()
        if hasattr(os, "geteuid") and os.geteuid() == 0:
            self.skipTest("root 无视权限位，这个用例证明不了任何事")
        self.logs = audit.log_dir(self.cfg)
        self.logs.mkdir(parents=True, exist_ok=True)
        self.addCleanup(lambda: os.chmod(self.logs, 0o700))
        os.chmod(self.logs, 0o500)

    def test_unwritable_directory_does_not_raise_but_says_so(self):
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            log_event(self.cfg, "info", "search", "cannot be written")

        self.assertNotIn("Traceback", stderr.getvalue())
        self.assertIn("log write failed", stderr.getvalue())
        self.assertIn("EACCES", stderr.getvalue(), "要给出 errno，不是一句泛泛的失败")

    def test_audit_failure_is_equally_loud_and_equally_harmless(self):
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            audit_event(self.cfg, "search", query="x", hits=0)
        self.assertIn("log write failed", stderr.getvalue())

    def test_text_logger_still_never_raises(self):
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            emit = file_logger(self.cfg)
            emit("mirror-sync: 12 files")
        self.assertIn("log write failed", stderr.getvalue(),
                      "文本那一路原本 except OSError: pass —— 现在必须出声")


class TestAudit(AuditTestBase):
    """C. 审计日志：谁、什么时候、看了什么；以及隐私红线。"""

    def test_query_is_truncated_to_200_characters(self):
        audit_event(self.cfg, "search", zone="all", query="q" * 500, hits=7,
                    actor="alice", transport="mcp")

        (record,) = _records(audit.audit_path(self.cfg))
        self.assertEqual(len(record["query"]), 200)
        self.assertEqual(record["query"], "q" * 200)
        self.assertEqual(record["actor"], "alice")
        self.assertEqual(record["transport"], "mcp")
        self.assertEqual(record["zone"], "all")
        self.assertEqual(record["hits"], 7)
        stamp = datetime.fromisoformat(record["ts"])
        self.assertIsNotNone(stamp.tzinfo)

    def test_defaults_and_schema_are_fixed(self):
        audit_event(self.cfg, "status")
        (record,) = _records(audit.audit_path(self.cfg))
        self.assertEqual(set(record), {"ts", "actor", "action", "zone", "query",
                                       "path", "hits", "lines", "transport"})
        self.assertEqual(record["actor"], "local")
        self.assertEqual(record["transport"], "cli")
        self.assertEqual(record["action"], "status")
        self.assertEqual(record["query"], "")
        self.assertIsNone(record["hits"])
        self.assertIsNone(record["lines"], "only a read carries a line count")

    def test_read_records_how_many_lines_were_read(self):
        """一条 read 记录若不知道读了多少行，就分不清「看了一眼」和「整页拉走」。"""
        audit_event(self.cfg, "read", path="z0-public/page.md", lines=42)
        (record,) = _records(audit.audit_path(self.cfg))
        self.assertEqual(record["lines"], 42)
        self.assertEqual(record["action"], "read")

    def test_path_is_recorded_relative_to_the_mirror(self):
        audit_event(self.cfg, "read", path=str(self.cfg.mirror_dir / "z0-public" / "page.md"))
        (record,) = _records(audit.audit_path(self.cfg))
        self.assertEqual(record["path"], "z0-public/page.md",
                         "绝对路径会把 home 布局泄进日志")

    def test_body_never_reaches_the_audit_file(self):
        """搜到一个含正文标记的页面，审计里只有"查过"，没有查到的东西。"""
        bundle = Path(self._tmp.name) / "bundle"
        page = bundle / "z0-public" / "mount.md"
        page.parent.mkdir(parents=True)
        page.write_text(
            "---\ntype: lesson\ntitle: 挂载点失效的处置\n---\n\n"
            "先看 /proc/mounts，再 mount -a。\n\n"
            f"{SENSITIVE_BODY}\n",
            encoding="utf-8",
        )
        cfg = Config(home=self.home, source="local", local_root=str(bundle))
        refresh(cfg)

        result = search(cfg, "挂载", zone="all", limit=5)
        self.assertTrue(result.hits, "先证明真的搜到了东西，否则下面的断言没有意义")
        self.assertTrue(any(SENSITIVE_BODY in hit.snippet for hit in result.hits),
                        "命中片段里确实带着正文标记 —— 它正是审计不该记的东西")

        audit_event(cfg, "search", zone="all", query="挂载", hits=len(result.hits),
                    transport="mcp", snippet=result.hits[0].snippet,
                    body=page.read_text(encoding="utf-8"))

        text = audit.audit_path(cfg).read_text(encoding="utf-8")
        self.assertIn("挂载", text, "查询串本身要记 —— 否则答不出「他在找什么」")
        self.assertNotIn(SENSITIVE_BODY, text, "正文标记绝不能出现在审计里")
        self.assertNotIn("type: lesson", text, "frontmatter 也不许进来")
        self.assertNotIn("先看 /proc/mounts", text, "片段也不许进来")

        (record,) = _records(audit.audit_path(cfg))
        self.assertEqual(set(record), {"ts", "actor", "action", "zone", "query",
                                       "path", "hits", "lines", "transport"},
                         "多传的 snippet/body 必须被丢掉，而不是换个键写进去")
        self.assertEqual(record["hits"], len(result.hits))

    def test_z2_wording_in_the_query_is_not_special_cased(self):
        """查询里的 ``z2`` 是用户输入，不做改写 —— 改写就等于答不出他查过什么。"""
        audit_event(self.cfg, "search", zone="z2", query="z2-sensitive 里的用药记录")
        (record,) = _records(audit.audit_path(self.cfg))
        self.assertEqual(record["query"], "z2-sensitive 里的用药记录")
        self.assertEqual(record["zone"], "z2")


class TestServiceIntegration(AuditTestBase):
    """D. ``file_logger`` 的对外行为：文本逐字节兼容 + 双写。"""

    def test_text_line_is_unchanged_and_the_json_twin_exists(self):
        emit = file_logger(self.cfg)
        emit("mirror-sync: 12 files fetched")

        lines = _lines(self.cfg.log_path)
        self.assertEqual(len(lines), 1)
        stamp, _, message = lines[0].partition(" ")
        self.assertEqual(message, "mirror-sync: 12 files fetched")
        self.assertIsNotNone(datetime.fromisoformat(stamp).tzinfo,
                             "前缀是带时区的 ISO8601 —— 既有格式不许动")

        (record,) = _records(audit.events_path(self.cfg))
        self.assertEqual(record["msg"], "mirror-sync: 12 files fetched")
        self.assertEqual(record["event"], "mirror-sync", "从消息里猜出来的事件名")
        self.assertEqual(record["level"], "info")

    def test_structured_false_keeps_the_legacy_single_log(self):
        emit = file_logger(self.cfg, structured=False)
        emit("index-build: 42 chunks")
        self.assertEqual(len(_lines(self.cfg.log_path)), 1)
        self.assertFalse(audit.events_path(self.cfg).exists())

    def test_explicit_event_wins_over_the_guess(self):
        emit = file_logger(self.cfg, event="index-build")
        emit("something the guesser would call log")
        (record,) = _records(audit.events_path(self.cfg))
        self.assertEqual(record["event"], "index-build")

    def test_failure_messages_are_guessed_as_warnings_and_errors(self):
        emit = file_logger(self.cfg)
        emit("auto-sync skipped: source unavailable")
        emit("auto-sync failed: TypeError: boom")
        warn, error = _records(audit.events_path(self.cfg))
        self.assertEqual(warn["level"], "warn")
        self.assertEqual(error["level"], "error")

    def test_service_exports_are_public(self):
        import scheda.service as service

        for name in ("log_event", "audit_event", "file_logger"):
            self.assertIn(name, service.__all__)


class TestConcurrency(AuditTestBase):
    """7. 两个线程各写 200 条：一行都不能坏。"""

    def test_two_threads_writing_four_hundred_lines(self):
        self.set_rotation(max_bytes=0, backups=0)   # 不轮转，专心测交错

        def worker(tag: str) -> None:
            for index in range(200):
                log_event(self.cfg, "info", "search", f"{tag}-{index}")

        threads = [threading.Thread(target=worker, args=(tag,)) for tag in ("a", "b")]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertFalse(any(thread.is_alive() for thread in threads))

        lines = _lines(audit.events_path(self.cfg))
        self.assertEqual(len(lines), 400)
        records = [json.loads(line) for line in lines]     # 每一行都必须能解析
        self.assertEqual({record["level"] for record in records}, {"info"})
        self.assertEqual(len({record["msg"] for record in records}), 400,
                         "没有两条写成同一行")


if __name__ == "__main__":
    unittest.main(verbosity=2)
