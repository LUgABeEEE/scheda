# SPDX-License-Identifier: AGPL-3.0-or-later
"""Where does this path live — on POSIX *and* on Windows?

:func:`handrolled.mounts.describe_path` answers that question from
``/proc/mounts``.  That is the right source on Linux and macOS (``cifs``,
``nfs4``, ``autofs`` triggers, ``ro`` options) and it is what this module
delegates to there, byte for byte.

On Windows there is no ``/proc/mounts``: the read fails, the table comes back
empty and every path degrades to ``fstype="local", readonly=False`` — a mapped
``Z:\\kb`` looks exactly like ``C:\\Temp``.  Windows answers the same questions
through three different APIs instead:

===================================  ==========================================
question                             how Windows is asked
===================================  ==========================================
is it a network drive?               ``kernel32.GetDriveTypeW`` → ``DRIVE_REMOTE``
                                       (4); a ``\\\\server\\share`` path is a UNC
                                       path by spelling alone
what is it mapped to / is it gone?   ``mpr.WNetGetConnectionW`` → the UNC target,
                                       or a hard failure when the mapping is
                                       disconnected (the ESTALE analogue)
is it writable?                      not answerable from a mount table at all —
                                       :func:`probe_write` writes a 0-byte file
===================================  ==========================================

So ``describe`` reports ``readonly=None`` ("unknown") on Windows and leaves the
verdict to a real write.  Guessing ``False`` is what made ``push`` die on a
permission error three files in; guessing ``True`` would refuse a writable
share.

Every Windows call goes through :func:`_win_api`, a module-level factory that
returns a small wrapper around ``ctypes.WinDLL`` (or ``None`` when the APIs are
not there).  That single seam is what lets the whole Windows branch be tested
on Linux, and it is also the reason no failure below can raise: a broken
``ctypes`` returns a ``platform="windows-unknown"`` descriptor instead of a
traceback.

The returned dict keeps the shape of ``handrolled.mounts.describe_path``
(``path``, ``platform``, ``unc``, ``mountpoint``, ``fstype``, ``device``,
``network``, ``readonly``) with two Windows-only additions: ``remote`` (the UNC
target behind a mapped drive) and ``stale`` (the mapping is disconnected).
``platform`` is normalised to ``"posix"`` / ``"windows"`` / ``"windows-unknown"``
so callers and tests can branch on it; the raw OS name stays available as
``sys.platform``.
"""
from __future__ import annotations

import os
import tempfile
from pathlib import Path

from handrolled.mounts import describe_path as _posix_describe_path
from handrolled.mounts import is_unc
from handrolled.mounts import stale_hint as _posix_stale_hint

__all__ = [
    "DRIVE_UNKNOWN",
    "DRIVE_NO_ROOT_DIR",
    "DRIVE_REMOVABLE",
    "DRIVE_FIXED",
    "DRIVE_REMOTE",
    "DRIVE_CDROM",
    "DRIVE_RAMDISK",
    "NETWORK_DRIVE_TYPES",
    "NETWORK_FSTYPES",
    "describe_path",
    "is_unc",
    "probe_write",
    "stale_hint",
]

#: ``GetDriveTypeW`` return values (winbase.h).
DRIVE_UNKNOWN = 0
DRIVE_NO_ROOT_DIR = 1
DRIVE_REMOVABLE = 2
DRIVE_FIXED = 3
DRIVE_REMOTE = 4
DRIVE_CDROM = 5
DRIVE_RAMDISK = 6

#: Drive types that mean "somebody else's disk over a wire".
NETWORK_DRIVE_TYPES = frozenset({DRIVE_REMOTE})

#: ``fstype`` spellings this module can emit for a Windows path.  A connected
#: mapping is reported as ``unc`` (that is literally what it resolves to); a
#: disconnected one cannot be resolved, so it stays ``remote``.
NETWORK_FSTYPES = frozenset({"unc", "remote", "cifs", "smb3", "nfs", "webdav"})

_FSTYPE_BY_DRIVE_TYPE = {
    DRIVE_UNKNOWN: "local",
    DRIVE_NO_ROOT_DIR: "no-root",
    DRIVE_REMOVABLE: "local",
    DRIVE_FIXED: "local",
    DRIVE_CDROM: "cdrom",
    DRIVE_RAMDISK: "ramdisk",
}

#: ``\\\\?\\`` / ``\\\\.\\`` device-path prefixes ``GetDriveTypeW`` understands
#: but a naive drive-letter test does not.
_LONG_PATH_PREFIXES = ("\\\\?\\UNC\\", "\\\\?\\", "\\\\.\\")

#: ``WNetGetConnectionW`` buffer: drive mappings are far shorter than this; the
#: call reports the required size on ``ERROR_MORE_DATA``.
_UNC_BUFFER = 1024


# ---------------------------------------------------------------------------
# Win32 API seam
# ---------------------------------------------------------------------------
class _WindowsApi:
    """The two Win32 calls this module needs, behind plain Python methods.

    Tests never touch this class: they replace :func:`_win_api` with a stub
    exposing ``drive_type(root)`` and ``connection(drive)``.
    """

    def __init__(self) -> None:
        import ctypes
        from ctypes import wintypes  # noqa: PLC0415 - Windows-only module

        self._ctypes = ctypes
        self._wintypes = wintypes
        self.kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self.mpr = ctypes.WinDLL("mpr", use_last_error=True)

        self._get_drive_type = self.kernel32.GetDriveTypeW
        self._get_drive_type.argtypes = [wintypes.LPCWSTR]
        self._get_drive_type.restype = wintypes.UINT

        self._wnet_get_connection = self.mpr.WNetGetConnectionW
        self._wnet_get_connection.argtypes = [
            wintypes.LPCWSTR,
            wintypes.LPWSTR,
            ctypes.POINTER(wintypes.DWORD),
        ]
        self._wnet_get_connection.restype = wintypes.DWORD

    def drive_type(self, root: str) -> int:
        """``GetDriveTypeW(root)``; raises only if ctypes itself is broken."""
        return int(self._get_drive_type(root))

    def connection(self, drive: str) -> str | None:
        """The UNC target of a mapped drive, or ``None`` when it is disconnected.

        ``WNetGetConnectionW`` returns a non-zero code (``ERROR_CONNECTION_UNAVAIL``,
        ``ERROR_NOT_CONNECTED``, …) for a mapping whose server is gone; that is
        the Windows form of ``ESTALE``, not an exception.
        """
        buf = self._ctypes.create_unicode_buffer(_UNC_BUFFER)
        size = self._wintypes.DWORD(_UNC_BUFFER)
        code = self._wnet_get_connection(drive, buf, self._ctypes.byref(size))
        if code != 0:
            return None
        return buf.value or None


def _win_api():
    """Return a Win32 API wrapper, or ``None`` when this host cannot provide one.

    This is the module's single monkeypatch point: replacing it with a stub (or
    with ``lambda: None``) exercises every Windows branch from Linux.
    """
    try:
        return _WindowsApi()
    except Exception:  # noqa: BLE001 - any ctypes failure means "cannot tell"
        return None


def _is_windows() -> bool:
    """True on the real Windows runtime (patched in tests, never inferred)."""
    return os.name == "nt"


# ---------------------------------------------------------------------------
# Path helpers
# ---------------------------------------------------------------------------
def _strip_device_prefix(text: str) -> str:
    for prefix in _LONG_PATH_PREFIXES:
        if text.upper().startswith(prefix):
            return text[len(prefix):]
    return text


def _drive_root(path: str | os.PathLike) -> str | None:
    """``"Z:\\\\"`` for ``Z:\\kb``, ``Z:/kb``, ``Z:`` and ``\\\\?\\Z:\\kb``."""
    text = _strip_device_prefix(os.fspath(path))
    if len(text) >= 2 and text[1] == ":" and text[0].isalpha():
        return text[0].upper() + ":\\"
    return None


def _unc_share(path: str | os.PathLike) -> str:
    """``"\\\\\\\\server\\\\share"`` for any UNC spelling of a path inside it."""
    text = _strip_device_prefix(os.fspath(path)).replace("/", "\\")
    parts = [part for part in text.split("\\") if part]
    if len(parts) >= 2:
        return "\\\\" + parts[0] + "\\" + parts[1]
    if parts:
        return "\\\\" + parts[0]
    return text


def _describe_unc(path: str | os.PathLike) -> dict:
    share = _unc_share(path)
    return _windows_info(
        path,
        fstype="unc",
        network=True,
        remote=share,
        mountpoint=share,
        device=share,
    )


def _windows_info(
    path: str | os.PathLike,
    *,
    fstype: str,
    network: bool,
    readonly: bool | None = None,
    remote: str | None = None,
    stale: bool = False,
    mountpoint: str = "",
    device: str = "",
    platform: str = "windows",
) -> dict:
    """Build the descriptor dict — same keys as ``handrolled``, plus the two above."""
    return {
        "path": str(path),
        "platform": platform,
        "unc": is_unc(path),
        "mountpoint": mountpoint,
        "fstype": fstype,
        "device": device,
        "network": network,
        "readonly": readonly,
        "remote": remote,
        "stale": stale,
    }


# ---------------------------------------------------------------------------
# Windows branch
# ---------------------------------------------------------------------------
def _describe_windows(path: str | os.PathLike) -> dict:
    """Describe a Windows path without ever trusting a mount table.

    ``mounts_text`` is a POSIX seam and is ignored here on purpose: on Windows
    there is no table to read, and pretending otherwise is the bug this module
    exists to fix.
    """
    text = os.fspath(path)

    # A UNC path needs no API call: the spelling *is* the answer, and it is the
    # one case that must survive a broken ctypes.  Both the plain spelling and
    # the extended-length one (``\\?\UNC\server\share``) count.
    if is_unc(text) or is_unc(_strip_device_prefix(text)):
        return _describe_unc(text)

    root = _drive_root(text)
    if root is None:
        # Relative path: ask the OS what it resolves to, then retry once.
        try:
            root = _drive_root(os.path.abspath(text))
        except Exception:  # noqa: BLE001 - cwd unavailable (deleted/dead drive)
            root = None

    api = _win_api()
    if api is None:
        return _workerless_unknown(text)

    if root is None:
        # Not a drive-lettered path, so there is nothing to classify.
        return _windows_info(text, fstype="local", network=False)

    try:
        drive_type = int(api.drive_type(root))
    except Exception:  # noqa: BLE001 - API present but this call failed
        return _workerless_unknown(text)

    if drive_type in NETWORK_DRIVE_TYPES:
        return _describe_mapped_drive(api, text, root)

    fstype = _FSTYPE_BY_DRIVE_TYPE.get(drive_type, "local")
    absent = drive_type == DRIVE_NO_ROOT_DIR
    return _windows_info(
        text,
        fstype=fstype,
        network=False,
        stale=absent,
        mountpoint=root,
        device=root,
    )


def _workerless_unknown(path: str) -> dict:
    """The documented fallback: never raise, say clearly that we do not know."""
    return _windows_info(
        path,
        fstype="local",
        network=False,
        readonly=None,
        remote=None,
        stale=False,
        platform="windows-unknown",
    )


def _describe_mapped_drive(api, path: str, root: str) -> dict:
    """A ``DRIVE_REMOTE`` letter: resolve the UNC target, or flag it stale."""
    target: str | None = None
    try:
        target = api.connection(root[:2])
    except Exception:  # noqa: BLE001 - disconnected mapping raises instead of returning
        target = None
    if target:
        return _windows_info(
            path,
            fstype="unc",
            network=True,
            remote=target,
            mountpoint=root,
            device=target,
        )
    # The letter exists but nothing is behind it: the ESTALE analogue.  Still a
    # network path, so `push` fails with a sentence rather than a traceback.
    return _windows_info(
        path,
        fstype="remote",
        network=True,
        remote=None,
        stale=True,
        mountpoint=root,
        device=root,
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------
def describe_path(path: str | os.PathLike, mounts_text: str | None = None) -> dict:
    """Summarise where a path lives, on either platform.

    POSIX delegates to :func:`handrolled.mounts.describe_path` and only relabels
    ``platform`` (``"posix"`` instead of ``sys.platform``); every other key and
    value is the delegation's own.
    """
    if not _is_windows():
        info = dict(_posix_describe_path(path, mounts_text=mounts_text))
        info["platform"] = "posix"
        return info
    return _describe_windows(path)


def probe_write(folder: str | os.PathLike) -> tuple[bool, str]:
    """Write and remove a 0-byte file — the only honest writability test.

    Used instead of ``os.access`` on Windows, where ``os.access`` ignores the
    ACLs that actually decide the outcome, and instead of a fixed probe name,
    which two concurrent doctors would collide on.
    """
    directory = Path(folder)
    created: str | None = None
    try:
        directory.mkdir(parents=True, exist_ok=True)
        handle, created = tempfile.mkstemp(prefix=".scheda-write-probe-", dir=str(directory))
        os.close(handle)
        return True, "write probe succeeded"
    except OSError as exc:
        return False, stale_hint(exc)
    finally:
        if created:
            try:
                os.unlink(created)
            except OSError:
                pass


def stale_hint(exc: OSError) -> str:
    """Translate a filesystem error into a sentence an operator can act on."""
    if not _is_windows():
        return _posix_stale_hint(exc)
    return _windows_stale_hint(exc)


def _windows_stale_hint(exc: OSError) -> str:
    code = getattr(exc, "winerror", None)
    text = str(exc).lower()
    # 53/67/1231/59/64: the share or the session behind the mapping is gone.
    if code in (53, 59, 64, 67, 1231) or "network path was not found" in text \
            or "network connection" in text or "not connected" in text:
        return (
            "the mapped drive looks stale: Windows cannot reach the share any more "
            f"({exc}).  Reconnect it (`net use Z: /delete` then `net use Z: \\\\host\\share`), "
            "or use the UNC path / SCHEDA_SOURCE=webdav"
        )
    if code in (5, 65, 32) or "access is denied" in text or "being used by another process" in text:
        return (
            "access denied on the share: the account behind the mapping has no write "
            f"access there, the file is locked, or the process is sandboxed ({exc})"
        )
    if code in (2, 3) or "cannot find the path" in text or "cannot find the file" in text:
        return "the share path does not exist: check the server name, share name and subdirectory"
    return f"{type(exc).__name__}: {exc}"
