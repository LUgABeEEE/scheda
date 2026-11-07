# SPDX-License-Identifier: AGPL-3.0-or-later
"""Helpers shared by every ``scheda`` sub-command.

These are the same functions that used to sit at the top of :mod:`scheda.cli`,
moved out unchanged (same names, same signatures, same output): the command
modules need one copy of them and must not import the CLI to get it.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

from ..config import Config, load_config
from ..service import file_logger


#: 嵌入端点默认是本地 ollama。doctor 只做一次极短探测：不可达意味着"能查、
#: 但暂时不能重建向量"，那是 warn 不是 fail。
#: 设 SCHEDA_RAG_OLLAMA=- 可跳过探测（没有 ollama 的机器/离线测试）。
RAG_OLLAMA_ENDPOINT = "http://127.0.0.1:11434"
RAG_OLLAMA_TIMEOUT = 0.5


def _emit(message: str, *, quiet: bool = False) -> None:
    if not quiet:
        print(message, file=sys.stderr)


def _log_to(cfg: Config, quiet: bool):
    """Return an ``on_event`` callback that tees to stderr and the log file."""
    write_file = file_logger(cfg)

    def emit(message: str) -> None:
        write_file(message)
        _emit(message, quiet=quiet)

    return emit


def _config(args) -> Config:
    overrides = {
        "source": getattr(args, "source", None),
        "mount": getattr(args, "mount", None),
        "local_root": getattr(args, "local_root", None),
        "smb_share": getattr(args, "smb_share", None),
        "webdav_root": getattr(args, "webdav_root", None),
    }
    home = getattr(args, "home", None)
    if home:
        from pathlib import Path

        overrides["home"] = Path(home).expanduser()
    return load_config(**overrides)


def _write_json_atomic(path: Path, payload: dict) -> None:
    """Write config.json without ever leaving a half-written file behind."""
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)


def _ollama_reachable(endpoint: str | None = None) -> tuple[bool, str]:
    """探一次 ollama 的 ``/api/tags``（只读、短超时）。不可达是结论，不抛异常。"""
    import os
    import urllib.request

    url = (endpoint or os.environ.get("SCHEDA_RAG_OLLAMA") or RAG_OLLAMA_ENDPOINT).rstrip("/")
    if url in ("", "-"):
        return True, "skipped"
    try:
        with urllib.request.urlopen(f"{url}/api/tags", timeout=RAG_OLLAMA_TIMEOUT) as response:
            return True, f"HTTP {response.status}"
    except Exception as exc:  # noqa: BLE001 - 探不通就是结论
        return False, f"{type(exc).__name__}: {exc}"

def cli_seam(name: str, fallback):
    """Resolve ``name`` on :mod:`scheda.cli` at call time.

    Splitting the CLI into ``commands/`` must not break the one monkeypatch seam
    the test suite relies on (``mock.patch("scheda.cli.open_configured_source")``).
    The lookup goes through ``sys.modules``, so ``commands`` never imports
    ``cli`` — that direction would be a cycle — while a patch installed on the
    CLI module still wins over the value bound here at import time.
    """
    module = sys.modules.get("scheda.cli")
    if module is not None:
        return getattr(module, name, fallback)
    return fallback
