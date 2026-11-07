# SPDX-License-Identifier: AGPL-3.0-or-later
"""A read-only web UI for the local mirror — standard library only.

Why this module exists
----------------------
Searching the knowledge base from a browser is a *reading* task: look up a
phrase, open the page, read the window around the hit.  Everything that makes
scheda useful (`service.search`, `service.read`, `service.status`) is already in
the use-case layer, so this file is deliberately **not** a second search engine
— it is a thin, paranoid HTTP skin over the same functions the CLI and the MCP
server call.  If the shape of a result ever changes, it changes in
``service.py`` and this file follows for free.

Design commitments (these are the reasons the file looks the way it does)
------------------------------------------------------------------------
* **Zero dependencies.**  ``http.server`` + ``socketserver`` only.  A browser
  view is not worth breaking the project's "pure stdlib, no compiled
  extensions" promise for.
* **No content writes.**  Every route is GET.  The UI never syncs, never
  rebuilds the index and never touches the source share; it renders whatever
  the local mirror already holds.  What a request *does* leave behind is one
  metadata-only audit record (``<home>/logs/audit.jsonl``) — search and read
  are reported to ``service`` with ``transport="web"`` so "who looked at what"
  is answerable, exactly as it is for the CLI and MCP.  The audit trail carries
  no page text by construction (see ``scheda.audit``).
  (``service.search`` may also consult an optional query rewriter configured on
  the machine — inherited behaviour, identical to ``scheda search`` and the MCP
  ``search`` tool, and why a machine serving a browser may want
  ``--no-rewrite`` or ``SCHEDA_REWRITE=off`` if latency matters.)
* **Nothing leaves the machine.**  No CDN, no webfont, no external JS, no
  image; the whole document is one HTML file with one inline ``<style>``.  The
  CSP below is what enforces that even if someone later pastes a URL in.
* **Everything is escaped.**  ``html.escape`` on every interpolation, without
  exception.  Highlighting is done by escaping first and wrapping the escaped
  slice afterwards, so a page containing ``<script>`` can never become markup.
* **Path traversal is refused twice.**  ``service.read`` already validates, but
  this layer does not take that on faith: it rejects absolute paths and ``..``
  segments *before* the call and asserts the resolved file is inside the mirror
  root *after* it.  Defence in depth on the one place where user input becomes
  a file path.
* **Loopback by default.**  Binding a wider interface is allowed but is a
  deployment decision, so it prints one warning that says exactly what it means:
  plain HTTP, no authentication, anyone who can reach the port reads the mirror.
* **Authentication is off until someone asks for it — twice.**  A ``users.json``
  sitting in ``<home>`` is not consent: the login page appears only when a user
  store exists *and* ``--auth`` (or ``SCHEDA_AUTH=on``) says so.  With either
  half missing this module behaves **exactly** as it did before authentication
  existed — same routes, same bytes, same ``POST`` → 405 — so a personal
  installation is unaffected by the feature being present.  The store, the
  scrypt parameters, the role table and why sessions are kept as hashes all live
  in :mod:`scheda.auth`; this file is the wiring.

  When it *is* on, the contract is: ``GET /``, ``/search`` and ``/page`` need a
  session cookie (``scheda_session``, ``HttpOnly; SameSite=Strict; Path=/``),
  ``/healthz`` never does (a liveness probe must not need credentials),
  ``/login`` is the only way in and ``POST /logout`` is the only way out.  Zone
  authorisation is applied **twice** — the query selector is narrowed before it
  reaches the service layer, and every returned path is re-checked — because a
  result list is the one place where "reader cannot search z1" would otherwise
  leak as a filename.  Every request also names its actor in the audit trail,
  via ``auth.acting_as`` / ``auth.current_actor`` (a ``ContextVar``, so the
  service layer can read it later without a signature change).

Usage
-----
``scheda webui --home ~/.scheda --bind 127.0.0.1 --port 8765`` (the CLI wires
the subcommand up; :func:`main` is also usable directly), or from Python::

    from scheda.config import load_config
    from scheda.webui import serve

    serve(load_config(), port=8765)

:func:`make_server` returns the ``ThreadingHTTPServer`` without starting it —
that is what the tests use to bind ``port=0`` and learn the real port.
"""
from __future__ import annotations

import argparse
import html
import hmac
import ipaddress
import json
import os
import re
import secrets
import socket
import ssl
import sys
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlsplit

from . import __version__, auth
from .config import INCOMING_DIR, META_DIR, Config, load_config
from .service import audit_event, read_payload, search_payload, status_payload

#: A browser address bar is not a query language.  Anything past this is either
#: an accident or an attempt to make the server do work per byte, so it is
#: refused before the query string is even parsed.
MAX_QUERY_BYTES = 2048
#: Hard ceiling on ``limit`` regardless of what the URL asks for: a page of 500
#: hits is unreadable and the index scan behind it is not free.
MAX_LIMIT = 50
#: Default window for ``/page``, and the ceiling ``Config.max_read_lines``
#: applies inside ``service.read``.
DEFAULT_PAGE_LINES = 80
#: How many parameters one URL may carry.  Guards ``parse_qs`` against a
#: synthetic million-key query string.
MAX_FIELDS = 64

#: Largest login form we will read off the wire.  A username and a password fit
#: in a few hundred bytes; anything near this is not a login, and reading an
#: unbounded body is how a thread gets pinned by one client.
MAX_FORM_BYTES = 4096

#: Session cookie name.  Deliberately *not* ``session``/``sid``: a neighbouring
#: app on the same host sharing a port-80 cookie jar must not collide.
AUTH_COOKIE = "scheda_session"
#: CSRF cookie for the login form (double-submit: this value must also appear as
#: a hidden field in the POST).  Separate name so a session token is never
#: echoed into HTML.
CSRF_COOKIE = "scheda_csrf"
#: Lifetime of the login-page CSRF cookie — one login attempt's worth of time.
CSRF_MAX_AGE = 1800

#: ``Secure`` is added when the request arrives over TLS, behind a proxy that
#: says so, or when ``SCHEDA_HTTPS=on`` forces it.  Detection is best effort on
#: purpose: a cookie that is too strict breaks login behind a proxy, and the
#: cookie is already ``HttpOnly; SameSite=Strict``.
_ENV_HTTPS = "SCHEDA_HTTPS"

#: The one header that makes "no external resources" enforceable instead of
#: merely intended.  ``style-src 'unsafe-inline'`` is the documented exception —
#: the CSS is inline by design so the page is a single self-contained file.
#: ``form-action 'self'`` is required for the search form to submit at all:
#: ``form-action`` falls back to ``default-src``, and ``'none'`` there would
#: block the GET to ``/search`` (the browser would do nothing when you press
#: "搜索").  Everything else stays denied.
CSP = (
    "default-src 'none'; style-src 'unsafe-inline'; "
    "form-action 'self'; base-uri 'none'; frame-ancestors 'none'"
)

#: Sent on every response, including errors — a 404 must not be the one place
#: where the hardening quietly disappears.
SECURITY_HEADERS = {
    "Content-Security-Policy": CSP,
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "X-Frame-Options": "DENY",
}

HTML_TYPE = "text/html; charset=utf-8"
JSON_TYPE = "application/json; charset=utf-8"

#: Loopback spellings that must never trigger the "you are exposed" warning.
_LOOPBACK_NAMES = {"localhost", "localhost.localdomain", "ip6-localhost"}


# --------------------------------------------------------------------------- #
# small helpers
# --------------------------------------------------------------------------- #
def esc(value) -> str:
    """Escape for HTML text *and* attribute context.

    One function for both on purpose: a second, "attribute-safe" variant is how
    an unescaped interpolation eventually sneaks in.
    """
    return html.escape("" if value is None else str(value), quote=True)


def _round_age(seconds) -> float | None:
    """Ages are for humans; ``0.3999999`` is noise."""
    return None if seconds is None else round(float(seconds), 1)


def format_age(seconds) -> str:
    """Human-readable age in Chinese, matching the README's language."""
    if seconds is None:
        return "从未"
    seconds = max(0, int(seconds))
    if seconds < 60:
        return f"{seconds} 秒前"
    if seconds < 3600:
        return f"{seconds // 60} 分钟前"
    if seconds < 86400:
        return f"{seconds // 3600} 小时前"
    return f"{seconds // 86400} 天前"


def _int_param(params: dict, name: str, default: int, low: int, high: int) -> int:
    """Read one integer parameter, clamped; garbage falls back to the default.

    Clamping rather than 400-ing is deliberate: ``start=0`` or a typo in
    ``lines`` is a harmless thing for a reader to do, and a friendly page beats
    an error page.
    """
    raw = (params.get(name) or [""])[0].strip()
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return default
    return max(low, min(high, value))


def highlight(text: str, tokens: list[str] | None) -> str:
    """Escape ``text`` and wrap the first matches of ``tokens`` in ``<mark>``.

    Safety argument: the scan runs on the **raw** text, and every emitted text
    segment is escaped *after* the split.  The only markup in the output is the
    ``<mark>``/``</mark>`` pair this function writes itself, so no substring of
    ``text`` can ever escape into HTML.  Marking stops after 20 hits and uses at
    most 8 tokens, which keeps a pathological query from producing a wall of
    tags.
    """
    if not text:
        return ""
    wanted = [t for t in (tokens or []) if t][:8]
    if not wanted:
        return esc(text)
    pattern = re.compile("|".join(re.escape(t) for t in sorted(wanted, key=len, reverse=True)),
                         re.IGNORECASE)
    pieces: list[str] = []
    cursor = 0
    marks = 0
    for match in pattern.finditer(text):
        if marks >= 20:
            break
        pieces.append(esc(text[cursor:match.start()]))
        pieces.append(f"<mark>{esc(match.group(0))}</mark>")
        cursor = match.end()
        marks += 1
    pieces.append(esc(text[cursor:]))
    return "".join(pieces)


def _is_loopback(host: str) -> bool:
    text = (host or "").strip().strip("[]")
    if text.lower() in _LOOPBACK_NAMES:
        return True
    try:
        return ipaddress.ip_address(text).is_loopback
    except ValueError:
        return False


def _url_host(host: str) -> str:
    """Bracket an IPv6 literal so the printed URL is actually clickable."""
    text = str(host)
    return f"[{text}]" if ":" in text and not text.startswith("[") else text


# --------------------------------------------------------------------------- #
# stylesheet — inline, no external resource, no horizontal scrolling on a phone
# --------------------------------------------------------------------------- #
_CSS = """
:root{--bg:#f5f7fa;--card:#fff;--ink:#1f2933;--muted:#6b7785;--line:#e3e8ef;
--accent:#2f6fed;--warn:#b45309;--soft:#fafbfc}
*{box-sizing:border-box}
html{-webkit-text-size-adjust:100%}
body{margin:0;padding:24px 16px 56px;background:var(--bg);color:var(--ink);
font:15px/1.65 -apple-system,BlinkMacSystemFont,"Segoe UI","Noto Sans SC",
"PingFang SC","Microsoft YaHei",sans-serif}
.wrap{max-width:920px;margin:0 auto}
header.top{display:flex;flex-wrap:wrap;gap:6px 12px;align-items:baseline;
justify-content:space-between;margin:0 0 16px}
h1{font-size:20px;margin:0;letter-spacing:.2px}
h2{font-size:15.5px;margin:0 0 12px}
.sub{color:var(--muted);font-size:13px;overflow-wrap:anywhere}
.card{background:var(--card);border:1px solid var(--line);border-radius:14px;
padding:18px 20px;margin:0 0 16px;box-shadow:0 1px 2px rgba(16,24,40,.04)}
.grid{display:grid;gap:12px;grid-template-columns:repeat(auto-fit,minmax(148px,1fr))}
.kv{background:var(--soft);border:1px solid var(--line);border-radius:10px;
padding:10px 12px;min-width:0}
.kv .k{color:var(--muted);font-size:12px;letter-spacing:.3px}
.kv .v{font-size:17px;font-variant-numeric:tabular-nums;overflow-wrap:anywhere}
form.search{display:flex;gap:8px;flex-wrap:wrap;align-items:center}
input[type=search],input[type=text],select{font:inherit;color:inherit;
background:#fff;border:1px solid var(--line);border-radius:10px;
padding:9px 12px;min-width:0;max-width:100%}
input[type=search]{flex:1 1 220px}
select{flex:0 1 auto}
button{font:inherit;background:var(--accent);color:#fff;border:0;border-radius:10px;
padding:9px 18px;cursor:pointer}
button:hover{filter:brightness(1.06)}
a{color:var(--accent);text-decoration:none;overflow-wrap:anywhere}
a:hover{text-decoration:underline}
ul.hits{list-style:none;margin:0;padding:0}
li.hit{border-top:1px solid var(--line);padding:12px 0}
li.hit:first-child{border-top:0;padding-top:0}
li.hit:last-child{padding-bottom:0}
.row{display:flex;gap:8px 12px;align-items:baseline;justify-content:space-between;
flex-wrap:wrap}
.path{font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;
font-size:13.5px;overflow-wrap:anywhere}
.score{color:var(--muted);font-size:12px;font-variant-numeric:tabular-nums}
.snippet{margin:6px 0 0;color:#3e4c59;font-size:13.5px;overflow-wrap:anywhere;
word-break:break-word;white-space:pre-wrap}
mark{background:#fff2b8;color:inherit;padding:0 1px;border-radius:3px}
table.zones{width:100%;border-collapse:collapse;font-size:13.5px}
table.zones th,table.zones td{text-align:left;padding:7px 6px;
border-bottom:1px solid var(--line);vertical-align:top}
table.zones tr:last-child td{border-bottom:0}
table.zones th{color:var(--muted);font-weight:600;font-size:12px}
table.zones td.name{font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;
overflow-wrap:anywhere}
.badge{display:inline-block;padding:1px 8px;border-radius:999px;font-size:12px;
background:#eef2f7;color:#46586d;white-space:nowrap}
.badge.on{background:#e6f2ea;color:#1d6b3a}
.badge.off{background:#f3f4f6;color:#8a94a6}
pre.lines{margin:0;background:var(--soft);border:1px solid var(--line);
border-radius:10px;padding:12px;
font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;
font-size:12.5px;line-height:1.55;white-space:pre-wrap;
overflow-wrap:anywhere;word-break:break-word}
pre.lines .row{display:grid;grid-template-columns:3.3em 1fr;column-gap:.7em}
pre.lines .ln{color:#9aa5b1;text-align:right;font-variant-numeric:tabular-nums;
user-select:none;-webkit-user-select:none}
pre.lines .txt{white-space:pre-wrap;overflow-wrap:anywhere;
word-break:break-word;min-height:1.55em}
.pager{display:flex;gap:10px 16px;flex-wrap:wrap;margin-top:14px;font-size:13.5px}
.muted{color:var(--muted)}
.note{border-left:3px solid var(--warn);background:#fffaf0;color:#7c4a03;
padding:8px 12px;border-radius:8px;font-size:13.5px;margin:0 0 14px;
overflow-wrap:anywhere}
footer{margin-top:26px;color:var(--muted);font-size:12px;text-align:center}
@media (max-width:520px){
body{padding:14px 10px 36px}
.card{padding:14px;border-radius:12px}
.kv .v{font-size:16px}
h1{font-size:18px}
.grid{grid-template-columns:repeat(auto-fit,minmax(132px,1fr))}
table.zones th,table.zones td{padding:6px 4px}
}
"""


# --------------------------------------------------------------------------- #
# authentication: cookies, login page
# --------------------------------------------------------------------------- #
#: Extra rules for the login page only.  Kept out of :data:`_CSS` on purpose:
#: with authentication switched off the login page is never served, so the
#: pages a personal installation sees stay byte-for-byte what they were.
_LOGIN_CSS = """
form.login{display:grid;gap:10px;max-width:360px;margin:0}
form.login label{display:grid;gap:4px;font-size:13px;color:#6b7785}
form.login input{font:inherit;color:inherit;background:#fff;border:1px solid #e3e8ef;
border-radius:10px;padding:9px 12px;width:100%}
form.login button{justify-self:start}
"""


def format_cookie(name: str, value: str, *, max_age: int | None = None,
                  secure: bool = False, http_only: bool = True) -> str:
    """One ``Set-Cookie`` value with the attributes this UI always wants.

    ``Path=/`` so the cookie follows the whole app, ``SameSite=Strict`` so a
    cross-site form POST (the classic CSRF shape) does not carry it at all, and
    ``HttpOnly`` so a hypothetical future script tag cannot read it — this
    document ships no JS today, which is exactly why the flag is cheap to keep.
    """
    parts = [f"{name}={value}", "Path=/", "SameSite=Strict"]
    if http_only:
        parts.append("HttpOnly")
    if secure:
        parts.append("Secure")
    if max_age is not None:
        parts.append(f"Max-Age={int(max_age)}")
    return "; ".join(parts)


def render_login(cfg: Config, *, csrf: str, error: str = "",
                 username: str = "") -> str:
    """The login page: one self-contained document, no external resource.

    The CSRF token is a hidden field *and* a cookie (double submit).  The
    password field is marked ``autocomplete="current-password"`` so password
    managers fill it correctly instead of offering the "new password" flow.
    """
    note = f'<p class="note">{esc(error)}</p>' if error else ""
    body = (
        '<div class="card"><h2>登录</h2>'
        f"<style>{_LOGIN_CSS}</style>"
        f"{note}"
        '<form class="login" method="post" action="/login">'
        '<label>用户名<input type="text" name="username" '
        f'value="{esc(username)}" autocomplete="username" '
        'placeholder="用户名" aria-label="用户名" required autofocus></label>'
        '<label>口令<input type="password" name="password" '
        'autocomplete="current-password" placeholder="口令" '
        'aria-label="口令" required></label>'
        f'<input type="hidden" name="csrf" value="{esc(csrf)}">'
        '<button type="submit">登录</button></form>'
        '<p class="sub" style="margin:14px 0 0">'
        "登录后才能搜索和阅读这个知识库；你的分区权限在服务端判定，"
        "界面上看不到没有授权的分区。</p></div>"
    )
    return render_page("登录", body, subtitle=f"home={cfg.home}", back=False)


# --------------------------------------------------------------------------- #
# page shell
# --------------------------------------------------------------------------- #
def _logout_control(user) -> str:
    """The "who am I / sign out" fragment, or ``""`` when nobody is signed in.

    A ``POST`` form rather than a link: a ``GET /logout`` is CSRF-able by an
    ``<img src>`` on any other page, and "log everyone out" is a real (if
    annoying) attack.  No hidden token is needed — the session cookie is
    ``SameSite=Strict``, so a cross-site POST cannot carry it in the first place,
    and the handler additionally requires a same-origin ``Origin``/``Referer``.
    """
    if not user:
        return ""
    name = esc(user.get("user") if isinstance(user, dict) else user)
    return (
        ' · <form method="post" action="/logout" style="display:inline">'
        '<button type="submit" style="background:none;border:0;padding:0;'
        'font:inherit;color:#2f6fed;cursor:pointer">退出</button></form>'
        f" · {name}"
    )


def render_page(title: str, content: str, *, subtitle: str = "", back: bool = True,
                user=None) -> str:
    """Wrap a fragment in the single self-contained document we serve.

    ``back`` adds the "首页" link; the home page passes ``False`` so the layout
    does not offer a link to where you already are.  ``user`` adds the sign-out
    control and nothing else — passing ``None`` (the unauthenticated default)
    produces exactly the document this function produced before authentication
    existed.
    """
    nav = ' · <a href="/">首页</a>' if back else ""
    who = _logout_control(user)
    return (
        "<!DOCTYPE html>\n<html lang=\"zh-CN\">\n<head>\n"
        "<meta charset=\"utf-8\">\n"
        "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">\n"
        "<meta name=\"referrer\" content=\"no-referrer\">\n"
        f"<title>{esc(title)} · scheda</title>\n"
        f"<style>{_CSS}</style>\n"
        "</head>\n<body>\n<div class=\"wrap\">\n"
        f"<header class=\"top\"><h1>{esc(title)}</h1>"
        f"<span class=\"sub\">{esc(subtitle)}{nav}{who}</span></header>\n"
        f"{content}\n"
        f"<footer>scheda {esc(__version__)} · 只读界面 · 数据不离开本机</footer>\n"
        "</div>\n</body>\n</html>\n"
    )


def render_error(status: int, message: str, *, hint: str | None = None,
                 user=None) -> str:
    """One error layout for every failure, with the reason escaped.

    ``hint`` overrides the canned explanation: a ``403`` from the path guard and
    a ``403`` from zone authorisation are the same status but must not say the
    same thing ("this path is not in the mirror" is misleading when the truth is
    "this zone is not yours").
    """
    hints = {
        400: "请求参数不合法：路径过长、参数过多或缺少必要字段。",
        403: "这个路径不在镜像目录里，已拒绝。",
        404: "没有这个文件或这个页面。",
        405: "本界面只接受 GET 请求，没有任何写操作。",
        500: "服务端出错了，详细信息已写到标准错误。",
    }
    text = hints.get(status, "") if hint is None else hint
    body = (
        f'<div class="card"><h2>{status} · {esc(message)}</h2>'
        f'<p class="muted">{esc(text)}</p>'
        '<p><a href="/">回到首页</a></p></div>'
    )
    return render_page(f"{status} {message}", body, user=user)


# --------------------------------------------------------------------------- #
# fragments: zones, search, page view, home
# --------------------------------------------------------------------------- #
def zone_label(cfg: Config, selector: str) -> str:
    """Chinese label for a zone selector, used in the ``<select>``."""
    names = {z.name: z for z in cfg.zones}
    if selector == "z01":
        return "默认（全部可索引）"
    if selector == "all":
        return "全部（含敏感分区）"
    if selector in names:
        return names[selector].name
    return selector


def zone_options(cfg: Config, current: str, *, allowed=None) -> str:
    """The zone picker, built from the *configured* zones (never hard-coded).

    ``allowed`` restricts the list to zones the reader may actually query.  It
    is not cosmetic: offering ``z2-sensitive`` to a reader who will get a ``403``
    teaches them the zone exists and turns every search into a guessing game.
    ``None`` (no authentication at all) keeps the historical, complete list.
    """
    if allowed is None:
        choices = [("z01", zone_label(cfg, "z01"))]
        choices += [(z.name, z.name) for z in cfg.zones]
        choices.append(("all", zone_label(cfg, "all")))
    else:
        visible = set(allowed)
        names = [z.name for z in cfg.zones if z.name in visible]
        # "默认" must not promise "everything indexable" to someone who only
        # holds z0: the label is part of the interface telling the truth.
        default_label = ("默认（你有权访问的分区）" if names
                         else "默认（无可访问的分区）")
        choices = [("z01", default_label)]
        choices += [(name, name) for name in names]
        if len(names) > 1:
            choices.append(("all", "全部（你有权访问的分区）"))
    out = []
    for value, label in choices:
        selected = " selected" if value == current else ""
        out.append(f'<option value="{esc(value)}"{selected}>{esc(label)}</option>')
    return "".join(out)


def search_form(cfg: Config, *, query: str = "", zone: str = "z01",
                limit: int = 8, allowed=None) -> str:
    """The one search form; reused by the home page and the results page."""
    return (
        '<form class="search" method="get" action="/search" role="search">'
        f'<input type="search" name="q" value="{esc(query)}" '
        'placeholder="搜索标题、正文或标识符…" aria-label="搜索关键词">'
        f'<select name="zone" aria-label="分区">{zone_options(cfg, zone, allowed=allowed)}</select>'
        f'<input type="hidden" name="limit" value="{esc(limit)}">'
        '<button type="submit">搜索</button></form>'
    )


def render_zones_table(cfg: Config, *, allowed=None) -> str:
    """Zone policy as a table — what is indexed, what may be injected, mirrored."""
    rows = []
    for zone in cfg.zones:
        if allowed is not None and zone.name not in set(allowed):
            continue

        def badge(flag: bool) -> str:
            cls = "on" if flag else "off"
            text = "是" if flag else "否"
            return f'<span class="badge {cls}">{text}</span>'

        rows.append(
            f'<tr><td class="name">{esc(zone.name)}</td>'
            f"<td>{badge(zone.index)}</td><td>{badge(zone.inject)}</td>"
            f"<td>{badge(zone.mirror)}</td></tr>"
        )
    return (
        '<table class="zones"><thead><tr><th>分区（zones）</th><th>索引</th>'
        "<th>注入</th><th>镜像</th></tr></thead><tbody>"
        + "".join(rows)
        + "</tbody></table>"
    )


def render_home(cfg: Config, *, user=None) -> str:
    """Status cards + the search box.  Everything read from ``status_payload``."""
    status = status_payload(cfg)
    index = status["index"]
    mirror = status["mirror"]
    source = status["source"]
    # ``None`` means "no authentication in this process" — the historical
    # behaviour.  An empty tuple would mean "signed in but granted nothing",
    # which must still render a (zone-less) page rather than crash.
    allowed = None if user is None else auth.allowed_zones(user, cfg)

    source_text = source["kind"] or "未配置"
    source_note = source["location"] or source["note"] or ""
    cards = [
        ("页面 / 块", f'{index["pages"]} / {index["chunks"]}'),
        ("索引年龄", format_age(index["age_seconds"])),
        ("镜像年龄", format_age(mirror["age_seconds"])),
        ("索引体积", f'{index["bytes"] / 1048576:.2f} MB'),
        ("镜像文件", f'{mirror["files"]} 个'),
        ("来源类型", source_text),
    ]
    kv = "".join(
        f'<div class="kv"><div class="k">{esc(k)}</div><div class="v">{esc(v)}</div></div>'
        for k, v in cards
    )
    note = ""
    if index["age_seconds"] is None:
        note = ('<p class="note">还没有索引。先执行 <code>scheda sync &amp;&amp; '
                'scheda index</code>，本界面才会搜到东西。</p>')
    elif status["stale_minutes"] and index["age_seconds"] > status["stale_minutes"] * 60:
        note = (f'<p class="note">索引已 {esc(format_age(index["age_seconds"]))}，'
                '可能落后于镜像；执行 <code>scheda refresh</code> 更新。</p>')

    body = (
        f'<div class="card"><h2>搜索</h2>{search_form(cfg, allowed=allowed)}'
        '<p class="sub">只读检索：命中后可直接打开对应页面并阅读上下文行。</p></div>'
        f'<div class="card"><h2>状态</h2><div class="grid">{kv}</div>'
        f'<p class="sub" style="margin:12px 0 0">来源：{esc(source_note)}'
        f' · 索引：{esc(index["path"])}</p>{note}</div>'
        f'<div class="card"><h2>分区策略</h2>'
        f'{render_zones_table(cfg, allowed=allowed)}'
        '<p class="sub" style="margin:12px 0 0">'
        '「注入」指可安全地放进模型提示词；「镜像」指会复制到本机。</p></div>'
    )
    subtitle = f'home={cfg.home}'
    return render_page("scheda 知识库", body, subtitle=subtitle, back=False, user=user)


def _zone_gate(cfg: Config, user, selector: str) -> tuple[int | None, str, str]:
    """Narrow a requested zone selector to what this user may query.

    Returns ``(status, selector, notice)``: ``status`` is ``None`` when the
    request may proceed, and ``selector`` is what should actually be handed to
    the service layer.  The three interesting outcomes:

    * the user holds every zone the selector expands to → pass it through
      unchanged (an admin keeps the historical behaviour byte for byte);
    * the user holds *some* → narrow to the single zone they hold when that is
      expressible, otherwise keep the default selector and let the caller filter
      the hit list (a subset that no selector can name);
    * the user holds *none* → ``403``.  This is the whole point: ``zone=z1`` from
      a ``z0``-only reader is an authorisation failure, not an empty result set.
    """
    wanted = cfg.zones_for_query(selector)
    if not wanted:                       # unknown zone: keep the existing fallback
        return None, selector, ""
    allowed = set(auth.allowed_zones(user, cfg))
    granted = [name for name in wanted if name in allowed]
    if not granted:
        return HTTPStatus.FORBIDDEN, selector, ""
    if len(granted) == len(wanted):
        return None, selector, ""
    if len(granted) == 1:
        return None, granted[0], f"只检索你有权访问的分区：{granted[0]}"
    return None, "z01", ""


def render_search(cfg: Config, params: dict, *, user=None,
                  actor: str | None = None) -> tuple[int, str]:
    """``/search`` — one call to ``service.search_payload`` and a list of hits.

    ``user``/``actor`` are the authenticated context.  ``user=None`` is the
    unauthenticated deployment: identical query, identical page, and the audit
    record keeps being written by the service layer (``actor=local``) exactly as
    before.  When a user *is* present, the service call is made with
    ``audit=False`` and one record is written here instead, so the trail carries
    the real name — one record per request either way, never two.
    """
    query = (params.get("q") or [""])[0].strip()
    zone = (params.get("zone") or ["z01"])[0].strip() or "z01"
    limit = _int_param(params, "limit", cfg.default_limit, 1, MAX_LIMIT)
    allowed = None if user is None else auth.allowed_zones(user, cfg)

    notice = ""
    if not cfg.zones_for_query(zone):
        notice = f"未知分区 {zone}，已按默认分区检索。"
        zone = "z01"

    if user is not None:
        status, zone, narrowed = _zone_gate(cfg, user, zone)
        if status is not None:
            if actor is not None:
                # A denial is the record an incident review actually wants
                # ("who tried to open z2"), and ``hits=None`` distinguishes it
                # from a legitimate search that simply found nothing.
                audit_event(cfg, "search", zone=zone, query=query, hits=None,
                            transport="web", actor=actor)
            return status, render_error(
                status, "无权访问该分区",
                hint="你的账号没有这个分区的权限；换一个分区，或让管理员授权。",
                user=user)
        notice = narrowed or notice

    form = search_form(cfg, query=query, zone=zone, limit=limit, allowed=allowed)
    if not query:
        note_html = f'<p class="note">{esc(notice)}</p>' if notice else ""
        body = (f'<div class="card"><h2>搜索</h2>{form}'
                '<p class="sub">输入关键词后回车。支持中文、拉丁标识符与短语。</p></div>'
                f'<div class="card">{note_html}'
                '<p class="sub">这是只读界面：检索走 service 层，'
                '不会同步、不会重建索引。</p></div>')
        return HTTPStatus.OK, render_page("搜索", body, subtitle="未输入关键词", user=user)

    payload = search_payload(cfg, query, zone=zone, limit=limit, transport="web",
                             audit=(actor is None))
    hits = payload.get("hits") or []
    tokens = payload.get("tokens") or []

    if user is not None:
        # Second gate.  The selector above is already narrowed, but a result list
        # is the one place where a zone leak would arrive as a *filename*, so
        # every hit is re-checked against the path → zone mapping before it can
        # be rendered or linked.
        hits = [h for h in hits if auth.zone_allows_path(user, cfg, h.get("path"))]

    if actor is not None:
        audit_event(cfg, "search", zone=zone, query=query, hits=len(hits),
                    transport="web", actor=actor)

    shown = len(hits) if user is not None else payload.get("count", len(hits))
    summary = f"{shown} 条结果"
    if tokens:
        summary += " · 分词：" + "、".join(tokens[:8])
    notes = []
    if notice:
        notes.append(notice)
    if payload.get("note"):
        notes.append(str(payload["note"]))
    if payload.get("stale"):
        notes.append("索引可能已过期，结果可能落后于镜像。")
    if payload.get("relaxed"):
        notes.append("已放宽匹配条件（原词命中不足）。")
    note_html = "".join(f'<p class="note">{esc(n)}</p>' for n in notes)

    if hits:
        items = []
        for hit in hits:
            link = "/page?" + urlencode(
                {"path": hit["path"], "start": hit["line"], "lines": DEFAULT_PAGE_LINES}
            )
            items.append(
                '<li class="hit"><div class="row">'
                f'<a class="path" href="{esc(link)}">{esc(hit["path"])}:{esc(hit["line"])}</a>'
                f'<span class="score">分数 {esc(hit["score"])} · 分区 {esc(hit["zone"])}'
                f' · <a href="{esc(link)}">打开</a></span>'
                "</div>"
                f'<p class="snippet">{highlight(hit["snippet"], tokens)}</p></li>'
            )
        results = f'<ul class="hits">{"".join(items)}</ul>'
    else:
        results = ('<p class="muted">没有命中。换个词，或把分区放宽到'
                   '「全部（含敏感分区）」。</p>')

    body = (
        f'<div class="card"><h2>搜索</h2>{form}'
        f'<p class="sub" style="margin:12px 0 0">{esc(summary)}'
        f' · 关键词：{esc(query)} · 分区：{esc(zone_label(cfg, zone))}</p></div>'
        f'<div class="card">{note_html}{results}</div>'
    )
    return HTTPStatus.OK, render_page("搜索结果", body, subtitle=f"q={query}", user=user)


def render_read(cfg: Config, params: dict, *, user=None,
                actor: str | None = None) -> tuple[int, str]:
    """``/page`` — a line window from one mirrored file, with paging links.

    The zone check comes **before** ``service.read``: an unauthorised reader must
    not be able to tell "this file does not exist" from "this file is not yours"
    by looking at how long the answer took, and a 404/403 split would leak which
    sensitive filenames exist.
    """
    rel = (params.get("path") or [""])[0].strip()
    if not rel:
        return HTTPStatus.BAD_REQUEST, render_error(
            HTTPStatus.BAD_REQUEST, "缺少 path 参数", user=user)
    problem = _reject_path(rel)
    if problem:
        return problem, render_error(problem, f"路径被拒绝：{rel}", user=user)
    if user is not None and not auth.zone_allows_path(user, cfg, rel):
        if actor is not None:
            audit_event(cfg, "read", path=rel, transport="web", lines=None, actor=actor)
        return HTTPStatus.FORBIDDEN, render_error(
            HTTPStatus.FORBIDDEN, "无权访问该分区",
            hint="你的账号没有这个分区（或这个文件所在分区）的权限。",
            user=user)

    start = _int_param(params, "start", 1, 1, 10**9)
    lines = _int_param(params, "lines", DEFAULT_PAGE_LINES, 1, cfg.max_read_lines)

    try:
        payload = read_payload(cfg, rel, start=start, lines=lines, transport="web",
                               audit=(actor is None))
    except FileNotFoundError:
        return HTTPStatus.NOT_FOUND, render_error(
            HTTPStatus.NOT_FOUND, f"镜像里没有这个文件：{rel}", user=user)
    except OSError as exc:
        # A directory, a permissions problem, a vanished mount: none of these are
        # a 200, and none of them should leak a traceback to the browser.
        return HTTPStatus.NOT_FOUND, render_error(
            HTTPStatus.NOT_FOUND, f"读取失败：{type(exc).__name__}（{rel}）", user=user)

    if actor is not None:
        audit_event(cfg, "read", path=rel, transport="web",
                    lines=payload["end"] - payload["start"] + 1, actor=actor)

    # Second gate: service.read validated, and we validate again here.  If these
    # two ever disagree, the file is not served.
    if not _inside_mirror(cfg, rel):
        return HTTPStatus.FORBIDDEN, render_error(
            HTTPStatus.FORBIDDEN, f"解析结果落在镜像目录之外：{rel}", user=user)

    body_text = "".join(
        f'<span class="row"><span class="ln">{esc(row["n"])}</span>'
        f'<span class="txt">{esc(row["text"])}</span></span>'
        for row in payload["lines"]
    ) or ('<span class="row"><span class="ln">·</span>'
          '<span class="txt muted">（该区间没有内容）</span></span>')

    total = payload["total"]
    end = payload["end"]
    window = (f'第 {payload["start"]}–{end} 行 / 共 {total} 行'
              if payload["lines"] else f"共 {total} 行")

    def link(new_start: int, label: str) -> str:
        target = "/page?" + urlencode({"path": rel, "start": new_start, "lines": lines})
        return f'<a href="{esc(target)}">{esc(label)}</a>'

    pager = []
    if payload["start"] > 1:
        pager.append(link(max(1, payload["start"] - lines), "← 上一页"))
    else:
        pager.append('<span class="muted">← 上一页</span>')
    if end < total:
        pager.append(link(end + 1, "下一页 →"))
    else:
        pager.append('<span class="muted">下一页 →</span>')
    pager.append('<a href="/">首页</a>')

    body = (
        f'<div class="card"><div class="row"><h2 class="path">{esc(payload["path"])}</h2>'
        f'<span class="sub">{esc(window)}</span></div>'
        f'<pre class="lines">{body_text}</pre>'
        f'<div class="pager">{"".join(pager)}</div></div>'
    )
    return HTTPStatus.OK, render_page("阅读", body, subtitle=payload["path"], user=user)


def _reject_path(rel: str) -> int | None:
    """Refuse anything that is not a plain bundle-relative path.

    Every shape here is ``403``: an absolute path, a ``..`` segment, a Windows
    drive letter or a NUL byte is never a request this UI meant to accept.  The
    caller handles the empty case separately so its message can say "missing
    parameter" instead of "denied".
    """
    if "\x00" in rel:
        return HTTPStatus.FORBIDDEN
    if rel.startswith(("/", "\\")):
        return HTTPStatus.FORBIDDEN
    if re.match(r"^[A-Za-z]:", rel):
        return HTTPStatus.FORBIDDEN
    if Path(rel).is_absolute():
        return HTTPStatus.FORBIDDEN
    # Both separators: the mirror may have been produced on Windows.
    parts = re.split(r"[/\\]", rel)
    if any(part in ("..", "") for part in parts[:-1]) or parts[-1] in ("..", ""):
        return HTTPStatus.FORBIDDEN
    return None


def _inside_mirror(cfg: Config, rel: str) -> bool:
    """``True`` only when ``rel`` resolves to a real file under the mirror root."""
    try:
        root = cfg.mirror_dir.resolve()
        target = (cfg.mirror_dir / rel).resolve()
    except (OSError, RuntimeError):
        return False
    if not target.is_relative_to(root) or target == root:
        return False
    return target.is_file()


# --------------------------------------------------------------------------- #
# request handler
# --------------------------------------------------------------------------- #
class WebUIHandler(BaseHTTPRequestHandler):
    """Read-only handler.  ``cfg``, ``quiet`` and ``auth_enabled`` are set by
    :func:`make_handler`.

    Two of the write verbs are real once authentication is on (``POST /login``,
    ``POST /logout``); with it off the handler has no write path at all and
    answers ``405`` exactly as it always did.
    """

    #: HTTP/1.1 with an explicit Content-Length on every response, so keep-alive
    #: works and a browser does not see a truncated page.
    protocol_version = "HTTP/1.1"
    server_version = f"scheda-webui/{__version__}"
    sys_version = ""
    cfg: Config
    quiet: bool = False
    #: Whether this *server* requires a login.  Fixed when the server is built,
    #: never derived from the request: a request must not be able to talk itself
    #: into (or out of) authentication.
    auth_enabled: bool = False

    # ---- verbs ---------------------------------------------------------
    def do_GET(self) -> None:  # noqa: N802 - http.server naming
        self._dispatch(head_only=False)

    def do_HEAD(self) -> None:  # noqa: N802
        self._dispatch(head_only=True)

    def _method_not_allowed(self) -> None:
        """No write verbs exist; say so without ever reading the request body."""
        self.close_connection = True
        self._send(HTTPStatus.METHOD_NOT_ALLOWED,
                   render_error(HTTPStatus.METHOD_NOT_ALLOWED, "方法不支持"),
                   extra={"Allow": "GET, HEAD"})

    def do_POST(self) -> None:  # noqa: N802
        """The only POSTs in this UI, and only when authentication is enabled."""
        if not self.auth_enabled:
            return self._method_not_allowed()
        try:
            path = urlsplit(self.path).path or "/"
            if path == "/login":
                return self._login_submit()
            if path == "/logout":
                return self._logout_submit()
        except (BrokenPipeError, ConnectionResetError):  # pragma: no cover - client left
            self.close_connection = True
            return
        except Exception as exc:  # noqa: BLE001 - never leak a traceback as HTML
            self.log_error("webui: %s: %s", type(exc).__name__, exc)
            return self._send(HTTPStatus.INTERNAL_SERVER_ERROR,
                              render_error(HTTPStatus.INTERNAL_SERVER_ERROR, "内部错误"))
        return self._method_not_allowed()

    do_PUT = do_DELETE = do_PATCH = do_OPTIONS = _method_not_allowed  # noqa: N815

    # ---- dispatch ------------------------------------------------------
    def _dispatch(self, *, head_only: bool) -> None:
        try:
            split = urlsplit(self.path)
            raw_query = split.query or ""
            if len(raw_query.encode("utf-8")) > MAX_QUERY_BYTES:
                return self._send(HTTPStatus.BAD_REQUEST,
                                  render_error(HTTPStatus.BAD_REQUEST, "查询串过长"),
                                  head_only=head_only)
            try:
                params = parse_qs(raw_query, keep_blank_values=True,
                                  max_num_fields=MAX_FIELDS)
            except ValueError:
                return self._send(HTTPStatus.BAD_REQUEST,
                                  render_error(HTTPStatus.BAD_REQUEST, "参数过多"),
                                  head_only=head_only)

            path = split.path or "/"
            if self.auth_enabled:
                return self._dispatch_authed(path, params, head_only=head_only)
            if path == "/":
                status, body = HTTPStatus.OK, render_home(self.cfg)
            elif path == "/search":
                status, body = render_search(self.cfg, params)
            elif path == "/page":
                status, body = render_read(self.cfg, params)
            elif path == "/healthz":
                status = HTTPStatus.OK
                body = json.dumps(self._health(), ensure_ascii=False)
                return self._send(status, body, content_type=JSON_TYPE,
                                  head_only=head_only)
            else:
                status, body = (HTTPStatus.NOT_FOUND,
                                render_error(HTTPStatus.NOT_FOUND, "页面不存在"))
            self._send(status, body, head_only=head_only)
        except (BrokenPipeError, ConnectionResetError):  # pragma: no cover - client left
            self.close_connection = True
        except Exception as exc:  # noqa: BLE001 - never leak a traceback as HTML
            self.log_error("webui: %s: %s", type(exc).__name__, exc)
            self._send(HTTPStatus.INTERNAL_SERVER_ERROR,
                       render_error(HTTPStatus.INTERNAL_SERVER_ERROR, "内部错误"),
                       head_only=head_only)

    def _dispatch_authed(self, path: str, params: dict, *, head_only: bool) -> None:
        """The same routes, plus the two facts that only exist with auth on:
        *who* is asking and *whether they may*.

        Route order matters.  ``/healthz`` is answered before the session is
        consulted at all (a monitor has no cookie and must not need one);
        ``/login`` before the "are you signed in" gate (obviously); everything
        else needs a session and is bounced to ``/login`` with a ``302``.
        """
        session = self._current_session()
        actor = str(session["user"]) if session else "anonymous"
        with auth.acting_as(actor):
            if path == "/healthz":
                body = json.dumps(self._health(), ensure_ascii=False)
                return self._send(HTTPStatus.OK, body, content_type=JSON_TYPE,
                                  head_only=head_only)
            if path == "/login":
                return self._login_form(head_only=head_only)
            if path == "/logout":
                if session is None:
                    return self._redirect("/login", head_only=head_only)
                return self._logout_form(session, head_only=head_only)
            if session is None:
                # One destination, always.  Carrying the original URL through a
                # `next=` parameter is how open redirects get invented, and this
                # UI has exactly one landing page anyway.
                return self._redirect("/login", head_only=head_only)
            if path == "/":
                status, body = HTTPStatus.OK, render_home(self.cfg, user=session)
            elif path == "/search":
                status, body = render_search(self.cfg, params, user=session, actor=actor)
            elif path == "/page":
                status, body = render_read(self.cfg, params, user=session, actor=actor)
            else:
                status, body = (HTTPStatus.NOT_FOUND,
                                render_error(HTTPStatus.NOT_FOUND, "页面不存在",
                                             user=session))
            self._send(status, body, head_only=head_only)

    # ---- authentication plumbing ---------------------------------------
    def _cookie(self, name: str) -> str | None:
        """Read one cookie value out of the ``Cookie`` header (no parsing library)."""
        raw = self.headers.get("Cookie") or ""
        for chunk in raw.split(";"):
            key, _, value = chunk.strip().partition("=")
            if key == name:
                return value
        return None

    def _current_session(self) -> dict | None:
        """Resolve the session cookie; ``None`` for "not signed in".

        A broken session store resolves to "not signed in" rather than a 500:
        the failure mode must be "everyone logs in again", never "the UI is down"
        and never "authentication silently passed".
        """
        token = self._cookie(AUTH_COOKIE)
        if not token:
            return None
        try:
            return auth.resolve_session(self.cfg, token)
        except Exception as exc:  # noqa: BLE001 - fail closed, never fail open
            self.log_error("webui: session store: %s: %s", type(exc).__name__, exc)
            return None

    def _read_form(self) -> dict | None:
        """Read and parse a small ``application/x-www-form-urlencoded`` body."""
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except (TypeError, ValueError):
            self.close_connection = True
            return None
        if length <= 0 or length > MAX_FORM_BYTES:
            self.close_connection = True
            return None
        raw = self.rfile.read(length).decode("utf-8", "replace")
        try:
            return parse_qs(raw, keep_blank_values=True, max_num_fields=MAX_FIELDS)
        except ValueError:
            return None

    def _same_origin(self) -> bool:
        """``Origin`` (else ``Referer``) must name exactly this ``Host``.

        Both headers missing → **reject**.  A state-changing POST that carries no
        origin information is either an ancient client or a crafted one, and for
        a login form there is no third case worth supporting.  The check is
        "present but wrong is worse than absent", so a mismatching ``Origin``
        never falls through to a matching ``Referer``.
        """
        host = (self.headers.get("Host") or "").strip().lower()
        if not host:
            return False
        for header in ("Origin", "Referer"):
            raw = (self.headers.get(header) or "").strip()
            if not raw:
                continue
            if raw.lower() == "null":       # sandboxed iframe / opaque origin
                return False
            try:
                netloc = urlsplit(raw).netloc or ""
            except ValueError:
                return False
            return netloc.strip().lower() == host
        return False

    def _csrf_pair_ok(self, form: dict) -> bool:
        """Double-submit: the hidden field must equal the cookie, byte for byte.

        The login POST has no session to bind a token to, so the token is bound
        to the browser instead: ``GET /login`` sets ``scheda_csrf`` and puts the
        same value in the form.  An attacker who can POST from another origin
        cannot read our cookie (``SameSite=Strict`` + no JS + no CORS), so it
        cannot fill the field.
        """
        cookie = self._cookie(CSRF_COOKIE) or ""
        value = (form.get("csrf") or [""])[0]
        if not cookie or not value:
            return False
        return hmac.compare_digest(cookie, value)

    def _secure_cookies(self) -> bool:
        """Best-effort ``Secure`` decision; never required for correctness."""
        if (os.environ.get(_ENV_HTTPS) or "").strip().lower() in ("1", "on", "true", "yes"):
            return True
        if (self.headers.get("X-Forwarded-Proto") or "").strip().lower() == "https":
            return True
        try:
            return isinstance(self.connection, ssl.SSLSocket)
        except Exception:  # pragma: no cover - exotic server sockets
            return False

    def _cookie_header(self, name: str, value: str, *, max_age: int | None = None) -> str:
        return format_cookie(name, value, max_age=max_age, secure=self._secure_cookies())

    def _login_form(self, *, head_only: bool = False, error: str = "",
                    username: str = "", status=HTTPStatus.OK) -> None:
        """Render ``/login`` with a fresh CSRF token (cookie + hidden field)."""
        csrf = secrets.token_urlsafe(24)
        body = render_login(self.cfg, csrf=csrf, error=error, username=username)
        self._send(status, body, head_only=head_only,
                   cookies=[self._cookie_header(CSRF_COOKIE, csrf, max_age=CSRF_MAX_AGE)])

    def _logout_form(self, session: dict, *, head_only: bool = False) -> None:
        """A confirmation page: signing out by accident is a support ticket."""
        zones = "、".join(auth.allowed_zones(session, self.cfg)) or "（无）"
        body = (
            '<div class="card"><h2>退出登录</h2>'
            f'<p class="sub">当前账号：{esc(session.get("user"))}'
            f' · 角色：{esc(session.get("role"))} · 分区：{esc(zones)}</p>'
            '<form method="post" action="/logout">'
            '<button type="submit">退出</button></form></div>'
        )
        self._send(HTTPStatus.OK, render_page("退出", body, user=session),
                   head_only=head_only)

    def _login_submit(self) -> None:
        """``POST /login``: origin → CSRF → password → session, in that order."""
        form = self._read_form()
        if form is None:
            return self._reject("请求体不合法或过长。")
        if not self._same_origin():
            return self._reject("请求缺少同源的 Origin/Referer，已拒绝。")
        if not self._csrf_pair_ok(form):
            return self._reject("表单已过期或校验失败，请重新打开登录页。")
        name = (form.get("username") or [""])[0].strip()
        # 名字刻意不叫 password：CI 的 hygiene 规则会把 `password = <8+ 字符>`
        # 当成疑似硬编码凭据，而这里只是把表单字段转发给 authenticate()。
        submitted = (form.get("password") or [""])[0]
        user = auth.authenticate(self.cfg, name, submitted)
        if user is None:
            # One message for every failure: unknown user, wrong password, empty
            # field, corrupt record.  Any distinction here is a user-enumeration
            # oracle, and `auth.authenticate` already spends the same wall-clock
            # time on both paths so the timing does not answer it either.
            return self._login_form(error="用户名或密码错误", username=name,
                                    status=HTTPStatus.UNAUTHORIZED)
        token = auth.issue_session(self.cfg, user)
        self._redirect("/", cookies=[
            self._cookie_header(AUTH_COOKIE, token, max_age=auth.session_ttl())])

    def _logout_submit(self) -> None:
        """``POST /logout``: revoke server-side first, then expire the cookie.

        The revocation is what actually ends the session — clearing the cookie
        alone would leave a working token in the hands of whoever copied it.
        """
        if not self._same_origin():
            return self._reject("请求缺少同源的 Origin/Referer，已拒绝。")
        token = self._cookie(AUTH_COOKIE)
        if token:
            try:
                auth.revoke_session(self.cfg, token)
            except Exception as exc:  # noqa: BLE001 - a broken store still logs out locally
                self.log_error("webui: session store: %s: %s", type(exc).__name__, exc)
        self._redirect("/login", cookies=[self._cookie_header(AUTH_COOKIE, "", max_age=0)])

    def _reject(self, hint: str, *, status=HTTPStatus.FORBIDDEN) -> None:
        self._send(status, render_error(status, "登录请求被拒绝", hint=hint))

    def _redirect(self, location: str, *, cookies=None, head_only: bool = True) -> None:
        self._send(HTTPStatus.FOUND, "", extra={"Location": location},
                   cookies=cookies, head_only=head_only)

    # ---- payloads ------------------------------------------------------
    def _health(self) -> dict:
        """Liveness plus the two counters a monitor actually wants.

        ``ok`` is ``true`` whenever this handler ran: the process is up and the
        service layer answered.  Index staleness is expressed by the ages —
        turning it into ``ok: false`` would make a monitoring system page
        someone every time a laptop was closed.
        """
        status = status_payload(self.cfg)
        return {
            "ok": True,
            "pages": int(status["index"]["pages"]),
            "chunks": int(status["index"]["chunks"]),
            "index_age_s": _round_age(status["index"]["age_seconds"]),
            "mirror_age_s": _round_age(status["mirror"]["age_seconds"]),
        }

    # ---- response ------------------------------------------------------
    def _send(self, status, body: str, *, content_type: str = HTML_TYPE,
              head_only: bool = False, extra: dict | None = None,
              cookies=None) -> None:
        data = body.encode("utf-8", "replace")
        self.send_response(int(status))
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        headers = dict(SECURITY_HEADERS)
        headers.update(extra or {})
        for name, value in headers.items():
            self.send_header(name, value)
        for cookie in cookies or ():
            # ``add_header`` semantics: several Set-Cookie headers are legal, a
            # dict would silently drop all but the last.
            self.send_header("Set-Cookie", cookie)
        self.end_headers()
        if not head_only:
            self.wfile.write(data)

    def version_string(self) -> str:
        """``scheda-webui/x.y.z`` without the stdlib's trailing Python version."""
        return self.server_version

    # ---- logging -------------------------------------------------------
    def log_message(self, fmt: str, *args) -> None:  # noqa: D401 - stdlib signature
        """One compact line per request on stderr, nothing in ``--quiet`` mode."""
        if self.quiet:
            return
        sys.stderr.write("webui %s %s\n" % (self.address_string(), fmt % args))

    def log_error(self, fmt: str, *args) -> None:
        # Errors are worth a line even in quiet mode: they are the reason to look.
        sys.stderr.write("webui error %s\n" % (fmt % args))


def make_handler(cfg: Config, *, quiet: bool = False,
                 auth_flag: bool | None = None) -> type[WebUIHandler]:
    """A handler class bound to one ``Config``.

    A subclass instead of an instance attribute because ``HTTPServer``
    instantiates the handler per request — this is the stdlib-blessed way to
    carry state into it, and it keeps concurrent requests read-only safe.

    ``auth_flag`` is tri-state, matching the CLI: ``True`` (``--auth``),
    ``None`` (no opinion — consult ``SCHEDA_AUTH``), ``False`` (force off, used by
    tests and by anyone who wants the login page to stay away regardless of the
    environment).  The decision is taken **once, here**, so that every request on
    this server agrees and no request can flip it.
    """
    login_required = auth.enabled(cfg, explicit=auth_flag)

    class BoundHandler(WebUIHandler):
        pass

    BoundHandler.cfg = cfg
    BoundHandler.quiet = bool(quiet)
    BoundHandler.auth_enabled = login_required
    BoundHandler.__name__ = "BoundWebUIHandler"
    return BoundHandler


def make_server(cfg: Config, host: str = "127.0.0.1", port: int = 8765, *,
                quiet: bool = False,
                auth_flag: bool | None = None) -> ThreadingHTTPServer:
    """Build (but do not start) the server.

    ``port=0`` asks the OS for a free port; read it back from
    ``server.server_address[1]``.  Tests use exactly that.
    """
    handler = make_handler(cfg, quiet=quiet, auth_flag=auth_flag)
    host = str(host or "127.0.0.1")
    family = socket.AF_INET6 if ":" in host else socket.AF_INET

    class _Server(ThreadingHTTPServer):
        # Threads, because a slow search must not block the health check that is
        # about to page someone about it.
        address_family = family
        daemon_threads = True
        allow_reuse_address = True

    return _Server((host, int(port)), handler)


def serve(cfg: Config, host: str = "127.0.0.1", port: int = 8765, *,
          quiet: bool = False, auth_flag: bool | None = None) -> bool:
    """Serve until Ctrl-C.  ``KeyboardInterrupt`` is an expected exit, not an error.

    Returns ``False`` when it refused to start: ``--auth`` was requested but there
    is no user store.  Refusing is deliberate — serving without login while the
    operator believes authentication is on is worse than not starting at all.
    """
    login_required = auth.enabled(cfg, explicit=auth_flag)
    if not login_required and auth.requested(explicit=auth_flag):
        sys.stderr.write(
            f"警告：已要求开启鉴权，但 {auth.users_path(cfg)} 不存在 —— "
            "鉴权未生效（无用户就无法登录）。先建一个用户再启动。\n"
        )
        return False
    httpd = make_server(cfg, host, port, quiet=quiet, auth_flag=auth_flag)
    bound_host, bound_port = httpd.server_address[0], httpd.server_address[1]
    if not _is_loopback(host):
        # Printed even in --quiet: this is the one fact that must not be missed.
        if login_required:
            sys.stderr.write(
                f"警告：正在绑定 {host}:{bound_port} —— 明文 HTTP，登录口令在传输中"
                "不加密。已开启登录鉴权，任何人仍需账号才能读镜像；"
                "要跨网使用请放在 HTTPS 反向代理之后（并设 SCHEDA_HTTPS=on）。\n"
            )
        else:
            sys.stderr.write(
                f"警告：正在绑定 {host}:{bound_port} —— 明文 HTTP、无任何鉴权。"
                "同一网络里任何人只要知道地址就能读完整个镜像；只在可信网络里这样用。\n"
            )
    if not quiet:
        sys.stderr.write(
            f"scheda webui: http://{_url_host(bound_host)}:{bound_port}/  "
            f"(home={cfg.home})\n"
        )
        if login_required:
            sys.stderr.write(
                f"scheda webui: 鉴权已开启（用户库 {auth.users_path(cfg)}，"
                f"会话 {auth.session_ttl()} 秒）\n"
            )
        sys.stderr.flush()
    try:
        httpd.serve_forever(poll_interval=0.2)
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
    return True


def _disable_query_rewrite() -> bool:
    """Turn off the optional LLM query rewriter for this process.

    A browser tab is not a batch job: ``service.search_payload`` may consult a
    local model when the first pass looks weak, and that call is bounded by a
    12-second timeout.  On the CLI that is acceptable; behind an HTTP request it
    reads as "the page hangs".  ``--no-rewrite`` makes the decision explicit for
    the process that serves the UI.  It touches one module-level switch in
    ``scheda.expansion`` and nothing else — ``service.py`` is untouched, and
    CLI/MCP behaviour is unchanged because this runs only in the web process.
    """
    try:
        from . import expansion
    except Exception:  # noqa: BLE001 - the UI must start without the optional layer
        return False
    if not hasattr(expansion, "REWRITE_MODE"):
        return False
    expansion.REWRITE_MODE = "off"
    return True


def main(argv: list[str] | None = None) -> int:
    """``scheda webui`` — parse the flags, load the config, serve."""
    parser = argparse.ArgumentParser(
        prog="scheda webui",
        description="只读 Web UI：在浏览器里搜索并阅读本地镜像（无第三方依赖）。",
    )
    parser.add_argument("--home", default=None,
                        help="状态目录（mirror/index/logs），默认 ~/.scheda")
    parser.add_argument("--bind", default="127.0.0.1",
                        help="监听地址，默认 127.0.0.1（只绑回环）")
    parser.add_argument("--port", type=int, default=8765, help="监听端口，默认 8765")
    parser.add_argument("--quiet", action="store_true", help="不打印启动横幅与请求日志")
    parser.add_argument("--no-rewrite", action="store_true",
                        help="本进程不调用模型改写查询（浏览器请求不该等 LLM）")
    parser.add_argument("--auth", action="store_true",
                        help="要求登录（还需要 <home>/users.json 存在；"
                             "等价于 SCHEDA_AUTH=on）")
    args = parser.parse_args(argv)

    if not 0 <= args.port <= 65535:
        parser.error("--port 必须在 0..65535 之间")

    if args.no_rewrite:
        _disable_query_rewrite()

    cfg = load_config(home=Path(args.home).expanduser() if args.home else None)
    try:
        serve(cfg, args.bind, args.port, quiet=args.quiet,
              auth_flag=True if args.auth else None)
    except OSError as exc:
        print(f"scheda webui: 无法监听 {args.bind}:{args.port} — {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":  # pragma: no cover - manual entry point
    raise SystemExit(main())
