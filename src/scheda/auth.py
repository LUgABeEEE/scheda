# SPDX-License-Identifier: AGPL-3.0-or-later
"""多用户、密码与会话 —— 纯标准库，**默认完全关闭**。

为什么这个模块必须默认关闭
--------------------------
scheda 的默认部署是「一个人的知识库，Web UI 只听 ``127.0.0.1``」。给这种部署
强加一个登录页是净损失：多一个忘记密码的入口、多一个锁死自己的机会、多一份
要备份的状态。所以本模块的每一条规则都围绕同一句话：

    没有 ``users.json`` 就没有鉴权，行为与加这个功能之前**逐字节一致**。

:func:`enabled` 因此是唯一的开关判据，而且它要求两件事同时成立：

1. ``<home>/users.json`` **存在**（否则谁也没法登录 —— 打开鉴权就等于锁死自己，
   这是唯一不能靠「小心一点」避免的事故，所以文件不存在时一律视为关闭）；
2. 有人**显式**要求（``--auth`` 或 ``SCHEDA_AUTH=on``）。文件存在只是历史遗留
   （备份恢复、试过又忘了删）不该悄悄改变一个生产实例的行为。

两条都不满足时 ``enabled() is False``，而 falsy 时下面所有函数的语义都是
「放行」：:func:`allowed_zones` 返回该配置下的全部分区，:func:`resolve_session`
返回 ``None`` 但 webui 在关闭状态下根本不会调用它。

设计取舍（写在这里，是因为读代码的人一定会问）
--------------------------------------------
* **scrypt 而不是 bcrypt/argon2。** 项目承诺「纯标准库、无编译扩展」，
  ``hashlib.scrypt`` 是标准库里唯一的内存硬口令哈希（``n=2**14, r=8, p=1``
  ≈ 16 MiB/次、单核约 30–60 ms），比 PBKDF2 抗 GPU/ASIC 好得多，且不需要装
  任何东西。参数写进模块常量、**不**写进用户库：库格式比参数更需要稳定，
  将来整库换参数用「旧哈希可验证 + 登录成功时重写」即可平滑迁移。
* **只存 token 的 sha256，不存明文。** ``sessions.json`` 会被备份、会被复制到
  另一台机器、可能被误提交。token 是 bearer 凭证 —— 拿到明文就等于登录成功，
  而 sha256 拿到后什么也做不了（32 字节随机数没有字典可查）。同理，密码只以
  scrypt 派生值落盘，盐每用户独立。
* **所有读写都 flock + tmp + ``os.replace``。** Web UI 是线程模型、真相却在
  磁盘上：两个并发登录同时「读-改-写」会让其中一个凭空消失。锁的是 **home
  目录 fd**（与 :mod:`scheda.audit` 轮转时同一手法），因此不产生额外锁文件，
  也不存在「文件被 replace 后锁还锁着旧 inode」的经典问题。
* **权限从创建那一刻就是 0600。** 先 0644 再 chmod 的那几毫秒里，同机任何进程
  都能把明文盐和派生值读走；用 ``mkstemp`` + ``os.fchmod`` + ``os.replace``，
  世界上从来没有出现过一份可读的中间态。读到历史遗留的宽权限文件时顺手收紧。
* **actor 通过 ContextVar 传递。** ``service.py`` 的签名不许改（另一个分支正在
  拆它），而审计必须回答「谁查了什么」。:func:`current_actor` /
  :func:`acting_as` 是那条不侵入的通道：webui 在每次请求里
  ``with acting_as("alice"):``，本模块的 :func:`audit` 自动带上 actor；
  ``service`` 侧哪天愿意读就读，不愿意读也不影响今天的行为。

角色 → 能力（以及将来的 MCP 复用）
---------------------------------
角色是**固定的三档**，能力是一张扁平表 :data:`CAPABILITIES`。刻意不做权限位
组合：三档能覆盖「只读 / 提案 / 管理」的全部现实需求，而可组合的权限位会在
第一次「给某人临时加一个能力」时开始腐烂。

    reader  读（search / read / status），受 zones 限制
    writer  以上 + push（向 ``_incoming`` 提交提案）
    admin   以上 + admin（管理用户），且**不受 zones 限制**

``zones`` 是用户级的**额外**限制，只对非 admin 生效：admin 的定义就是「全部
zone」，否则「管理员看不见 z2」会让运维无法排查自己刚配错的分区。

文件格式
--------
``<home>/users.json``::

    {"users": [{"name": "alice", "role": "reader",
                "zones": ["z0-public"], "salt": "<hex>", "hash": "<hex>",
                "created": "2026-11-07T18:00:00+08:00"}]}

``<home>/sessions.json``::

    {"sessions": [{"token_hash": "<sha256 hex>", "user": "alice",
                   "created": "2026-11-07T18:01:00+08:00",
                   "expires": 1794000000.0}]}

``expires`` 是 epoch 秒（浮点）而不是 ISO 串：会话判定是**比较**而不是展示，
少一层解析就少一类「时区没带、字符串比大小」的错。``created`` 保留 ISO 是因为
人要读它。
"""
from __future__ import annotations

import contextlib
import contextvars
import hashlib
import hmac
import json
import os
import re
import secrets
import tempfile
import time
from datetime import datetime
from pathlib import Path

from .audit import DEFAULT_ACTOR, audit_event
from .config import INCOMING_DIR, META_DIR

# --------------------------------------------------------------------------- #
# 常量
# --------------------------------------------------------------------------- #
#: 用户库文件名（在 ``<home>`` 下）。
USERS_NAME = "users.json"
#: 会话库文件名（在 ``<home>`` 下）。
SESSIONS_NAME = "sessions.json"

#: 显式开启鉴权的环境变量。``--auth`` 与它等价，任一成立即可。
ENV_FLAG = "SCHEDA_AUTH"
#: 会话有效期（秒）的环境变量覆盖；默认 12 小时。
ENV_SESSION_TTL = "SCHEDA_SESSION_TTL"

#: 私密状态文件的权限位。**创建时即 0600**，不做「先宽后紧」。
PRIVATE_MODE = 0o600

#: scrypt 参数。``n=2**14, r=8, p=1`` ≈ 16 MiB 内存、单核 30–60 ms，
#: 是 OWASP 对交互式登录的建议档位，也是本项目「零第三方依赖」下能拿到的最好选择。
SCRYPT_N = 2 ** 14
SCRYPT_R = 8
SCRYPT_P = 1
SCRYPT_DKLEN = 32
#: 盐长度（字节）。``os.urandom(16)``，每用户独立。
SALT_BYTES = 16
#: scrypt 的内存上限参数。OpenSSL 默认 32 MiB 够用；显式写出来是为了让
#: 「为什么 n 不能随手上调」这件事在代码里可见（n 翻倍 = 内存翻倍）。
SCRYPT_MAXMEM = 64 * 1024 * 1024

#: 会话默认有效期：12 小时。一个工作日，够用，且短到「笔记本丢了」的窗口可接受。
DEFAULT_SESSION_TTL = 12 * 3600

#: 会话 token 的字节数（``secrets.token_urlsafe(32)`` → 43 个字符）。
TOKEN_BYTES = 32

#: 用户名允许的字符集。刻意窄：用户名会进审计日志、进 URL、进 ``users.json``，
#: 让它只能是 ``[A-Za-z0-9._-]`` 就一次性消灭了日志注入与路径拼接两类问题。
NAME_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")

#: 角色名（固定三档，见模块 docstring）。
ROLES: tuple[str, ...] = ("reader", "writer", "admin")
#: 默认角色：最小权限。
DEFAULT_ROLE = "reader"

#: 角色 → 能力表。**这张表是 webui 与将来 MCP 共用的唯一真相**：
#: 任意入口想做「这个人能不能 push」，问 :func:`has_capability`，不要再写一份
#: 角色分支 —— 两份判断迟早会不一致，而不一致的那天就是越权的开始。
CAPABILITIES: dict[str, frozenset[str]] = {
    "reader": frozenset({"search", "read", "status"}),
    "writer": frozenset({"search", "read", "status", "push"}),
    "admin": frozenset({"search", "read", "status", "push", "admin"}),
}

#: 分区选择器里代表「全部」与「默认可索引」的两个保留值（与 ``config`` 一致）。
ALL_SELECTOR = "all"
DEFAULT_SELECTOR = "z01"

#: 给「用户不存在」的登录尝试准备的一次性假哈希材料，见 :func:`authenticate`。
_DUMMY: tuple[bytes, bytes] | None = None

#: 审计 actor 的传递通道。默认 ``local`` —— 与 ``audit.DEFAULT_ACTOR`` 同义，
#: 所以「没开鉴权」时写出来的记录与改造前完全一致。
_ACTOR: contextvars.ContextVar[str] = contextvars.ContextVar(
    "scheda_actor", default=DEFAULT_ACTOR
)


# --------------------------------------------------------------------------- #
# 路径
# --------------------------------------------------------------------------- #
def users_path(cfg) -> Path:
    """``<home>/users.json`` —— 用户库位置，与 home 布局同源。"""
    return Path(cfg.home) / USERS_NAME


def sessions_path(cfg) -> Path:
    """``<home>/sessions.json`` —— 会话库位置。"""
    return Path(cfg.home) / SESSIONS_NAME


# --------------------------------------------------------------------------- #
# actor（ContextVar 通道）
# --------------------------------------------------------------------------- #
def current_actor() -> str:
    """当前请求的 actor 名；没有上下文时是 ``local``。

    ``service.search_payload`` 之类将来若愿意把 actor 传进审计，读这个函数即可；
    今天它由 :func:`audit` 与 webui 使用。
    """
    return _ACTOR.get()


def set_actor(name) -> contextvars.Token:
    """把 actor 设为 ``name``（空值退回 ``local``），返回可 reset 的 token。"""
    text = str(name or "").strip()
    return _ACTOR.set(text or DEFAULT_ACTOR)


@contextlib.contextmanager
def acting_as(name):
    """``with acting_as("alice"):`` —— 作用域内 :func:`current_actor` 是 alice。

    用 ContextVar 而不是线程局部变量：ContextVar 在线程池里不会串味（每个
    请求线程有独立的 context），也不会在协程切换时被另一个请求踩掉。
    """
    token = set_actor(name)
    try:
        yield
    finally:
        _ACTOR.reset(token)


def audit(cfg, action: str, **fields) -> None:
    """写一条审计记录，``actor`` 自动取 :func:`current_actor`。

    只是 :func:`scheda.audit.audit_event` 的一层薄包装 —— 存在的唯一理由是
    「每条记录都带着人」这件事不该由每个调用点各写一遍（漏一处就是一条无主记录）。
    """
    fields.setdefault("actor", current_actor())
    audit_event(cfg, action, **fields)


# --------------------------------------------------------------------------- #
# 口令哈希
# --------------------------------------------------------------------------- #
def _password_bytes(password) -> bytes:
    if isinstance(password, bytes):
        return password
    return str(password if password is not None else "").encode("utf-8")


def hash_password(password, *, salt: bytes | None = None) -> tuple[str, str]:
    """派生口令，返回 ``(salt_hex, hash_hex)``。

    盐默认 ``os.urandom(16)``。同一个口令两次调用得到**不同的盐与不同的派生值**
    （这是盐存在的意义）；显式传 ``salt`` 只给测试与迁移用，生产路径不该传。
    """
    material = os.urandom(SALT_BYTES) if salt is None else bytes(salt)
    digest = hashlib.scrypt(
        _password_bytes(password),
        salt=material,
        n=SCRYPT_N,
        r=SCRYPT_R,
        p=SCRYPT_P,
        dklen=SCRYPT_DKLEN,
        maxmem=SCRYPT_MAXMEM,
    )
    return material.hex(), digest.hex()


def verify_password(password, salt_hex, hash_hex) -> bool:
    """常量时间比较；任何格式问题都返回 ``False`` 而不是抛异常。

    仓库里的 ``users.json`` 可能被手改坏（少一位 hex、多了空格、被写成 ``null``）。
    登录路径不能因为一条坏记录就 500 —— 坏记录的语义是「这个人登不进来」，
    而不是「整个鉴权层崩掉」。
    """
    try:
        salt = bytes.fromhex(str(salt_hex or ""))
        expected = bytes.fromhex(str(hash_hex or ""))
    except (TypeError, ValueError):
        return False
    if not salt or not expected:
        return False
    try:
        actual = hashlib.scrypt(
            _password_bytes(password),
            salt=salt,
            n=SCRYPT_N,
            r=SCRYPT_R,
            p=SCRYPT_P,
            dklen=len(expected),
            maxmem=SCRYPT_MAXMEM,
        )
    except (ValueError, TypeError):  # pragma: no cover - 参数来自本模块常量
        return False
    return hmac.compare_digest(actual, expected)


def _dummy_material() -> tuple[bytes, bytes]:
    """一条与任何真实用户都无关的盐/派生值，用于「用户不存在」时消耗同样的时间。

    懒计算并缓存：模块 import 时不该花 40 ms 做一次没人要的 scrypt。
    """
    global _DUMMY
    if _DUMMY is None:
        salt_hex, hash_hex = hash_password(secrets.token_urlsafe(16))
        _DUMMY = (bytes.fromhex(salt_hex), bytes.fromhex(hash_hex))
    return _DUMMY


# --------------------------------------------------------------------------- #
# 开关
# --------------------------------------------------------------------------- #
def _truthy(value) -> bool:
    return str(value or "").strip().lower() not in ("", "0", "false", "no", "off")


def requested(*, explicit: bool | None = None) -> bool:
    """有人显式要求开启鉴权吗？``explicit`` 优先，其次 ``SCHEDA_AUTH``。

    ``explicit is False`` 表示「明确关掉」（测试与「我就是不要鉴权」的场景），
    此时环境变量不参与 —— 显式的否定比环境变量更强。
    """
    if explicit is False:
        return False
    if explicit is True:
        return True
    return _truthy(os.environ.get(ENV_FLAG))


def enabled(cfg, *, explicit: bool | None = None) -> bool:
    """鉴权是否生效。**唯一开关判据**，所有入口都该问它。

    两个条件缺一不可：

    * ``<home>/users.json`` 存在 —— 不存在时打开鉴权 = 没人能登录 = 把自己锁死；
    * :func:`requested` —— 文件存在本身不构成「我要鉴权」的意思表示。
    """
    if not requested(explicit=explicit):
        return False
    try:
        return users_path(cfg).is_file()
    except OSError:  # pragma: no cover - home 不可达时按关闭处理
        return False


# --------------------------------------------------------------------------- #
# 原子读写（flock + tmp + os.replace）
# --------------------------------------------------------------------------- #
@contextlib.contextmanager
def _home_lock(folder: Path):
    """以 **目录 fd** 上的 ``flock`` 串行化「读-改-写」。

    与 :func:`scheda.audit._rotation_lock` 同一手法：不新建 ``.lock`` 文件，
    所以状态目录里不会多出一个谁也不知道该不该备份的东西。拿不到 ``fcntl``
    （Windows）或锁不上时**照常读写**：退化到「极小概率丢一条并发写」，
    而不是因为锁不可用就干脆不让登录。
    """
    try:
        import fcntl
    except ImportError:  # pragma: no cover - 只在 Windows 上走到
        yield
        return
    handle = None
    try:
        Path(folder).mkdir(parents=True, exist_ok=True)
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


def _read_json(path: Path) -> dict:
    """读一个 JSON 对象；不存在 / 不是对象 / 坏了都返回 ``{}``。

    坏文件在这里**不抛异常**：用户库坏了的结果是「谁都登不进来」（fail closed），
    会话库坏了的结果是「会话失效」（用户重登一次）。两者都不该变成 500。
    """
    try:
        raw = Path(path).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return {}
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        return {}
    return data if isinstance(data, dict) else {}


def _tighten(path: Path) -> None:
    """历史遗留的宽权限文件顺手收紧到 0600（best effort，从不抛）。"""
    try:
        if Path(path).stat().st_mode & 0o077:
            os.chmod(path, PRIVATE_MODE)
    except OSError:
        pass


def _write_all(fd: int, data: bytes) -> None:
    view = memoryview(data)
    while view:
        written = os.write(fd, view)
        view = view[written:]


def _atomic_write(path: Path, payload: dict) -> None:
    """写一个 0600 的 JSON 文件，**原子替换**，从不出现可读的中间态。

    顺序：``mkstemp``（天生 0600）→ ``fchmod`` 显式钉死 → 写 → ``fsync`` →
    ``os.replace``。任何一步失败都清掉临时文件，目标文件保持旧内容（宁可不改，
    也不要半个 ``users.json`` 让所有人登不进来）。
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    data = (json.dumps(payload, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp",
                               dir=str(path.parent))
    try:
        try:
            os.fchmod(fd, PRIVATE_MODE)
        except OSError:  # pragma: no cover - 非 POSIX 或文件系统不支持
            pass
        _write_all(fd, data)
        os.fsync(fd)
    finally:
        os.close(fd)
    try:
        os.replace(tmp, path)
    except OSError:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _now_iso() -> str:
    """本机时区、秒级 —— 与 ``audit._now`` 同格式，人读一致。"""
    return datetime.now().astimezone().isoformat(timespec="seconds")


# --------------------------------------------------------------------------- #
# 用户库
# --------------------------------------------------------------------------- #
def load_users(cfg) -> list[dict]:
    """读用户库，返回 ``list[dict]``；文件不存在或坏掉时返回 ``[]``。

    坏记录（不是对象、缺 name）被跳过而不是整体失败：一条手改坏的记录不该让
    其他所有人登不进来。``salt``/``hash`` 缺失的记录会被保留但永远验证失败 ——
    它仍然是「这个人存在」，只是没有可用口令，行为上等于登不进来。
    """
    path = users_path(cfg)
    data = _read_json(path)
    raw = data.get("users")
    if not isinstance(raw, list):
        return []
    out: list[dict] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        if not name:
            continue
        record = dict(item)
        record["name"] = name
        record["role"] = normalise_role(record.get("role"))
        zones = record.get("zones")
        record["zones"] = [str(z) for z in zones] if isinstance(zones, list) else []
        out.append(record)
    _tighten(path)
    return out


def save_users(cfg, users) -> None:
    """整表写入用户库（0600，原子）。调用方通常应该用 :func:`add_user`。"""
    records = [dict(u) for u in users if isinstance(u, dict) and u.get("name")]
    with _home_lock(Path(cfg.home)):
        _atomic_write(users_path(cfg), {"users": records})


def normalise_role(role) -> str:
    """不认识的 / 空角色退回 ``reader`` —— 配置写错必须是「权限变小」。"""
    text = str(role or "").strip().lower()
    return text if text in ROLES else DEFAULT_ROLE


def has_capability(role, capability: str) -> bool:
    """这个角色有没有某项能力。未知角色按 ``reader`` 算（最小权限）。"""
    return str(capability) in CAPABILITIES.get(normalise_role(role), frozenset())


def find_user(cfg, name) -> dict | None:
    """按名字查用户（大小写敏感：``Alice`` ≠ ``alice``）。"""
    wanted = str(name or "").strip()
    if not wanted:
        return None
    for user in load_users(cfg):
        if user["name"] == wanted:
            return user
    return None


def add_user(cfg, name, password, *, role: str = DEFAULT_ROLE,
             zones=("z0-public",)) -> dict:
    """新建（或覆盖同名）用户，返回落盘的记录。**不含明文口令**。

    ``zones`` 允许写前缀（``"z1"`` → ``z1-restricted``）或保留值
    （``"all"`` / ``"z01"``），见 :func:`expand_zones`；存进去的是**原样**写下的
    字符串，展开在读取时做 —— 这样改配置里的分区名之后，用户库不必迁移。
    """
    if not NAME_RE.match(str(name or "")):
        raise ValueError("用户名只能是 1–64 位 [A-Za-z0-9._-]")
    if not str(password or ""):
        raise ValueError("口令不能为空")
    salt_hex, hash_hex = hash_password(password)
    record = {
        "name": str(name),
        "role": normalise_role(role),
        "zones": [str(z) for z in (zones or ())],
        "salt": salt_hex,
        "hash": hash_hex,
        "created": _now_iso(),
    }
    with _home_lock(Path(cfg.home)):
        data = _read_json(users_path(cfg))
        current = data.get("users") if isinstance(data.get("users"), list) else []
        kept = [u for u in current
                if not (isinstance(u, dict) and str(u.get("name") or "") == record["name"])]
        kept.append(record)
        _atomic_write(users_path(cfg), {"users": kept})
    return dict(record)


def set_password(cfg, name, password) -> bool:
    """改口令并清掉该用户的全部会话；用户不存在返回 ``False``。

    清会话是刻意的：改密码的心理预期就是「旧登录立刻失效」。
    """
    wanted = str(name or "").strip()
    salt_hex, hash_hex = hash_password(password)
    found = False
    with _home_lock(Path(cfg.home)):
        data = _read_json(users_path(cfg))
        current = data.get("users") if isinstance(data.get("users"), list) else []
        for item in current:
            if isinstance(item, dict) and str(item.get("name") or "") == wanted:
                item["salt"] = salt_hex
                item["hash"] = hash_hex
                found = True
        if found:
            _atomic_write(users_path(cfg), {"users": current})
    if found:
        revoke_user_sessions(cfg, wanted)
    return found


def authenticate(cfg, name, password) -> dict | None:
    """校验口令，成功返回用户记录，失败返回 ``None``。

    **两条失败路径花同样的时间。** 用户不存在时也跑一次 scrypt（对
    :func:`_dummy_material` 里的假材料），否则「响应快 = 这个人不存在」就是一个
    免费的用户枚举接口。文案一致性由 webui 负责，时间一致性由这里负责。
    """
    user = find_user(cfg, name)
    if user is None:
        salt, expected = _dummy_material()
        verify_password(password, salt.hex(), expected.hex())
        return None
    if verify_password(password, user.get("salt"), user.get("hash")):
        return user
    return None


# --------------------------------------------------------------------------- #
# zone 授权
# --------------------------------------------------------------------------- #
def expand_zones(cfg, values) -> tuple[str, ...]:
    """把用户库里的分区写法展开成**配置里真实存在的**分区名。

    支持三种写法，展开顺序即书写顺序，重复项去掉：

    * 精确名 ``z1-restricted``；
    * 前缀 ``z1``（与 ``config.zones_for_query`` 的前缀语义一致）；
    * 保留值 ``all``（全部分区）/ ``z01`` / ``default``（可索引分区）。

    配置里不存在的名字**静默丢弃** —— 用户库提到一个本机没有的分区，正确语义是
    「他在这个机器上无权访问任何东西」，而不是「给他一个看不见的分区」。
    """
    known = [str(z.name) for z in getattr(cfg, "zones", ()) or ()]
    indexable = [str(z.name) for z in getattr(cfg, "zones", ()) or () if getattr(z, "index", False)]
    out: list[str] = []
    for raw in values or ():
        text = str(raw or "").strip()
        if not text:
            continue
        if text == ALL_SELECTOR:
            out.extend(known)
        elif text in (DEFAULT_SELECTOR, "default"):
            out.extend(indexable)
        elif text in known:
            out.append(text)
        else:
            out.extend([n for n in known if n.startswith(text)])
    return tuple(dict.fromkeys(out))


def allowed_zones(user, cfg) -> tuple[str, ...]:
    """该用户可读的分区名。

    * 没有用户（鉴权关闭 / 未登录）→ 返回 ``()``，调用方不该拿它当「全部」用；
      关闭状态的放行语义由调用方自己短路（见 :func:`zone_allows_path`）。
    * ``admin`` → 配置里的**全部**分区，忽略 ``zones`` 字段（管理员的定义如此）。
    * 其他角色 → ``zones`` 展开结果。
    """
    if not user:
        return ()
    if normalise_role(user.get("role")) == "admin":
        return tuple(str(z.name) for z in getattr(cfg, "zones", ()) or ())
    return expand_zones(cfg, user.get("zones") or ())


def can_read_zone(user, cfg, zone_name) -> bool:
    """能不能读某个分区。未知分区名一律 ``False``（不猜）。"""
    known = {str(z.name) for z in getattr(cfg, "zones", ()) or ()}
    if str(zone_name) not in known:
        return False
    return str(zone_name) in allowed_zones(user, cfg)


def zone_allows_path(user, cfg, rel) -> bool:
    """某个 bundle 相对路径对该用户是否可读。

    判定顺序（每一层都有理由）：

    1. 路径归一化（反斜杠 → 正斜杠、去掉前导 ``/``）—— 镜像可能是 Windows 产的；
    2. ``_meta/`` 与 ``_incoming/`` 是**结构**而不是隐私分区，任何已登录用户都
       可读（与 ``Config.query_prefixes`` 的语义一致，否则 bundle 自己的约定和
       待审提案会凭空消失）；
    3. 顶层目录是分区 → 看是否在 :func:`allowed_zones` 里；
    4. 顶层目录不是分区（散装文件、别的目录）→ 放行，因为分区策略只管分区。
    """
    text = str(rel or "").replace("\\", "/").lstrip("/")
    if not text:
        return False
    head = text.split("/", 1)[0]
    if head in (META_DIR, INCOMING_DIR):
        return True
    zone = cfg.zone_for(text) if hasattr(cfg, "zone_for") else None
    if zone is None:
        return True
    return str(zone.name) in allowed_zones(user, cfg)


def allowed_selectors(user, cfg) -> tuple[str, ...]:
    """搜索下拉框该给出哪些选择器（``z01``、各分区、必要时 ``all``）。

    只在该用户能访问的分区里出现 ``all``：给一个「全部」却只返回一部分，比不给
    更糟 —— 读者会以为自己看全了。
    """
    zones = allowed_zones(user, cfg)
    if not zones:
        return (DEFAULT_SELECTOR,)
    known = {str(z.name) for z in getattr(cfg, "zones", ()) or ()}
    out = [DEFAULT_SELECTOR]
    out.extend([z for z in zones if z in known])
    if len(zones) > 1:
        out.append(ALL_SELECTOR)
    return tuple(dict.fromkeys(out))


# --------------------------------------------------------------------------- #
# 会话
# --------------------------------------------------------------------------- #
def token_hash(token) -> str:
    """token 的 sha256（hex）。落盘的永远只有这个值。"""
    return hashlib.sha256(str(token or "").encode("utf-8")).hexdigest()


def session_ttl() -> int:
    """会话有效期（秒）：``SCHEDA_SESSION_TTL`` 优先，否则 12 小时。"""
    raw = os.environ.get(ENV_SESSION_TTL)
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError):
        return DEFAULT_SESSION_TTL
    return value if value > 0 else DEFAULT_SESSION_TTL


def _load_sessions(cfg) -> list[dict]:
    data = _read_json(sessions_path(cfg))
    raw = data.get("sessions")
    if not isinstance(raw, list):
        return []
    out = []
    for item in raw:
        if isinstance(item, dict) and item.get("token_hash") and item.get("user"):
            out.append(dict(item))
    return out


def _save_sessions(cfg, sessions) -> None:
    _atomic_write(sessions_path(cfg), {"sessions": [dict(s) for s in sessions]})


def _expiry(record) -> float:
    try:
        return float(record.get("expires", 0))
    except (TypeError, ValueError):
        return 0.0


def issue_session(cfg, user, *, ttl: int | None = None) -> str:
    """为用户签发会话，返回**明文 token**（只在这一刻存在过，之后只有哈希）。

    顺手清一遍过期记录：会话库唯一的增长来源就是签发，在这里清理意味着它不会
    无限长，也不需要 cron。``ttl <= 0`` 表示「立刻过期」（测试用）。
    """
    name = str((user or {}).get("name") if isinstance(user, dict) else user or "").strip()
    if not name:
        raise ValueError("签发会话需要一个用户")
    lifetime = session_ttl() if ttl is None else int(ttl)
    token = secrets.token_urlsafe(TOKEN_BYTES)
    record = {
        "token_hash": token_hash(token),
        "user": name,
        "created": _now_iso(),
        "expires": time.time() + lifetime,
    }
    with _home_lock(Path(cfg.home)):
        now = time.time()
        kept = [s for s in _load_sessions(cfg) if _expiry(s) > now]
        kept.append(record)
        _save_sessions(cfg, kept)
    _tighten(sessions_path(cfg))
    return token


def resolve_session(cfg, token) -> dict | None:
    """把 token 解成会话（含用户的 ``role`` / ``zones``），无效返回 ``None``。

    每次调用都重新读用户库：角色被降级或用户被删掉时，**下一个请求**就该生效，
    而不是等 12 小时会话自然过期。代价是一次小文件读 —— 相对一次检索可以忽略。
    """
    if not token:
        return None
    wanted = token_hash(token)
    for record in _load_sessions(cfg):
        if not hmac.compare_digest(str(record.get("token_hash") or ""), wanted):
            continue
        if _expiry(record) <= time.time():
            return None
        user = find_user(cfg, record.get("user"))
        if user is None:
            return None            # 用户被删 → 会话立即失效（fail closed）
        return {
            "user": user["name"],
            "role": normalise_role(user.get("role")),
            "zones": list(user.get("zones") or []),
            "created": record.get("created", ""),
            "expires": _expiry(record),
        }
    return None


def revoke_session(cfg, token) -> bool:
    """撤销一个会话（登出）。返回是否真的删掉了某条记录。"""
    if not token:
        return False
    wanted = token_hash(token)
    removed = False
    with _home_lock(Path(cfg.home)):
        kept = []
        for record in _load_sessions(cfg):
            if hmac.compare_digest(str(record.get("token_hash") or ""), wanted):
                removed = True
                continue
            kept.append(record)
        if removed:
            _save_sessions(cfg, kept)
    return removed


def revoke_user_sessions(cfg, name) -> int:
    """撤销某用户的**全部**会话，返回撤掉几条（改口令 / 停权时用）。"""
    wanted = str(name or "").strip()
    if not wanted:
        return 0
    count = 0
    with _home_lock(Path(cfg.home)):
        kept = []
        for record in _load_sessions(cfg):
            if str(record.get("user") or "") == wanted:
                count += 1
                continue
            kept.append(record)
        if count:
            _save_sessions(cfg, kept)
    return count


def purge_expired(cfg) -> int:
    """删掉已过期会话，返回条数。不抛异常（会话库坏了等于 0 条）。"""
    now = time.time()
    removed = 0
    with _home_lock(Path(cfg.home)):
        kept = []
        for record in _load_sessions(cfg):
            if _expiry(record) <= now:
                removed += 1
                continue
            kept.append(record)
        if removed:
            _save_sessions(cfg, kept)
    return removed


def active_sessions(cfg, *, now: float | None = None) -> list[dict]:
    """当前有效会话（**不含** token 与哈希），供运维与将来的管理界面使用。"""
    moment = time.time() if now is None else float(now)
    return [
        {"user": r.get("user"), "created": r.get("created"), "expires": _expiry(r)}
        for r in _load_sessions(cfg)
        if _expiry(r) > moment
    ]


__all__ = [
    "ALL_SELECTOR",
    "CAPABILITIES",
    "DEFAULT_ROLE",
    "DEFAULT_SELECTOR",
    "DEFAULT_SESSION_TTL",
    "ENV_FLAG",
    "ENV_SESSION_TTL",
    "NAME_RE",
    "PRIVATE_MODE",
    "ROLES",
    "SCRYPT_DKLEN",
    "SCRYPT_MAXMEM",
    "SCRYPT_N",
    "SCRYPT_P",
    "SCRYPT_R",
    "SESSIONS_NAME",
    "TOKEN_BYTES",
    "USERS_NAME",
    "acting_as",
    "active_sessions",
    "add_user",
    "allowed_selectors",
    "allowed_zones",
    "audit",
    "authenticate",
    "can_read_zone",
    "current_actor",
    "enabled",
    "expand_zones",
    "find_user",
    "has_capability",
    "hash_password",
    "issue_session",
    "load_users",
    "normalise_role",
    "purge_expired",
    "requested",
    "resolve_session",
    "revoke_session",
    "revoke_user_sessions",
    "save_users",
    "session_ttl",
    "sessions_path",
    "set_actor",
    "set_password",
    "token_hash",
    "users_path",
    "verify_password",
    "zone_allows_path",
]
