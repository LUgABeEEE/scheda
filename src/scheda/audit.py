# SPDX-License-Identifier: AGPL-3.0-or-later
"""结构化事件日志 + 审计日志（JSON Lines）。

为什么单独一个模块
------------------
``service.file_logger`` 原来的形态是「一行裸文本追加到 ``kb.log``」：没有级别、
没有可过滤的字段、没有轮转，且 ``except OSError: pass`` 把「日志写不进去」这件
事彻底淹没。对企业内网来说那三点都是硬伤 —— 出了事要能回答「谁、什么时候、
查了什么」，排障时要能 ``jq 'select(.level=="error")'``，而磁盘满时运维必须
收到提示而不是什么都没发生。

文本日志因此**保留**（CLI 到处在用、格式已被依赖），结构化日志与它**并列**
写一份：``<home>/logs/kb.log``（人读）＋ ``<home>/logs/events.jsonl``（机读）＋
``<home>/logs/audit.jsonl``（审计）。三个文件各写各的，任何一个写失败都不
影响另外两个。

两个文件的分工是刻意的
----------------------
* ``events.jsonl`` —— **运行**发生了什么（同步、建索引、搜索耗时）。
* ``audit.jsonl`` —— **谁看了什么**。只记元数据：动作、zone、截断后的查询串、
  目标路径、命中条数、传输通道。**绝不记页面正文、绝不记凭证**：审计日志是
  要长期保留、可能被第三方查看的东西，一旦开始记正文，知识库的隐私分区
  （``z2-sensitive`` 之类）就在日志里被绕过了。

轮转
----
单文件默认 5 MiB、保留 3 个历史文件（``.1`` ``.2`` ``.3``）。轮转用
``os.replace``（同一文件系统上的原子改名）做 ``path → .1 → .2 …`` 的位移：
**不截断、不复制**，所以正在读旧文件的进程看到的还是完整内容。写入用
``O_APPEND`` + 单次 ``os.write``，一行在 POSIX 上不会与另一个进程的行交错；
轮转本身另加一把目录锁（见 :func:`_rotation_lock`）。并发下仍接受一个已知
不完美：**可能有一行落在轮转前的旧文件里**（哪个文件是「新的」在改名的一瞬
有歧义）—— 这是可以接受的，丢行不行，落错文件可以。

过渡用的环境变量
----------------
轮转阈值与保留份数**本该**是 ``Config`` 的字段，但本版不动 ``config.py``
（配置面一改，既有 ``config.json`` 的语义就得重新论证，而这不是本次的目标）。
所以本模块按「``Config`` 属性 → 环境变量 → 默认值」的顺序解析：

    SCHEDA_LOG_MAX_BYTES   单文件阈值（字节），默认 5242880；0 = 不轮转
    SCHEDA_LOG_BACKUPS     保留几个历史文件，默认 3；0 = 超阈值即丢弃旧文件

**这两个是过渡用的环境变量，下一版进 Config**（届时属性名 ``log_max_bytes`` /
``log_backups``，解析顺序不变，环境变量继续作为覆盖层保留）。已经写好的调用
方不必改。
"""
from __future__ import annotations

import errno
import json
import os
import sys
import threading
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

#: 允许的级别。固定四个 —— 再多就会出现「none / notice / critical」各自表述。
LEVELS: tuple[str, ...] = ("debug", "info", "warn", "error")

#: 审计动作的合法取值。这是「谁看了什么」的动词表，故意短。
ACTIONS: tuple[str, ...] = ("search", "read", "status", "skills", "push", "config-change")

#: 审计记录里的传输通道。
TRANSPORTS: tuple[str, ...] = ("cli", "mcp", "web")

#: 没传 ``actor`` 时的默认值：本机单一用户部署，机器身份就是 ``local``。
DEFAULT_ACTOR = "local"

#: 没传 ``transport`` 时的默认值（CLI 是最常见的入口）。
DEFAULT_TRANSPORT = "cli"

#: 审计里的查询串最长保留多少**字符**（不是字节）。截断而不是丢弃：前 200
#: 字符足够回答「他在找什么」，而整段查询可能是一整篇粘贴进来的文档。
MAX_QUERY_CHARS = 200

#: 单文件阈值：5 MiB。够放约 5 万条事件，翻一遍 ``jq`` 也在秒级。
DEFAULT_MAX_BYTES = 5 * 1024 * 1024

#: 历史文件份数。
DEFAULT_BACKUPS = 3

EVENTS_NAME = "events.jsonl"
AUDIT_NAME = "audit.jsonl"

#: ``extra`` 里允许的键。固定集合 = 下游的 ``jq`` 查询可以假定字段存在；
#: 不在表里的键被丢弃（并在 docstring 里说清楚，不静默改变语义）。
EXTRA_KEYS: tuple[str, ...] = (
    "duration_ms", "pages", "chunks", "fetched", "removed", "error",
)

#: 进程内串行化。跨进程靠 ``O_APPEND`` 的单次写原子性，见模块 docstring。
_LOCK = threading.Lock()


# --------------------------------------------------------------------------- #
# 路径
# --------------------------------------------------------------------------- #
def log_dir(cfg) -> Path:
    """日志目录 —— 与 ``cfg.log_path`` 同目录，不另立规则。"""
    return Path(cfg.log_path).parent


def events_path(cfg) -> Path:
    """结构化事件日志路径（``<home>/logs/events.jsonl``）。"""
    return log_dir(cfg) / EVENTS_NAME


def audit_path(cfg) -> Path:
    """审计日志路径（``<home>/logs/audit.jsonl``），与事件日志同目录、独立轮转。"""
    return log_dir(cfg) / AUDIT_NAME


# --------------------------------------------------------------------------- #
# 轮转参数
# --------------------------------------------------------------------------- #
def _as_int(raw):
    """把配置/环境里的值读成非负整数；读不出来返回 ``None``（让上层用默认值）。"""
    if isinstance(raw, bool):
        return int(raw)
    try:
        return int(str(raw).strip())
    except (TypeError, ValueError):
        return None


def _setting(cfg, names: tuple[str, ...], env: str, default: int) -> int:
    for name in names:
        value = _as_int(getattr(cfg, name, None))
        if value is not None and value >= 0:
            return value
    value = _as_int(os.environ.get(env))
    if value is not None and value >= 0:
        return value
    return default


def rotation_settings(cfg) -> tuple[int, int]:
    """返回 ``(max_bytes, backups)``。

    ``Config`` 上先找 ``log_max_bytes`` / ``log_backups``（下一版会有的字段，
    有就照它走）；没有就退回 ``SCHEDA_LOG_MAX_BYTES`` / ``SCHEDA_LOG_BACKUPS``；
    再没有就用 5 MiB / 3 份。``max_bytes == 0`` 关闭轮转，``backups == 0``
    表示超阈值就地丢弃旧文件（不保留历史）。

    环境变量是**过渡手段**，见模块 docstring。
    """
    return (
        _setting(cfg, ("log_max_bytes",), "SCHEDA_LOG_MAX_BYTES", DEFAULT_MAX_BYTES),
        _setting(cfg, ("log_backups",), "SCHEDA_LOG_BACKUPS", DEFAULT_BACKUPS),
    )


# --------------------------------------------------------------------------- #
# 底层写入
# --------------------------------------------------------------------------- #
def report_write_failure(exc: BaseException) -> None:
    """把一次写失败报到 ``stderr`` —— 这是「不再静默吞掉」的全部实现。

    格式固定为 ``scheda: log write failed: <ERRNO>``（``errno`` 符号名，拿不到
    符号名时退化为异常类型名）。不做去重、不计数：日志写不进去通常意味着磁盘
    满或权限坏，重复恰恰是想要的信号。
    """
    code = getattr(exc, "errno", None)
    label = errno.errorcode.get(code) if isinstance(code, int) else None
    if label is None:
        label = type(exc).__name__
    try:
        sys.stderr.write(f"scheda: log write failed: {label}\n")
        sys.stderr.flush()
    except Exception:  # noqa: BLE001 - 连 stderr 都写不了，那就真的没有去处
        pass


def _generation(path: Path, index: int) -> Path:
    """``audit.jsonl`` 的第 ``index`` 代：``audit.jsonl.1`` …"""
    return path.with_name(f"{path.name}.{index}")


@contextmanager
def _rotation_lock(folder: Path):
    """轮转期间的跨进程互斥。

    锁**目录 fd** 而不是新建一个 ``.lock`` 文件：``flock`` 在 Linux 上对目录 fd
    同样有效，而多出来的锁文件会污染「日志目录里只有日志」这个可预期性（清理
    脚本按 ``*.jsonl*`` 扫的时候尤其明显）。拿不到 ``fcntl``（Windows）或锁不上
    时**照常轮转**：退化到模块 docstring 里说的「接受可能落错文件」，
    而不是因为锁不可用就干脆不轮转。
    """
    try:
        import fcntl
    except ImportError:  # pragma: no cover - 只在 Windows 上走到
        yield
        return
    handle = None
    try:
        handle = os.open(folder, os.O_RDONLY)
        fcntl.flock(handle, fcntl.LOCK_EX)
    except OSError:  # pragma: no cover - 目录权限异常时才命中
        if handle is not None:
            os.close(handle)
            handle = None
        yield
        return
    try:
        yield
    finally:
        try:
            fcntl.flock(handle, fcntl.LOCK_UN)
        finally:
            os.close(handle)


def _rotate(path: Path, backups: int) -> None:
    """``path → .1 → .2 …`` 位移，超过 ``backups`` 的最老那份被覆盖。

    全部用 ``os.replace``：同文件系统上是原子改名，任何时刻都**没有半个文件**，
    也不复制内容（大日志轮转 O(1)）。缺某代（手删过、上次轮转被打断）时
    ``FileNotFoundError`` 直接跳过 —— 不因为缺一代就不轮转。
    """
    if backups <= 0:
        try:
            os.unlink(path)
        except FileNotFoundError:
            pass
        return
    for index in range(backups - 1, 0, -1):
        try:
            os.replace(_generation(path, index), _generation(path, index + 1))
        except FileNotFoundError:
            continue
    try:
        os.replace(path, _generation(path, 1))
    except FileNotFoundError:
        pass


def _rotate_if_needed(path: Path, max_bytes: int, backups: int) -> None:
    """写完一行后检查大小。**先写后转**：所以单文件最多超出最后一行。"""
    if max_bytes <= 0:
        return
    try:
        if path.stat().st_size < max_bytes:
            return
    except OSError:  # pragma: no cover - stat 失败说明文件已经不在了
        return
    with _rotation_lock(path.parent):
        try:
            # 另一个进程可能刚刚转过：重查一次，避免一次写入触发两次位移。
            if path.stat().st_size < max_bytes:
                return
        except OSError:  # pragma: no cover
            return
        _rotate(path, backups)


def _write_all(fd: int, data: bytes) -> None:
    view = memoryview(data)
    while view:
        written = os.write(fd, view)
        view = view[written:]


def _write_jsonl(path, record: dict, *, max_bytes: int | None = None,
                 backups: int | None = None) -> None:
    """追加一条记录（一行 JSON）。**任何异常都不上抛。**

    写失败只在 ``stderr`` 留一行 ``scheda: log write failed: <ERRNO>``：日志是
    观测手段，不能反过来成为新的失败源 —— 磁盘满时搜索仍然要能用。
    """
    threshold = DEFAULT_MAX_BYTES if max_bytes is None else int(max_bytes)
    keep = DEFAULT_BACKUPS if backups is None else int(backups)
    try:
        payload = (json.dumps(record, ensure_ascii=False) + "\n").encode("utf-8")
    except (TypeError, ValueError) as exc:   # 某个字段不可序列化（不该发生，兜住）
        report_write_failure(exc)
        return
    try:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        with _LOCK:
            fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
            try:
                _write_all(fd, payload)
            finally:
                os.close(fd)
            _rotate_if_needed(target, threshold, keep)
    except OSError as exc:
        report_write_failure(exc)
    except Exception as exc:  # noqa: BLE001 - best effort，绝不把异常带给调用方
        report_write_failure(exc)


# --------------------------------------------------------------------------- #
# 字段归一化
# --------------------------------------------------------------------------- #
def _one_line(text) -> str:
    """把任意文本压成一行：JSON 里换行会被转义，但人读的 ``msg`` 不该带换行。"""
    return str("" if text is None else text).replace("\r\n", " ").replace("\r", " ").replace("\n", " ")


def _now() -> str:
    """ISO8601、带本机时区、秒级 —— 与 ``service.file_logger`` 的文本前缀同源。"""
    return datetime.now().astimezone().isoformat(timespec="seconds")


def normalise_level(level) -> str:
    """归一化级别；不认识的值退到 ``info``（写日志这件事本身不能因为级别写错而丢）。"""
    text = str(level if level is not None else "").strip().lower()
    if text in ("warning", "warn"):
        return "warn"
    return text if text in LEVELS else "info"


def normalise_event(event) -> str:
    """短横线命名：``Index Build!`` → ``index-build``；空值 → ``log``。"""
    text = str(event if event is not None else "").strip().lower()
    out = []
    for char in text:
        out.append(char if (char.isalnum() or char == "-") else "-")
    slug = "".join(out).strip("-")
    while "--" in slug:
        slug = slug.replace("--", "-")
    return slug or "log"


def _jsonable(value):
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def _extra_fields(extra: dict) -> dict:
    out = {}
    for key, value in extra.items():
        if key not in EXTRA_KEYS or value is None:
            continue
        out[key] = _jsonable(value)
    return out


_ERROR_WORDS = ("failed", "failure", "error", "traceback", "refused", "cannot")
_WARN_WORDS = ("skipped", "skipping", "warn", "stale", "unavailable", "no usable",
               "missing", "not found", "retry", "ignored")

#: 从自由文本猜事件名用的词表。刻意**粗**：``emit(message)`` 只有一句话，
#: 没有结构化信息可依；要精确就由调用方显式传 ``event=``。顺序无关，靠出现
#: 位置取最先命中的那个关键词。
_EVENT_HINTS: tuple[tuple[str, str], ...] = (
    ("sync", "mirror-sync"),
    ("mirror", "mirror-sync"),
    ("pull", "mirror-sync"),
    ("prune", "mirror-prune"),
    ("index", "index-build"),
    ("build", "index-build"),
    ("chunk", "index-build"),
    ("search", "search"),
    ("read", "read"),
    ("push", "push"),
    ("config", "config-change"),
)


def guess_level(message) -> str:
    """从一句人读消息猜级别（``error`` → ``warn`` → ``info`` 的优先级）。"""
    text = _one_line(message).lower()
    if any(word in text for word in _ERROR_WORDS):
        return "error"
    if any(word in text for word in _WARN_WORDS):
        return "warn"
    return "info"


def guess_event(message) -> str:
    """从一句人读消息猜事件名；猜不出就是 ``log``（不编造语义）。"""
    head = _one_line(message).split(":", 1)[0].lower()
    best: tuple[int, str] | None = None
    for keyword, event in _EVENT_HINTS:
        position = head.find(keyword)
        if position >= 0 and (best is None or position < best[0]):
            best = (position, event)
    return best[1] if best else "log"


# --------------------------------------------------------------------------- #
# 两个公开入口
# --------------------------------------------------------------------------- #
def log_event(cfg, level: str, event: str, msg: str, **extra) -> None:
    """追加一条结构化事件到 ``events.jsonl``。**不抛异常。**

    记录形状固定：``ts`` / ``level`` / ``event`` / ``msg`` / 可选 ``extra``。
    ``extra`` 只接受 :data:`EXTRA_KEYS` 里的键，其余丢弃 —— 下游的字段集合是
    契约，多一个键不会让 ``jq`` 更好用，只会让「这版有没有这个字段」变成猜谜。
    """
    record = {
        "ts": _now(),
        "level": normalise_level(level),
        "event": normalise_event(event),
        "msg": _one_line(msg),
    }
    fields = _extra_fields(extra)
    if fields:
        record["extra"] = fields
    max_bytes, backups = rotation_settings(cfg)
    _write_jsonl(events_path(cfg), record, max_bytes=max_bytes, backups=backups)


def _normalise_action(action) -> str:
    text = str(action if action is not None else "").strip().lower()
    return text or "unknown"


def _relative_path(cfg, value) -> str:
    """审计里的 ``path``：尽量记**相对路径**（绝对路径会把 home 布局泄进日志）。

    绝对路径落在 ``mirror/`` 或 ``home`` 底下时就地相对化；落在外面时原样保留
    —— 记错不如记全，但也不假装它是相对的。
    """
    text = _one_line(value).strip().replace("\\", "/")
    if not text:
        return ""
    candidate = Path(text)
    if candidate.is_absolute():
        for base in (getattr(cfg, "mirror_dir", None), getattr(cfg, "home", None)):
            if base is None:
                continue
            try:
                return candidate.relative_to(Path(base)).as_posix()
            except ValueError:
                continue
        return text
    while text.startswith("./"):
        text = text[2:]
    return text


def _hits(value):
    text = _as_int(value)
    return text if text is not None and text >= 0 else None


def audit_event(cfg, action: str, **fields) -> None:
    """追加一条审计记录到 ``audit.jsonl``。**不抛异常。**

    记录形状固定：``ts`` / ``actor`` / ``action`` / ``zone`` / ``query`` /
    ``path`` / ``hits`` / ``lines`` / ``transport``。没提供的字段写空值（固定
    schema 比「有的行有这个键、有的行没有」好用得多）。

    ``lines`` 只有 ``read`` 会填：一条 read 记录若不知道读了多少行，事后无法
    区分「看了一眼标题」和「把整页拉走」。

    隐私红线（这段是承诺，不是注释）：

    * 只记**元数据**。正文、片段、frontmatter、凭证一律不进来 —— 调用方也不该
      把 ``snippet`` 塞进 ``fields``，那样会被忽略（不在固定字段里）。
    * ``query`` 截断到 :data:`MAX_QUERY_CHARS` 字符。命中 ``z2`` 之类的字样
      不做任何特殊处理：那是用户自己敲进去的字符串，审计不该替它改写成
      「红acted」，否则「他查过什么」就答不出来了。
    * 不认识的 ``action`` 照样落盘（审计完整性优先于 schema 洁癖），只是名字
      归一化成小写。
    """
    query = _one_line(fields.pop("query", "")).strip()
    if len(query) > MAX_QUERY_CHARS:
        query = query[:MAX_QUERY_CHARS]
    transport = str(fields.pop("transport", "") or DEFAULT_TRANSPORT).strip().lower()
    if transport not in TRANSPORTS:
        transport = DEFAULT_TRANSPORT
    record = {
        "ts": _now(),
        "actor": _one_line(fields.pop("actor", "") or DEFAULT_ACTOR).strip(),
        "action": _normalise_action(action),
        "zone": _one_line(fields.pop("zone", "")).strip(),
        "query": query,
        "path": _relative_path(cfg, fields.pop("path", "")),
        "hits": _hits(fields.pop("hits", None)),
        "lines": _hits(fields.pop("lines", None)),
        "transport": transport,
    }
    max_bytes, backups = rotation_settings(cfg)
    _write_jsonl(audit_path(cfg), record, max_bytes=max_bytes, backups=backups)


__all__ = [
    "ACTIONS",
    "AUDIT_NAME",
    "DEFAULT_ACTOR",
    "DEFAULT_BACKUPS",
    "DEFAULT_MAX_BYTES",
    "DEFAULT_TRANSPORT",
    "EVENTS_NAME",
    "EXTRA_KEYS",
    "LEVELS",
    "MAX_QUERY_CHARS",
    "TRANSPORTS",
    "audit_event",
    "audit_path",
    "events_path",
    "guess_event",
    "guess_level",
    "log_dir",
    "log_event",
    "normalise_event",
    "normalise_level",
    "report_write_failure",
    "rotation_settings",
]
