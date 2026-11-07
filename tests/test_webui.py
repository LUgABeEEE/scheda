"""Web UI tests — a real server on a real socket, no mocks.

The UI is the one place in scheda where untrusted input (a URL) turns into a
file path and into HTML, so these tests talk HTTP rather than calling the
functions directly: status codes, headers and the *bytes* a browser would
receive are the contract.  Coverage is deliberately shaped around the failures
that would matter in a review:

* the three pages render from the service layer (no second search engine);
* traversal and absolute paths are refused before and after the service call;
* every response carries the CSP and ``nosniff`` — including error pages;
* the document references no external resource at all;
* a pathological query string is refused instead of parsed;
* no write verb is accepted.

The fixture follows ``tests/test_e2e.py``: a temporary directory *is* the
"share", ``source=local``, Mirror + index built by ``service.refresh``.  No
network, no external service.
"""
from __future__ import annotations

import contextlib
import http.client
import io
import json
import shutil
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock
from urllib.parse import urlencode

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from scheda import webui  # noqa: E402
from scheda.config import Config  # noqa: E402
from scheda.service import refresh, status_payload  # noqa: E402
from scheda.webui import (  # noqa: E402
    CSP,
    MAX_QUERY_BYTES,
    make_server,
    main,
)

Z0_DOC = """---
type: lesson
title: 挂载点失效的处置
description: CIFS 挂载 stale 时如何恢复。
tags: [ops, cifs]
---

# 处置步骤

1. 先确认 `/proc/mounts` 里的选项是 `rw`。
2. 再执行 `mount -a` 让 systemd 重新挂载。

唯一标识 UNIQ-WEBUI-ALPHA-77777
"""

Z1_DOC = """---
type: preference
title: 交付偏好
description: 报告优先 HTML。
---

交付偏好：调研类报告要分清事实与推测，并尽量给引用。

唯一标识 UNIQ-WEBUI-BRAVO-22222
"""

Z2_DOC = """---
type: fact
title: 敏感分区自检
---

哨兵串 UNIQ-WEBUI-CANARY-99999 只允许显式点名时读取。
"""

CRED_DOC = """---
type: fact
title: 凭证存放说明
---

哨兵串 UNIQ-WEBUI-NEVER-00000 永远不得进入镜像或索引。
"""

#: A page long enough that the default window is not the whole file, so the
#: prev/next links are exercised with a real line count.
LONG_DOC = "# 长页面\n\n" + "\n".join(
    f"第 {i} 行 LONGPAGE 内容" for i in range(1, 121)
) + "\n"

#: ``splitlines`` count of LONG_DOC — what the reader calls "共 N 行".
LONG_TOTAL = len(LONG_DOC.splitlines())

FIXTURE = {
    "index.md": '---\nokf_version: "0.2"\n---\n\n# Bundle index\n',
    "z0-public/mount.md": Z0_DOC,
    "z0-public/long.md": LONG_DOC,
    "z1-restricted/delivery.md": Z1_DOC,
    "z2-sensitive/notes.md": Z2_DOC,
    "credentials/keys.md": CRED_DOC,
}


def make_home(root: Path) -> tuple[Config, Path]:
    """Build a fixture bundle plus a synced mirror and index."""
    bundle = root / "share"
    for rel, text in FIXTURE.items():
        path = bundle / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    cfg = Config(home=root / "home", source="local", local_root=str(bundle))
    refresh(cfg)
    return cfg, bundle


class WebUITestCase(unittest.TestCase):
    """Shared fixture: one mirror/index, one server bound to an ephemeral port."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="scheda-webui-"))
        cls.cfg, cls.bundle = make_home(cls.tmp)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def setUp(self):
        # port=0 → the OS picks a free port, so parallel test runs never collide.
        self.httpd = make_server(self.cfg, "127.0.0.1", 0, quiet=True)
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(
            target=self.httpd.serve_forever,
            kwargs={"poll_interval": 0.02},
            daemon=True,
        )
        self.thread.start()

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=5)

    # ---- transport -----------------------------------------------------
    def request(self, path: str, method: str = "GET"):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=15)
        try:
            conn.request(method, path)
            response = conn.getresponse()
            body = response.read()
            headers = {k.lower(): v for k, v in response.getheaders()}
            return response.status, headers, body.decode("utf-8", "replace")
        finally:
            conn.close()

    def get(self, path: str):
        return self.request(path, "GET")

    def page_link(self, path: str, **params) -> str:
        return "/page?" + urlencode({"path": path, **params})

    # ---- assertions used by several tests ------------------------------
    def assert_hardened(self, headers: dict) -> None:
        self.assertIn("content-security-policy", headers)
        self.assertEqual(headers["content-security-policy"], CSP)
        self.assertEqual(headers["content-security-policy"].split(";")[0].strip(),
                         "default-src 'none'")
        self.assertIn("style-src 'unsafe-inline'", headers["content-security-policy"])
        self.assertEqual(headers.get("x-content-type-options"), "nosniff")

    def assert_self_contained(self, body: str) -> None:
        """No CDN, no webfont, no external script, no image — nor an inline one."""
        lowered = body.lower()
        for marker in ("http://", "https://", "//cdn", "<script", "<link ",
                       "<iframe", "src=", "@import", "url("):
            self.assertNotIn(marker, lowered, f"页面引用了外部资源: {marker}")


class TestHome(WebUITestCase):
    def test_home_renders_status_and_zones(self):
        status, headers, body = self.get("/")
        self.assertEqual(status, 200)
        self.assert_hardened(headers)
        self.assertIn("zones", body)                    # 分区（zones）
        self.assertIn("z0-public", body)
        self.assertIn("z1-restricted", body)
        self.assertIn("搜索", body)
        self.assertIn('method="get"', body)
        self.assertIn('action="/search"', body)
        self.assert_self_contained(body)

    def test_home_reports_the_real_counters(self):
        expected = status_payload(self.cfg)
        body = self.get("/")[2]
        self.assertIn(f'{expected["index"]["pages"]} / {expected["index"]["chunks"]}', body)

    def test_home_never_leaks_sensitive_zone_content(self):
        body = self.get("/")[2]
        self.assertNotIn("UNIQ-WEBUI-CANARY-99999", body)
        self.assertNotIn("UNIQ-WEBUI-NEVER-00000", body)

    def test_home_is_mobile_friendly(self):
        body = self.get("/")[2]
        self.assertIn('name="viewport"', body)
        self.assertIn("@media (max-width:520px)", body)


class TestSecurityHeaders(WebUITestCase):
    def test_every_route_carries_the_security_headers(self):
        paths = ["/", "/search?" + urlencode({"q": "挂载"}),
                 self.page_link("z0-public/mount.md"),
                 "/healthz", "/nope", "/page?path=../../etc/passwd"]
        for path in paths:
            with self.subTest(path=path):
                status, headers, _ = self.get(path)
                self.assert_hardened(headers)
                self.assertIn(status, (200, 400, 403, 404))

    def test_unknown_route_is_404(self):
        status, headers, body = self.get("/no-such-route")
        self.assertEqual(status, 404)
        self.assert_hardened(headers)
        self.assertIn("页面不存在", body)


class TestSearch(WebUITestCase):
    def test_marker_query_hits_the_expected_page(self):
        status, headers, body = self.get("/search?q=UNIQ-WEBUI-ALPHA-77777")
        self.assertEqual(status, 200)
        self.assert_hardened(headers)
        self.assertIn("z0-public/mount.md", body)
        # every hit carries an "打开" link into /page
        self.assertIn("/page?path=z0-public%2Fmount.md", body)
        self.assertIn("打开", body)
        self.assert_self_contained(body)

    def test_chinese_query_hits(self):
        status, _headers, body = self.get("/search?" + urlencode({"q": "挂载"}))
        self.assertEqual(status, 200)
        self.assertIn("z0-public/mount.md", body)

    def test_zone_filter_is_honoured(self):
        # The z1 page is in the default selector but not in z0-public's zone.
        bravo = self.get("/search?" + urlencode({"q": "UNIQ-WEBUI-BRAVO-22222",
                                                "zone": "z0-public"}))[2]
        self.assertNotIn("z1-restricted/delivery.md", bravo)

    def test_limit_is_clamped(self):
        status, _headers, body = self.get("/search?" + urlencode(
            {"q": "LONGPAGE", "limit": "9999"}))
        self.assertEqual(status, 200)
        self.assertIn("LONGPAGE", body)

    def test_empty_query_renders_the_form(self):
        status, _headers, body = self.get("/search")
        self.assertEqual(status, 200)
        self.assertIn('method="get"', body)
        self.assertIn("未输入关键词", body)

    def test_unknown_zone_falls_back_instead_of_crashing(self):
        status, _headers, body = self.get("/search?" + urlencode(
            {"q": "挂载", "zone": "z9-nowhere"}))
        self.assertEqual(status, 200)
        self.assertIn("未知分区", body)
        self.assertIn("z0-public/mount.md", body)

    def test_oversized_query_is_refused(self):
        path = "/search?q=" + ("a" * (MAX_QUERY_BYTES + 100))
        status, headers, body = self.get(path)
        self.assertEqual(status, 400)
        self.assert_hardened(headers)
        self.assertIn("查询串过长", body)

    def test_many_parameters_are_refused(self):
        path = "/search?" + "&".join(f"p{i}=1" for i in range(200))
        self.assertEqual(self.get(path)[0], 400)


class TestPageView(WebUITestCase):
    def test_real_file_renders_with_line_numbers(self):
        status, headers, body = self.get(self.page_link("z0-public/mount.md"))
        self.assertEqual(status, 200)
        self.assert_hardened(headers)
        self.assertIn("挂载", body)
        self.assertIn('class="ln"', body)
        self.assertIn("第 1–", body)
        self.assert_self_contained(body)

    def test_window_and_pager(self):
        status, _headers, body = self.get(
            self.page_link("z0-public/long.md", start=1, lines=10))
        self.assertEqual(status, 200)
        self.assertIn("第 1–10 行", body)
        self.assertIn("下一页", body)
        self.assertIn("start=11", body)
        self.assertNotIn("上一页 →", body)

        status, _headers, tail = self.get(
            self.page_link("z0-public/long.md", start=115, lines=10))
        self.assertEqual(status, 200)
        self.assertIn(f"第 115–{LONG_TOTAL} 行", tail)
        self.assertIn(f"共 {LONG_TOTAL} 行", tail)
        self.assertIn("上一页", tail)
        self.assertIn("start=105", tail)
        self.assertNotIn("下一页 →</a>", tail)

    def test_lines_are_clamped_to_max_read_lines(self):
        status, _headers, body = self.get(
            self.page_link("z0-public/long.md", start=1, lines=100000))
        self.assertEqual(status, 200)
        self.assertIn(f"第 1–{min(self.cfg.max_read_lines, LONG_TOTAL)} 行", body)

    def test_missing_file_is_404(self):
        self.assertEqual(self.get(self.page_link("z0-public/nope.md"))[0], 404)

    def test_missing_path_parameter_is_400(self):
        self.assertEqual(self.get("/page")[0], 400)

    def test_traversal_is_refused(self):
        evil = [
            "../../etc/passwd",
            "/etc/passwd",
            "z0-public/../../etc/passwd",
            "../mirror",
            "z0-public/../../../home/etc/passwd",
            "C:\\Windows\\win.ini",
            "..\\..\\etc\\passwd",
            "z0-public/\x00mount.md",
        ]
        for rel in evil:
            with self.subTest(path=rel):
                status, headers, body = self.get(self.page_link(rel))
                self.assertIn(status, (403, 404), rel)
                self.assertNotIn("root:", body)
                self.assert_hardened(headers)

    def test_absolute_path_never_reads_outside_the_mirror(self):
        outside = self.tmp / "outside.md"
        outside.write_text("UNIQ-OUTSIDE-SECRET\n", encoding="utf-8")
        for rel in (str(outside), "../outside.md", "../../" + outside.name):
            with self.subTest(path=rel):
                status, _headers, body = self.get(self.page_link(rel))
                self.assertIn(status, (403, 404))
                self.assertNotIn("UNIQ-OUTSIDE-SECRET", body)

    def test_path_parameter_is_escaped_not_rendered(self):
        status, _headers, body = self.get(
            self.page_link("<script>alert(1)</script>"))
        self.assertEqual(status, 404)
        self.assertNotIn("<script>alert(1)</script>", body)
        self.assertIn("&lt;script&gt;", body)

    def test_zone_argument_is_escaped(self):
        status, _headers, body = self.get(
            "/search?" + urlencode({"zone": "<script>alert(1)</script>"}))
        self.assertEqual(status, 200)
        self.assertNotIn("<script>alert(1)</script>", body)
        self.assertIn("&lt;script&gt;", body)


class TestHealth(WebUITestCase):
    def test_healthz_is_json_and_ok(self):
        status, headers, body = self.get("/healthz")
        self.assertEqual(status, 200)
        self.assertEqual(headers.get("content-type"), "application/json; charset=utf-8")
        self.assert_hardened(headers)
        payload = json.loads(body)
        self.assertIs(payload["ok"], True)
        self.assertGreaterEqual(payload["pages"], 1)
        self.assertGreaterEqual(payload["chunks"], 1)
        self.assertGreaterEqual(payload["index_age_s"], 0)
        self.assertGreaterEqual(payload["mirror_age_s"], 0)

    def test_healthz_matches_the_service_layer(self):
        expected = status_payload(self.cfg)
        payload = json.loads(self.get("/healthz")[2])
        self.assertEqual(payload["pages"], expected["index"]["pages"])
        self.assertEqual(payload["chunks"], expected["index"]["chunks"])


class TestReadOnly(WebUITestCase):
    def test_write_verbs_are_refused(self):
        for method in ("POST", "PUT", "DELETE", "PATCH"):
            with self.subTest(method=method):
                status, headers, _body = self.request("/search?q=x", method=method)
                self.assertEqual(status, 405)
                self.assert_hardened(headers)
                self.assertEqual(headers.get("allow"), "GET, HEAD")

    def test_head_has_headers_but_no_body(self):
        status, headers, body = self.request("/", "HEAD")
        self.assertEqual(status, 200)
        self.assertEqual(body, "")
        self.assertGreater(int(headers["content-length"]), 0)
        self.assert_hardened(headers)


class _StubServer:
    """Stands in for the real server so ``serve`` can be tested without a socket."""

    server_address = ("127.0.0.1", 8765)

    def __init__(self, interrupt: bool = True):
        self.interrupt = interrupt
        self.closed = False
        self.polled: float | None = None

    def serve_forever(self, poll_interval: float = 0.5) -> None:  # noqa: D401
        self.polled = poll_interval
        if self.interrupt:
            raise KeyboardInterrupt

    def server_close(self) -> None:
        self.closed = True


class TestServeContract(WebUITestCase):
    """``serve`` blocking/exit behaviour, without binding a second socket."""

    def test_keyboard_interrupt_is_a_normal_exit(self):
        stub = _StubServer()
        err = io.StringIO()
        with mock.patch.object(webui, "make_server", return_value=stub), \
                contextlib.redirect_stderr(err):
            # serve() 返回 bool：True = 正常退出（Ctrl-C），False = 拒绝启动（--auth 无用户库）
            self.assertIsNot(webui.serve(self.cfg, "127.0.0.1", 0, quiet=True), False)
        self.assertTrue(stub.closed)
        self.assertEqual(err.getvalue(), "")           # --quiet prints no banner

    def test_banner_names_the_real_port(self):
        stub = _StubServer(interrupt=False)
        err = io.StringIO()
        with mock.patch.object(webui, "make_server", return_value=stub), \
                contextlib.redirect_stderr(err):
            webui.serve(self.cfg, "127.0.0.1", 0, quiet=False)
        banner = err.getvalue()
        self.assertIn("http://127.0.0.1:8765/", banner)
        self.assertIn(str(self.cfg.home), banner)
        self.assertNotIn("警告", banner)               # loopback needs no warning

    def test_binding_a_public_interface_warns_even_when_quiet(self):
        stub = _StubServer(interrupt=False)
        err = io.StringIO()
        with mock.patch.object(webui, "make_server", return_value=stub), \
                contextlib.redirect_stderr(err):
            webui.serve(self.cfg, "0.0.0.0", 8765, quiet=True)
        warning = err.getvalue()
        self.assertIn("警告", warning)
        self.assertIn("无任何鉴权", warning)
        self.assertIn("明文 HTTP", warning)


class TestEntryPoint(unittest.TestCase):
    def test_invalid_port_is_rejected(self):
        with contextlib.redirect_stderr(io.StringIO()), \
                self.assertRaises(SystemExit):
            main(["--port", "70000"])

    def test_help_exits_zero(self):
        with contextlib.redirect_stdout(io.StringIO()), \
                self.assertRaises(SystemExit) as caught:
            main(["--help"])
        self.assertEqual(caught.exception.code, 0)


if __name__ == "__main__":
    unittest.main()
