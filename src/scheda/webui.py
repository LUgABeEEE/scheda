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
import ipaddress
import json
import re
import socket
import sys
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlsplit

from . import __version__
from .config import Config, load_config
from .service import read_payload, search_payload, status_payload

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
# page shell
# --------------------------------------------------------------------------- #
def render_page(title: str, content: str, *, subtitle: str = "", back: bool = True) -> str:
    """Wrap a fragment in the single self-contained document we serve.

    ``back`` adds the "首页" link; the home page passes ``False`` so the layout
    does not offer a link to where you already are.
    """
    nav = ' · <a href="/">首页</a>' if back else ""
    return (
        "<!DOCTYPE html>\n<html lang=\"zh-CN\">\n<head>\n"
        "<meta charset=\"utf-8\">\n"
        "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">\n"
        "<meta name=\"referrer\" content=\"no-referrer\">\n"
        f"<title>{esc(title)} · scheda</title>\n"
        f"<style>{_CSS}</style>\n"
        "</head>\n<body>\n<div class=\"wrap\">\n"
        f"<header class=\"top\"><h1>{esc(title)}</h1>"
        f"<span class=\"sub\">{esc(subtitle)}{nav}</span></header>\n"
        f"{content}\n"
        f"<footer>scheda {esc(__version__)} · 只读界面 · 数据不离开本机</footer>\n"
        "</div>\n</body>\n</html>\n"
    )


def render_error(status: int, message: str) -> str:
    """One error layout for every failure, with the reason escaped."""
    hints = {
        400: "请求参数不合法：路径过长、参数过多或缺少必要字段。",
        403: "这个路径不在镜像目录里，已拒绝。",
        404: "没有这个文件或这个页面。",
        405: "本界面只接受 GET 请求，没有任何写操作。",
        500: "服务端出错了，详细信息已写到标准错误。",
    }
    body = (
        f'<div class="card"><h2>{status} · {esc(message)}</h2>'
        f'<p class="muted">{esc(hints.get(status, ""))}</p>'
        '<p><a href="/">回到首页</a></p></div>'
    )
    return render_page(f"{status} {message}", body)


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


def zone_options(cfg: Config, current: str) -> str:
    """The zone picker, built from the *configured* zones (never hard-coded)."""
    choices = [("z01", zone_label(cfg, "z01"))]
    choices += [(z.name, z.name) for z in cfg.zones]
    choices.append(("all", zone_label(cfg, "all")))
    out = []
    for value, label in choices:
        selected = " selected" if value == current else ""
        out.append(f'<option value="{esc(value)}"{selected}>{esc(label)}</option>')
    return "".join(out)


def search_form(cfg: Config, *, query: str = "", zone: str = "z01",
                limit: int = 8) -> str:
    """The one search form; reused by the home page and the results page."""
    return (
        '<form class="search" method="get" action="/search" role="search">'
        f'<input type="search" name="q" value="{esc(query)}" '
        'placeholder="搜索标题、正文或标识符…" aria-label="搜索关键词">'
        f'<select name="zone" aria-label="分区">{zone_options(cfg, zone)}</select>'
        f'<input type="hidden" name="limit" value="{esc(limit)}">'
        '<button type="submit">搜索</button></form>'
    )


def render_zones_table(cfg: Config) -> str:
    """Zone policy as a table — what is indexed, what may be injected, mirrored."""
    rows = []
    for zone in cfg.zones:
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


def render_home(cfg: Config) -> str:
    """Status cards + the search box.  Everything read from ``status_payload``."""
    status = status_payload(cfg)
    index = status["index"]
    mirror = status["mirror"]
    source = status["source"]

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
        f'<div class="card"><h2>搜索</h2>{search_form(cfg)}'
        '<p class="sub">只读检索：命中后可直接打开对应页面并阅读上下文行。</p></div>'
        f'<div class="card"><h2>状态</h2><div class="grid">{kv}</div>'
        f'<p class="sub" style="margin:12px 0 0">来源：{esc(source_note)}'
        f' · 索引：{esc(index["path"])}</p>{note}</div>'
        f'<div class="card"><h2>分区策略</h2>{render_zones_table(cfg)}'
        '<p class="sub" style="margin:12px 0 0">'
        '「注入」指可安全地放进模型提示词；「镜像」指会复制到本机。</p></div>'
    )
    subtitle = f'home={cfg.home}'
    return render_page("scheda 知识库", body, subtitle=subtitle, back=False)


def render_search(cfg: Config, params: dict) -> tuple[int, str]:
    """``/search`` — one call to ``service.search_payload`` and a list of hits."""
    query = (params.get("q") or [""])[0].strip()
    zone = (params.get("zone") or ["z01"])[0].strip() or "z01"
    limit = _int_param(params, "limit", cfg.default_limit, 1, MAX_LIMIT)

    notice = ""
    if not cfg.zones_for_query(zone):
        notice = f"未知分区 {zone}，已按默认分区检索。"
        zone = "z01"

    form = search_form(cfg, query=query, zone=zone, limit=limit)
    if not query:
        note_html = f'<p class="note">{esc(notice)}</p>' if notice else ""
        body = (f'<div class="card"><h2>搜索</h2>{form}'
                '<p class="sub">输入关键词后回车。支持中文、拉丁标识符与短语。</p></div>'
                f'<div class="card">{note_html}'
                '<p class="sub">这是只读界面：检索走 service 层，'
                '不会同步、不会重建索引。</p></div>')
        return HTTPStatus.OK, render_page("搜索", body, subtitle="未输入关键词")

    payload = search_payload(cfg, query, zone=zone, limit=limit, transport="web")
    hits = payload.get("hits") or []
    tokens = payload.get("tokens") or []

    summary = f'{payload.get("count", len(hits))} 条结果'
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
    return HTTPStatus.OK, render_page("搜索结果", body, subtitle=f"q={query}")


def render_read(cfg: Config, params: dict) -> tuple[int, str]:
    """``/page`` — a line window from one mirrored file, with paging links."""
    rel = (params.get("path") or [""])[0].strip()
    if not rel:
        return HTTPStatus.BAD_REQUEST, render_error(
            HTTPStatus.BAD_REQUEST, "缺少 path 参数")
    problem = _reject_path(rel)
    if problem:
        return problem, render_error(problem, f"路径被拒绝：{rel}")

    start = _int_param(params, "start", 1, 1, 10**9)
    lines = _int_param(params, "lines", DEFAULT_PAGE_LINES, 1, cfg.max_read_lines)

    try:
        payload = read_payload(cfg, rel, start=start, lines=lines, transport="web")
    except FileNotFoundError:
        return HTTPStatus.NOT_FOUND, render_error(
            HTTPStatus.NOT_FOUND, f"镜像里没有这个文件：{rel}")
    except OSError as exc:
        # A directory, a permissions problem, a vanished mount: none of these are
        # a 200, and none of them should leak a traceback to the browser.
        return HTTPStatus.NOT_FOUND, render_error(
            HTTPStatus.NOT_FOUND, f"读取失败：{type(exc).__name__}（{rel}）")

    # Second gate: service.read validated, and we validate again here.  If these
    # two ever disagree, the file is not served.
    if not _inside_mirror(cfg, rel):
        return HTTPStatus.FORBIDDEN, render_error(
            HTTPStatus.FORBIDDEN, f"解析结果落在镜像目录之外：{rel}")

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
    return HTTPStatus.OK, render_page("阅读", body, subtitle=payload["path"])


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
    """GET-only handler.  ``cfg`` and ``quiet`` are set by :func:`make_handler`."""

    #: HTTP/1.1 with an explicit Content-Length on every response, so keep-alive
    #: works and a browser does not see a truncated page.
    protocol_version = "HTTP/1.1"
    server_version = f"scheda-webui/{__version__}"
    sys_version = ""
    cfg: Config
    quiet: bool = False

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

    do_POST = do_PUT = do_DELETE = do_PATCH = do_OPTIONS = _method_not_allowed  # noqa: N815

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
              head_only: bool = False, extra: dict | None = None) -> None:
        data = body.encode("utf-8", "replace")
        self.send_response(int(status))
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        headers = dict(SECURITY_HEADERS)
        headers.update(extra or {})
        for name, value in headers.items():
            self.send_header(name, value)
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


def make_handler(cfg: Config, *, quiet: bool = False) -> type[WebUIHandler]:
    """A handler class bound to one ``Config``.

    A subclass instead of an instance attribute because ``HTTPServer``
    instantiates the handler per request — this is the stdlib-blessed way to
    carry state into it, and it keeps concurrent requests read-only safe.
    """

    class BoundHandler(WebUIHandler):
        pass

    BoundHandler.cfg = cfg
    BoundHandler.quiet = bool(quiet)
    BoundHandler.__name__ = "BoundWebUIHandler"
    return BoundHandler


def make_server(cfg: Config, host: str = "127.0.0.1", port: int = 8765, *,
                quiet: bool = False) -> ThreadingHTTPServer:
    """Build (but do not start) the server.

    ``port=0`` asks the OS for a free port; read it back from
    ``server.server_address[1]``.  Tests use exactly that.
    """
    handler = make_handler(cfg, quiet=quiet)
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
          quiet: bool = False) -> None:
    """Serve until Ctrl-C.  ``KeyboardInterrupt`` is an expected exit, not an error."""
    httpd = make_server(cfg, host, port, quiet=quiet)
    bound_host, bound_port = httpd.server_address[0], httpd.server_address[1]
    if not _is_loopback(host):
        # Printed even in --quiet: this is the one fact that must not be missed.
        sys.stderr.write(
            f"警告：正在绑定 {host}:{bound_port} —— 明文 HTTP、无任何鉴权。"
            "同一网络里任何人只要知道地址就能读完整个镜像；只在可信网络里这样用。\n"
        )
    if not quiet:
        sys.stderr.write(
            f"scheda webui: http://{_url_host(bound_host)}:{bound_port}/  "
            f"(home={cfg.home})\n"
        )
        sys.stderr.flush()
    try:
        httpd.serve_forever(poll_interval=0.2)
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()


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
    """``scheda webui`` — parse the three flags, load the config, serve."""
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
    args = parser.parse_args(argv)

    if not 0 <= args.port <= 65535:
        parser.error("--port 必须在 0..65535 之间")

    if args.no_rewrite:
        _disable_query_rewrite()

    cfg = load_config(home=Path(args.home).expanduser() if args.home else None)
    try:
        serve(cfg, args.bind, args.port, quiet=args.quiet)
    except OSError as exc:
        print(f"scheda webui: 无法监听 {args.bind}:{args.port} — {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":  # pragma: no cover - manual entry point
    raise SystemExit(main())
