"""鉴权与会话测试 —— 真服务器、真 cookie、无 mock。

这个文件只证明三件事，但每一件都必须用「浏览器会收到什么字节」来证明，
而不是「函数返回了什么」：

1. **默认关闭是零影响**：没有用户库时 ``/`` 与 ``/healthz`` 照旧 200，
   连 ``/login`` 这个路由都不存在，``POST`` 仍是 405。个人用户升级后察觉不到
   这个功能存在。
2. **打开之后是关着的**：未登录 302 到 ``/login``；错误口令的文案不区分
   「用户不存在」；``/login`` 的 POST 缺 ``Origin``/``Referer`` 直接拒绝。
3. **分区授权在服务端判定**：``reader`` + ``zones=["z0-public"]`` 拿 ``zone=z1``
   是 403，不是「空结果」；默认选择器被收窄；``/page`` 不能绕过搜索直接读；
   返回体里不出现未授权分区的路径。

夹具沿用 ``tests/test_webui.py``：临时目录就是「共享盘」，``source=local``，
``service.refresh`` 建镜像与索引。网络只有 127.0.0.1 的临时端口。
"""
from __future__ import annotations

import json
import os
import re
import shutil
import stat
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from urllib.parse import urlencode

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from scheda import auth, webui  # noqa: E402
from scheda.config import Config  # noqa: E402
from scheda.service import refresh  # noqa: E402

#: 两个分区共有的词：用来证明「默认选择器会被收窄」，而不是「搜索碰巧没命中」。
SHARED = "SHAREDWORD"

Z0_DOC = f"""---
type: lesson
title: 挂载点失效的处置
---

公共分区的处置步骤，关键词 {SHARED}。

唯一标识 UNIQ-AUTH-ALPHA-77777
"""

Z1_DOC = f"""---
type: preference
title: 交付偏好
---

受限分区的交付偏好，关键词 {SHARED}。

唯一标识 UNIQ-AUTH-BRAVO-22222
"""

Z2_DOC = """---
type: fact
title: 敏感分区自检
---

哨兵串 UNIQ-AUTH-CANARY-99999 只允许管理员或显式授权读取。
"""

FIXTURE = {
    "index.md": '---\nokf_version: "0.2"\n---\n\n# Bundle index\n',
    "z0-public/mount.md": Z0_DOC,
    "z1-restricted/delivery.md": Z1_DOC,
    "z2-sensitive/notes.md": Z2_DOC,
}

#: alice 只有 z0；bob 有 z0+z1；carol 是 admin（忽略 zones）。
PASSWORDS = {
    "alice": "alice-口令-1",
    "bob": "bob-口令-2",
    "carol": "carol-口令-3",
}


def make_home(root: Path) -> Config:
    """一个临时目录当共享盘：写文档 → sync + index。"""
    bundle = root / "share"
    for rel, text in FIXTURE.items():
        path = bundle / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    cfg = Config(home=root / "home", source="local", local_root=str(bundle))
    refresh(cfg)
    return cfg


def provision(cfg: Config) -> None:
    """建三个用户，覆盖三种角色与两种 zones 组合。"""
    auth.add_user(cfg, "alice", PASSWORDS["alice"], role="reader",
                  zones=["z0-public"])
    auth.add_user(cfg, "bob", PASSWORDS["bob"], role="writer",
                  zones=["z0-public", "z1"])
    auth.add_user(cfg, "carol", PASSWORDS["carol"], role="admin", zones=[])


def cookie_from(set_cookies, name: str) -> str | None:
    """从若干 ``Set-Cookie`` 值里取出某个 cookie 的值。"""
    for raw in set_cookies:
        head = raw.split(";", 1)[0]
        key, _, value = head.partition("=")
        if key.strip() == name:
            return value
    return None


# --------------------------------------------------------------------------- #
# 1. 口令哈希（不需要服务器）
# --------------------------------------------------------------------------- #
class PasswordTests(unittest.TestCase):
    def test_same_password_gets_a_fresh_salt_every_time(self):
        salt_a, hash_a = auth.hash_password("同一个口令")
        salt_b, hash_b = auth.hash_password("同一个口令")
        self.assertNotEqual(salt_a, salt_b, "两次派生必须用不同的盐")
        self.assertNotEqual(hash_a, hash_b, "盐不同 ⇒ 派生值也不同")
        self.assertEqual(len(bytes.fromhex(salt_a)), auth.SALT_BYTES)
        self.assertEqual(len(bytes.fromhex(hash_a)), auth.SCRYPT_DKLEN)
        # 同一个盐必须可复现（否则谁也验证不了）
        again = auth.hash_password("同一个口令", salt=bytes.fromhex(salt_a))[1]
        self.assertEqual(again, hash_a)

    def test_verify_accepts_the_right_password_and_refuses_others(self):
        salt, digest = auth.hash_password("correct horse")
        self.assertTrue(auth.verify_password("correct horse", salt, digest))
        self.assertFalse(auth.verify_password("correct hors", salt, digest))
        self.assertFalse(auth.verify_password("", salt, digest))
        self.assertFalse(auth.verify_password("CORRECT HORSE", salt, digest))

    def test_verify_does_not_raise_on_a_corrupt_record(self):
        """``users.json`` 被手改坏 → 这个人登不进来，而不是整个鉴权层 500。"""
        for salt, digest in (("", ""), ("zz", "zz"), (None, None),
                             ("ab", "cd")):
            with self.subTest(salt=salt, digest=digest):
                self.assertFalse(auth.verify_password("x", salt, digest))

    def test_the_stored_hash_is_not_the_password(self):
        salt, digest = auth.hash_password("plain-text-secret")
        self.assertNotIn("plain-text-secret", salt + digest)

    def test_scrypt_parameters_are_the_documented_ones(self):
        """``n=2**14, r=8, p=1`` 是承诺值：改小等于悄悄降低抗暴力成本。"""
        self.assertEqual((auth.SCRYPT_N, auth.SCRYPT_R, auth.SCRYPT_P),
                         (2 ** 14, 8, 1))


# --------------------------------------------------------------------------- #
# 2. 开关与文件权限（不需要服务器）
# --------------------------------------------------------------------------- #
class StoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="scheda-auth-store-"))
        self.cfg = Config(home=self.tmp / "home")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_enabled_is_false_without_a_user_store(self):
        self.assertFalse(auth.enabled(self.cfg))
        # 就算有人显式要求，没有用户库也必须是关闭的 —— 否则没人能登录。
        self.assertFalse(auth.enabled(self.cfg, explicit=True))
        self.assertFalse(auth.users_path(self.cfg).exists())

    def test_enabled_needs_an_explicit_ask_even_when_the_store_exists(self):
        provision(self.cfg)
        self.assertFalse(auth.enabled(self.cfg, explicit=False),
                         "显式否定压倒一切")
        self.assertTrue(auth.enabled(self.cfg, explicit=True))
        # 文件存在但没有 --auth / 环境变量 ⇒ 仍然关闭（避免误锁）。
        self.assertFalse(auth.enabled(self.cfg, explicit=None))

    def test_the_environment_switch_counts_as_an_explicit_ask(self):
        provision(self.cfg)
        with _env(auth.ENV_FLAG, "on"):
            self.assertTrue(auth.enabled(self.cfg))
        with _env(auth.ENV_FLAG, "off"):
            self.assertFalse(auth.enabled(self.cfg))

    def test_the_user_store_is_created_private(self):
        auth.add_user(self.cfg, "alice", "s3cret", role="reader",
                      zones=["z0-public"])
        path = auth.users_path(self.cfg)
        mode = stat.S_IMODE(path.stat().st_mode)
        self.assertEqual(mode, 0o600, f"{path} 必须是 0600，实际 {oct(mode)}")
        text = path.read_text(encoding="utf-8")
        self.assertNotIn("s3cret", text, "用户库里不能有明文口令")
        record = json.loads(text)["users"][0]
        self.assertEqual(record["name"], "alice")
        self.assertEqual(record["role"], "reader")
        self.assertEqual(record["zones"], ["z0-public"])
        self.assertEqual(sorted(record), ["created", "hash", "name", "role",
                                          "salt", "zones"])

    def test_a_leftover_wide_file_is_tightened_on_read(self):
        provision(self.cfg)
        path = auth.users_path(self.cfg)
        os.chmod(path, 0o644)                     # 模拟升级前留下的文件
        auth.load_users(self.cfg)
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)

    def test_bad_names_and_empty_passwords_are_refused(self):
        for name in ("", "a b", "a/b", "x" * 65, "../../etc", "ali ce"):
            with self.subTest(name=name):
                with self.assertRaises(ValueError):
                    auth.add_user(self.cfg, name, "s3cret")
        with self.assertRaises(ValueError):
            auth.add_user(self.cfg, "alice", "")

    def test_the_role_table_is_the_single_source_of_truth(self):
        self.assertTrue(auth.has_capability("reader", "search"))
        self.assertFalse(auth.has_capability("reader", "push"))
        self.assertTrue(auth.has_capability("writer", "push"))
        self.assertFalse(auth.has_capability("writer", "admin"))
        self.assertTrue(auth.has_capability("admin", "admin"))
        # 写错角色必须是「权限变小」
        self.assertEqual(auth.normalise_role("Admin?"), "reader")
        self.assertEqual(auth.normalise_role(None), "reader")
        self.assertFalse(auth.has_capability("root", "admin"))

    def test_zone_strings_expand_against_the_config(self):
        self.assertEqual(auth.expand_zones(self.cfg, ["z1"]), ("z1-restricted",))
        self.assertEqual(auth.expand_zones(self.cfg, ["z9-nowhere"]), ())
        self.assertIn("z2-sensitive", auth.expand_zones(self.cfg, ["all"]))
        self.assertNotIn("z2-sensitive", auth.expand_zones(self.cfg, ["z01"]))

    def test_authorisation_helpers(self):
        provision(self.cfg)
        alice = auth.find_user(self.cfg, "alice")
        carol = auth.find_user(self.cfg, "carol")
        self.assertEqual(auth.allowed_zones(alice, self.cfg), ("z0-public",))
        self.assertIn("z2-sensitive", auth.allowed_zones(carol, self.cfg))
        self.assertTrue(auth.zone_allows_path(alice, self.cfg, "z0-public/x.md"))
        self.assertFalse(auth.zone_allows_path(alice, self.cfg, "z1-restricted/x.md"))
        # 结构目录不是隐私分区
        self.assertTrue(auth.zone_allows_path(alice, self.cfg, "_meta/x.md"))
        self.assertTrue(auth.zone_allows_path(alice, self.cfg, "_incoming/p.md"))


# --------------------------------------------------------------------------- #
# 3. 会话（不需要服务器）
# --------------------------------------------------------------------------- #
class SessionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="scheda-auth-session-"))
        self.cfg = Config(home=self.tmp / "home")
        provision(self.cfg)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_issue_then_resolve(self):
        user = auth.find_user(self.cfg, "alice")
        token = auth.issue_session(self.cfg, user)
        self.assertGreaterEqual(len(token), 32)
        session = auth.resolve_session(self.cfg, token)
        self.assertIsNotNone(session)
        self.assertEqual(session["user"], "alice")
        self.assertEqual(session["role"], "reader")
        self.assertEqual(session["zones"], ["z0-public"])
        self.assertGreater(session["expires"], 0)

    def test_unknown_and_empty_tokens_resolve_to_nothing(self):
        self.assertIsNone(auth.resolve_session(self.cfg, ""))
        self.assertIsNone(auth.resolve_session(self.cfg, None))
        self.assertIsNone(auth.resolve_session(self.cfg, "not-a-real-token"))

    def test_an_expired_session_does_not_resolve(self):
        user = auth.find_user(self.cfg, "alice")
        token = auth.issue_session(self.cfg, user, ttl=-1)      # 签发即过期
        self.assertIsNone(auth.resolve_session(self.cfg, token))
        self.assertEqual(auth.purge_expired(self.cfg), 1)
        self.assertEqual(auth.active_sessions(self.cfg), [])

    def test_revoke_kills_the_session(self):
        user = auth.find_user(self.cfg, "alice")
        token = auth.issue_session(self.cfg, user)
        self.assertTrue(auth.revoke_session(self.cfg, token))
        self.assertIsNone(auth.resolve_session(self.cfg, token))
        self.assertFalse(auth.revoke_session(self.cfg, token))

    def test_the_session_file_never_contains_the_plaintext_token(self):
        user = auth.find_user(self.cfg, "alice")
        token = auth.issue_session(self.cfg, user)
        path = auth.sessions_path(self.cfg)
        raw = path.read_text(encoding="utf-8")
        self.assertNotIn(token, raw, "会话库只允许存 token 的 sha256")
        self.assertIn(auth.token_hash(token), raw)
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        # 记录形状：{token_hash, user, created, expires}，没有别的字段能藏明文
        record = json.loads(raw)["sessions"][0]
        self.assertEqual(sorted(record), ["created", "expires", "token_hash", "user"])

    def test_a_deleted_user_loses_access_immediately(self):
        user = auth.find_user(self.cfg, "alice")
        token = auth.issue_session(self.cfg, user)
        kept = [u for u in auth.load_users(self.cfg) if u["name"] != "alice"]
        auth.save_users(self.cfg, kept)
        self.assertIsNone(auth.resolve_session(self.cfg, token))

    def test_rotating_the_password_revokes_every_session(self):
        user = auth.find_user(self.cfg, "alice")
        first = auth.issue_session(self.cfg, user)
        second = auth.issue_session(self.cfg, user)
        self.assertTrue(auth.set_password(self.cfg, "alice", "new-口令"))
        self.assertIsNone(auth.resolve_session(self.cfg, first))
        self.assertIsNone(auth.resolve_session(self.cfg, second))
        self.assertFalse(auth.set_password(self.cfg, "nobody", "x"))


# --------------------------------------------------------------------------- #
# HTTP 夹具
# --------------------------------------------------------------------------- #
class _Env:
    """Temporarily set (or unset) one environment variable."""

    def __init__(self, name: str, value: str | None):
        self.name, self.value = name, value
        self.saved = None

    def __enter__(self):
        self.saved = os.environ.get(self.name)
        if self.value is None:
            os.environ.pop(self.name, None)
        else:
            os.environ[self.name] = self.value
        return self

    def __exit__(self, *_exc):
        if self.saved is None:
            os.environ.pop(self.name, None)
        else:
            os.environ[self.name] = self.saved
        return False


_env = _Env


class ServerCase(unittest.TestCase):
    """One home, one server per test, real sockets and real cookies."""

    #: 传给 make_server 的三态开关：本基类的子类各自决定。
    auth_flag: bool | None = None
    provision_users: bool = True

    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="scheda-auth-http-"))
        cls.cfg = make_home(cls.tmp)
        if cls.provision_users:
            provision(cls.cfg)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def setUp(self):
        self.httpd = webui.make_server(self.cfg, "127.0.0.1", 0, quiet=True,
                                       auth_flag=self.auth_flag)
        self.port = self.httpd.server_address[1]
        self.origin = f"http://127.0.0.1:{self.port}"
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
    def request(self, path: str, method: str = "GET", *, form: dict | None = None,
                headers: dict | None = None, cookies: dict | None = None) -> dict:
        import http.client

        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=20)
        try:
            extra = dict(headers or {})
            if cookies:
                extra["Cookie"] = "; ".join(f"{k}={v}" for k, v in cookies.items())
            body = None
            if form is not None:
                body = urlencode(form)
                extra.setdefault("Content-Type",
                                 "application/x-www-form-urlencoded")
            conn.request(method, path, body=body, headers=extra)
            response = conn.getresponse()
            raw_headers = response.getheaders()
            text = response.read().decode("utf-8", "replace")
        finally:
            conn.close()
        return {
            "status": response.status,
            "headers": {k.lower(): v for k, v in raw_headers},
            "set_cookies": [v for k, v in raw_headers if k.lower() == "set-cookie"],
            "body": text,
        }

    def get(self, path: str, *, cookies: dict | None = None) -> dict:
        return self.request(path, "GET", cookies=cookies)

    def page_link(self, path: str, **params) -> str:
        return "/page?" + urlencode({"path": path, **params})

    # ---- login helpers -------------------------------------------------
    def login_page(self) -> tuple[dict, str, str]:
        """GET ``/login`` → (响应, csrf cookie, 表单里的 csrf 值)."""
        page = self.get("/login")
        self.assertEqual(page["status"], 200, page["body"][:400])
        csrf_cookie = cookie_from(page["set_cookies"], webui.CSRF_COOKIE)
        match = re.search(r'name="csrf" value="([^"]*)"', page["body"])
        self.assertIsNotNone(match, "登录页必须带 CSRF 隐藏域")
        return page, csrf_cookie, match.group(1)

    def login(self, name: str, password: str, *, csrf: str | None = None,
              origin: bool = True) -> dict:
        _page, csrf_cookie, token = self.login_page()
        form = {"username": name, "password": password,
                "csrf": token if csrf is None else csrf}
        headers = {"Origin": self.origin} if origin else {}
        cookies = {webui.CSRF_COOKIE: csrf_cookie} if csrf_cookie else {}
        return self.request("/login", "POST", form=form, headers=headers,
                            cookies=cookies)

    def session_cookie(self, response: dict) -> str | None:
        return cookie_from(response["set_cookies"], webui.AUTH_COOKIE)

    def audit_records(self) -> list[dict]:
        path = self.cfg.home / "logs" / "audit.jsonl"
        if not path.exists():
            return []
        out = []
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line:
                out.append(json.loads(line))
        return out

    # ---- assertions ----------------------------------------------------
    def assert_hardened(self, headers: dict) -> None:
        self.assertEqual(headers.get("content-security-policy"), webui.CSP)
        self.assertEqual(headers.get("x-content-type-options"), "nosniff")
        self.assertEqual(headers.get("x-frame-options"), "DENY")


# --------------------------------------------------------------------------- #
# 4. 默认关闭：与改造前逐字节一致
# --------------------------------------------------------------------------- #
class TestDefaultOff(ServerCase):
    """没有开关、没有用户库 —— 这就是个人用户机器上的样子。"""

    auth_flag = None
    provision_users = False

    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="scheda-auth-off-"))
        cls.cfg = make_home(cls.tmp)

    def test_home_and_healthz_answer_without_any_login(self):
        home = self.get("/")
        self.assertEqual(home["status"], 200)
        self.assert_hardened(home["headers"])
        self.assertIn("zones", home["body"])
        self.assertIn("搜索", home["body"])
        self.assertNotIn("登录", home["body"])
        self.assertNotIn('action="/login"', home["body"])

        health = self.get("/healthz")
        self.assertEqual(health["status"], 200)
        self.assertIs(json.loads(health["body"])["ok"], True)

    def test_search_and_page_are_open_as_before(self):
        found = self.get("/search?" + urlencode({"q": "UNIQ-AUTH-ALPHA-77777"}))
        self.assertEqual(found["status"], 200)
        self.assertIn("z0-public/mount.md", found["body"])
        # 默认选择器照旧覆盖 z1（没有任何分区收窄）
        both = self.get("/search?" + urlencode({"q": SHARED}))
        self.assertIn("z1-restricted/delivery.md", both["body"])
        self.assertEqual(self.get(self.page_link("z1-restricted/delivery.md"))["status"], 200)

    def test_the_login_route_does_not_exist(self):
        self.assertEqual(self.get("/login")["status"], 404)
        self.assertIn("页面不存在", self.get("/login")["body"])

    def test_post_is_still_method_not_allowed(self):
        for path in ("/login", "/logout", "/search?q=x"):
            with self.subTest(path=path):
                response = self.request(path, "POST", form={"username": "alice"})
                self.assertEqual(response["status"], 405)
                self.assertEqual(response["headers"].get("allow"), "GET, HEAD")
                self.assertEqual(response["set_cookies"], [])

    def test_a_user_store_alone_does_not_turn_the_login_on(self):
        """误锁保护：有 users.json 但没人说 --auth ⇒ 仍然是开放只读界面。"""
        other = self.tmp / "with-users"
        cfg = make_home(other)
        provision(cfg)
        httpd = webui.make_server(cfg, "127.0.0.1", 0, quiet=True, auth_flag=None)
        port = httpd.server_address[1]
        thread = threading.Thread(target=httpd.serve_forever,
                                  kwargs={"poll_interval": 0.02}, daemon=True)
        thread.start()
        try:
            saved = self.port
            self.port = port
            response = self.get("/")
            self.port = saved
        finally:
            httpd.shutdown()
            httpd.server_close()
            thread.join(timeout=5)
        self.assertEqual(response["status"], 200)
        self.assertNotIn('action="/login"', response["body"])


# --------------------------------------------------------------------------- #
# 5. 打开之后：未登录什么都看不到
# --------------------------------------------------------------------------- #
class TestLoginGate(ServerCase):
    auth_flag = True

    def test_anonymous_gets_a_redirect_to_the_login_page(self):
        for path in ("/", "/search?q=x", self.page_link("z0-public/mount.md")):
            with self.subTest(path=path):
                response = self.get(path)
                self.assertEqual(response["status"], 302)
                self.assertEqual(response["headers"].get("location"), "/login")
                self.assertEqual(response["body"], "")

    def test_healthz_needs_no_session(self):
        response = self.get("/healthz")
        self.assertEqual(response["status"], 200)
        payload = json.loads(response["body"])
        self.assertIs(payload["ok"], True)
        self.assertGreaterEqual(payload["pages"], 1)

    def test_the_login_page_is_self_contained_and_hints_password_managers(self):
        page, csrf_cookie, token = self.login_page()
        self.assertTrue(page["set_cookies"], "登录页必须种下 CSRF cookie")
        self.assertTrue(csrf_cookie)
        self.assertEqual(csrf_cookie, token)
        for marker in ("HttpOnly", "SameSite=Strict", "Path=/"):
            self.assertIn(marker, page["set_cookies"][0])
        self.assertIn('autocomplete="current-password"', page["body"])
        self.assertIn('autocomplete="username"', page["body"])
        self.assertIn("<style>", page["body"])
        lowered = page["body"].lower()
        for marker in ("http://", "https://", "<script", "<link ", "src="):
            self.assertNotIn(marker, lowered, f"登录页引用了外部资源: {marker}")

    def test_successful_login_sets_a_hardened_session_cookie(self):
        response = self.login("alice", PASSWORDS["alice"])
        self.assertEqual(response["status"], 302)
        self.assertEqual(response["headers"].get("location"), "/")
        cookie = cookie_from(response["set_cookies"], webui.AUTH_COOKIE)
        self.assertTrue(cookie, "登录成功必须发会话 cookie")
        raw = [c for c in response["set_cookies"]
               if c.split(";", 1)[0].startswith(webui.AUTH_COOKIE + "=")][0]
        self.assertIn("HttpOnly", raw)
        self.assertIn("SameSite=Strict", raw)
        self.assertIn("Path=/", raw)
        self.assertIn("Max-Age=", raw)
        # 会话可用
        home = self.get("/", cookies={webui.AUTH_COOKIE: cookie})
        self.assertEqual(home["status"], 200)
        self.assertIn("退出", home["body"])

    def test_a_wrong_password_says_exactly_one_thing(self):
        wrong = self.login("alice", "not-the-password")
        self.assertEqual(wrong["status"], 401)
        self.assertIn("用户名或密码错误", wrong["body"])
        self.assertNotIn("用户不存在", wrong["body"])
        self.assertIsNone(cookie_from(wrong["set_cookies"], webui.AUTH_COOKIE))

        unknown = self.login("nobody", "whatever")
        self.assertEqual(unknown["status"], 401)
        self.assertIn("用户名或密码错误", unknown["body"])
        # 两种失败的文案必须一模一样（不做用户存在性区分）
        self.assertIn("用户名或密码错误", unknown["body"])
        self.assertNotIn("nobody", re.sub(r'name="username"[^>]*', "", unknown["body"]))

    def test_post_without_origin_or_referer_is_refused(self):
        _page, csrf_cookie, token = self.login_page()
        before = len(auth.active_sessions(self.cfg))
        response = self.request(
            "/login", "POST",
            form={"username": "alice", "password": PASSWORDS["alice"],
                  "csrf": token},
            cookies={webui.CSRF_COOKIE: csrf_cookie},
        )
        self.assertEqual(response["status"], 403)
        self.assertIn("Origin", response["body"])
        self.assertIsNone(cookie_from(response["set_cookies"], webui.AUTH_COOKIE))
        self.assertEqual(len(auth.active_sessions(self.cfg)), before,
                         "被拒绝的 POST 不得签发会话")

    def test_a_mismatched_csrf_token_is_refused(self):
        response = self.login("alice", PASSWORDS["alice"], csrf="forged-token")
        self.assertEqual(response["status"], 403)
        self.assertIsNone(cookie_from(response["set_cookies"], webui.AUTH_COOKIE))

    def test_a_forged_origin_is_refused(self):
        response = self.login("alice", PASSWORDS["alice"])
        self.assertEqual(response["status"], 302)          # 同源 POST 才行
        page, csrf_cookie, token = self.login_page()
        forged = self.request(
            "/login", "POST",
            form={"username": "alice", "password": PASSWORDS["alice"],
                  "csrf": token},
            headers={"Origin": "http://evil.example"},
            cookies={webui.CSRF_COOKIE: csrf_cookie},
        )
        self.assertEqual(forged["status"], 403)

    def test_logout_revokes_the_session_server_side(self):
        before = len(auth.active_sessions(self.cfg))
        login = self.login("alice", PASSWORDS["alice"])
        token = cookie_from(login["set_cookies"], webui.AUTH_COOKIE)
        self.assertEqual(self.get("/", cookies={webui.AUTH_COOKIE: token})["status"], 200)

        out = self.request("/logout", "POST", headers={"Origin": self.origin},
                           cookies={webui.AUTH_COOKIE: token})
        self.assertEqual(out["status"], 302)
        self.assertEqual(out["headers"].get("location"), "/login")
        expired = [c for c in out["set_cookies"]
                   if c.split(";", 1)[0].startswith(webui.AUTH_COOKIE + "=")]
        self.assertTrue(expired[0].endswith("Max-Age=0"))

        # 服务端已撤销：旧 cookie 即使是原样重放也不再有效
        again = self.get("/", cookies={webui.AUTH_COOKIE: token})
        self.assertEqual(again["status"], 302)
        self.assertEqual(again["headers"].get("location"), "/login")
        # 类级 home 是共享的，前面的用例可能留下别的会话；这条用例只负责
        # 证明「本次会话少了一条」，而不是「全局清零」。
        self.assertEqual(len(auth.active_sessions(self.cfg)), before)

    def test_logout_without_a_same_origin_header_is_refused(self):
        login = self.login("alice", PASSWORDS["alice"])
        token = cookie_from(login["set_cookies"], webui.AUTH_COOKIE)
        response = self.request("/logout", "POST",
                                cookies={webui.AUTH_COOKIE: token})
        self.assertEqual(response["status"], 403)
        self.assertEqual(self.get("/", cookies={webui.AUTH_COOKIE: token})["status"], 200)

    def test_other_posts_are_still_refused(self):
        login = self.login("alice", PASSWORDS["alice"])
        token = cookie_from(login["set_cookies"], webui.AUTH_COOKIE)
        for path in ("/search?q=x", "/page?path=z0-public/mount.md", "/"):
            with self.subTest(path=path):
                response = self.request(path, "POST", headers={"Origin": self.origin},
                                        cookies={webui.AUTH_COOKIE: token})
                self.assertEqual(response["status"], 405)
                self.assertEqual(response["headers"].get("allow"), "GET, HEAD")

    def test_an_unknown_route_needs_a_session_too(self):
        self.assertEqual(self.get("/nope")["status"], 302)
        login = self.login("alice", PASSWORDS["alice"])
        token = cookie_from(login["set_cookies"], webui.AUTH_COOKIE)
        response = self.get("/nope", cookies={webui.AUTH_COOKIE: token})
        self.assertEqual(response["status"], 404)
        self.assertIn("页面不存在", response["body"])


# --------------------------------------------------------------------------- #
# 6. 分区授权
# --------------------------------------------------------------------------- #
class TestZoneAuthorisation(ServerCase):
    auth_flag = True

    def cookie(self, name: str) -> str:
        response = self.login(name, PASSWORDS[name])
        self.assertEqual(response["status"], 302, f"{name} 应该能登录")
        token = cookie_from(response["set_cookies"], webui.AUTH_COOKIE)
        self.assertTrue(token)
        return token

    def test_a_reader_cannot_ask_for_a_forbidden_zone(self):
        token = self.cookie("alice")
        response = self.get("/search?" + urlencode({"q": SHARED, "zone": "z1"}),
                            cookies={webui.AUTH_COOKIE: token})
        self.assertEqual(response["status"], 403)
        self.assertIn("无权访问该分区", response["body"])
        self.assertNotIn("z1-restricted", response["body"])
        self.assertNotIn("UNIQ-AUTH-BRAVO-22222", response["body"])

    def test_an_allowed_zone_search_never_shows_another_zone(self):
        token = self.cookie("alice")
        response = self.get("/search?" + urlencode({"q": SHARED, "zone": "z0-public"}),
                            cookies={webui.AUTH_COOKIE: token})
        self.assertEqual(response["status"], 200)
        self.assertIn("z0-public/mount.md", response["body"])
        self.assertNotIn("z1-restricted", response["body"])
        paths = re.findall(r"/page\?path=([^&\"']+)", response["body"])
        self.assertTrue(paths)
        for path in paths:
            from urllib.parse import unquote
            self.assertTrue(unquote(path).startswith("z0-public/"), path)

    def test_the_default_selector_is_narrowed_for_a_restricted_reader(self):
        alice = self.cookie("alice")
        narrowed = self.get("/search?" + urlencode({"q": SHARED}),
                            cookies={webui.AUTH_COOKIE: alice})
        self.assertEqual(narrowed["status"], 200)
        self.assertIn("z0-public/mount.md", narrowed["body"])
        self.assertNotIn("z1-restricted", narrowed["body"])

        # bob 有 z0+z1：同一个查询必须能看到 z1，证明确实是「收窄」而不是「搜不到」
        bob = self.cookie("bob")
        wider = self.get("/search?" + urlencode({"q": SHARED}),
                         cookies={webui.AUTH_COOKIE: bob})
        self.assertEqual(wider["status"], 200)
        self.assertIn("z1-restricted/delivery.md", wider["body"])

    def test_the_zone_picker_only_offers_granted_zones(self):
        token = self.cookie("alice")
        body = self.get("/", cookies={webui.AUTH_COOKIE: token})["body"]
        self.assertIn("z0-public", body)
        self.assertNotIn("z1-restricted", body)
        self.assertNotIn("z2-sensitive", body)

        admin = self.cookie("carol")
        admin_body = self.get("/", cookies={webui.AUTH_COOKIE: admin})["body"]
        for zone in ("z0-public", "z1-restricted", "z2-sensitive"):
            self.assertIn(zone, admin_body)

    def test_page_cannot_bypass_the_search_gate(self):
        token = self.cookie("alice")
        denied = self.get(self.page_link("z1-restricted/delivery.md"),
                          cookies={webui.AUTH_COOKIE: token})
        self.assertEqual(denied["status"], 403)
        self.assertNotIn("交付偏好", denied["body"])
        self.assertNotIn("UNIQ-AUTH-BRAVO-22222", denied["body"])

        allowed = self.get(self.page_link("z0-public/mount.md"),
                           cookies={webui.AUTH_COOKIE: token})
        self.assertEqual(allowed["status"], 200)
        self.assertIn("UNIQ-AUTH-ALPHA-77777", allowed["body"])

    def test_an_admin_sees_every_zone(self):
        token = self.cookie("carol")
        response = self.get("/search?" + urlencode({"q": SHARED, "zone": "all"}),
                            cookies={webui.AUTH_COOKIE: token})
        self.assertEqual(response["status"], 200)
        self.assertIn("z0-public/mount.md", response["body"])
        self.assertIn("z1-restricted/delivery.md", response["body"])

        opened = self.get(self.page_link("z1-restricted/delivery.md"),
                          cookies={webui.AUTH_COOKIE: token})
        self.assertEqual(opened["status"], 200)
        self.assertIn("UNIQ-AUTH-BRAVO-22222", opened["body"])

    def test_the_audit_trail_names_the_actor(self):
        marker = "UNIQ-AUTH-ALPHA-77777"
        # 类级 home 共享同一个审计文件，前面的用例可能已经查过同一个词；
        # 这里只保证"这次请求多了一条"，而不是"全文件只有一条"。
        before = len([r for r in self.audit_records()
                      if r.get("action") == "search" and r.get("query") == marker])
        token = self.cookie("alice")
        self.assertEqual(
            self.get("/search?" + urlencode({"q": marker}),
                     cookies={webui.AUTH_COOKIE: token})["status"], 200)
        records = [r for r in self.audit_records()
                   if r.get("action") == "search" and r.get("query") == marker]
        self.assertTrue(records, "搜索必须留下审计记录")
        self.assertEqual(records[-1]["actor"], "alice")
        self.assertEqual(records[-1]["transport"], "web")
        self.assertGreaterEqual(records[-1]["hits"], 1)
        # 一次请求一条记录（不是 service 一条 + webui 一条）
        self.assertEqual(len(records), before + 1,
                         "one request must produce exactly one audit record")

    def test_a_denied_attempt_is_audited_with_no_hit_count(self):
        marker = "UNIQ-AUTH-ALPHA-77777"
        token = self.cookie("alice")
        self.assertEqual(
            self.get("/search?" + urlencode({"q": marker, "zone": "z1"}),
                     cookies={webui.AUTH_COOKIE: token})["status"], 403)
        denied = [r for r in self.audit_records()
                  if r.get("actor") == "alice" and r.get("query") == marker
                  and r.get("zone") == "z1"]
        self.assertTrue(denied, "被拒绝的请求也要留痕")
        self.assertIsNone(denied[-1]["hits"], "拒绝用 hits=null 区别于「0 命中」")


# --------------------------------------------------------------------------- #
# 7. CLI 接线
# --------------------------------------------------------------------------- #
class TestWiring(unittest.TestCase):
    def test_auth_flag_is_a_real_switch(self):
        tmp = Path(tempfile.mkdtemp(prefix="scheda-auth-wire-"))
        try:
            cfg = make_home(tmp)
            provision(cfg)
            on = webui.make_handler(cfg, auth_flag=True)
            off = webui.make_handler(cfg, auth_flag=False)
            self.assertTrue(on.auth_enabled)
            self.assertFalse(off.auth_enabled)
            self.assertTrue(webui.make_handler(cfg, auth_flag=True).auth_enabled)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_auth_flag_without_a_user_store_warns_loudly(self):
        import contextlib
        import io

        tmp = Path(tempfile.mkdtemp(prefix="scheda-auth-warn-"))
        try:
            cfg = Config(home=tmp / "home")
            stderr = io.StringIO()
            with contextlib.redirect_stderr(stderr):
                webui.serve(cfg, "127.0.0.1", 0, quiet=True, auth_flag=True)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
        self.assertIn("users.json", stderr.getvalue())
        self.assertIn("鉴权未生效", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
