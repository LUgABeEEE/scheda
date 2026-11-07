"""Windows behaviour of :mod:`scheda.platform_fs`, tested from Linux.

The whole point of this module is that the Windows branch is *reachable* here:
:func:`platform_fs._win_api` is the only place ``ctypes.WinDLL`` is touched, so
stubbing ``_is_windows`` + ``_win_api`` drives every real code path without a
Windows machine, a mapped drive or CI on another OS.

What each test pins down:

* ``test_posix_*``      the POSIX branch is pure delegation — adding ``platform``
                        must not change a single other key or value.
* ``test_windows_remote_drive_*``   a ``DRIVE_REMOTE`` letter is a network path.
* ``test_windows_mapped_drive_*``   the mapping resolves to its UNC target.
* ``test_windows_disconnected_*``   a dead mapping is ``stale`` (ESTALE analogue),
                        not an exception.
* ``test_windows_without_ctypes_*`` no ``ctypes`` → ``windows-unknown``, no raise.
* ``test_describe_*``   ``FilesystemSource.describe`` fills ``readonly`` from a
                        real probe, and a failed probe is *not* a read-only verdict.
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import handrolled.mounts as handrolled_mounts  # noqa: E402
from scheda import platform_fs, sources  # noqa: E402
from scheda.sources import FilesystemSource  # noqa: E402

#: A POSIX mount table that makes ``/mnt/kb`` a read-only CIFS view.
MOUNTS = (
    "/dev/sda2 / ext4 rw,relatime 0 0\n"
    "//192.0.2.10/kb /mnt/kb cifs ro,relatime,vers=3.0 0 0\n"
)


def win_oserror(winerror: int, message: str) -> OSError:
    """An ``OSError`` carrying a Windows ``winerror`` (cannot be passed to the
    constructor on Linux)."""
    exc = OSError(2, message)
    exc.winerror = winerror
    return exc


class FakeWinApi:
    """Stands in for :class:`platform_fs._WindowsApi`.

    ``drive_type`` mirrors ``GetDriveTypeW``; ``connection`` mirrors
    ``mpr.WNetGetConnectionW`` (``None`` = disconnected, raising = API error).
    """

    def __init__(
        self,
        drive_type: int = platform_fs.DRIVE_FIXED,
        mapping: str | None = None,
        *,
        fail_drive_type: bool = False,
        fail_connection: bool = False,
    ) -> None:
        self.drive_type_value = drive_type
        self.mapping = mapping
        self.fail_drive_type = fail_drive_type
        self.fail_connection = fail_connection
        self.drive_type_calls: list[str] = []
        self.connection_calls: list[str] = []

    def drive_type(self, root: str) -> int:
        self.drive_type_calls.append(root)
        if self.fail_drive_type:
            raise OSError(1, "GetDriveTypeW failed")
        return self.drive_type_value

    def connection(self, drive: str) -> str | None:
        self.connection_calls.append(drive)
        if self.fail_connection:
            raise win_oserror(67, "The network name cannot be found")
        return self.mapping


class WindowsStub:
    """Context manager: ``os.name == "nt"`` plus a chosen Win32 API object."""

    def __init__(self, api: object) -> None:
        self.api = api
        self._patches: list = []

    def __enter__(self):
        self._patches = [
            mock.patch.object(platform_fs, "_is_windows", return_value=True),
            mock.patch.object(platform_fs, "_win_api", return_value=self.api),
        ]
        for patch in self._patches:
            patch.start()
        return self.api

    def __exit__(self, *exc_info):
        for patch in reversed(self._patches):
            patch.stop()
        return False


def windows(api: object = None) -> WindowsStub:
    """Pretend to be Windows; ``api=None`` means "ctypes is not available"."""
    return WindowsStub(api)


class PosixDelegationTests(unittest.TestCase):
    """1. Nothing about the POSIX answer may change except ``platform``."""

    def test_posix_matches_handrolled_except_platform(self):
        ours = platform_fs.describe_path("/tmp")
        theirs = handrolled_mounts.describe_path("/tmp")
        self.assertEqual(set(ours), set(theirs), "key set must stay identical")
        self.assertEqual(ours.pop("platform"), "posix")
        theirs.pop("platform")
        self.assertEqual(ours, theirs)

    def test_posix_passes_mounts_text_through(self):
        ours = platform_fs.describe_path("/mnt/kb", mounts_text=MOUNTS)
        self.assertTrue(ours["network"])
        self.assertTrue(ours["readonly"])
        self.assertEqual(ours["fstype"], "cifs")
        self.assertEqual(ours["platform"], "posix")

    def test_posix_stale_hint_is_the_handrolled_one(self):
        exc = OSError(116, "Stale file handle")
        self.assertEqual(platform_fs.stale_hint(exc), handrolled_mounts.stale_hint(exc))
        self.assertIn("ESTALE", platform_fs.stale_hint(exc))

    def test_posix_readonly_is_a_bool_never_none(self):
        # The Windows-only "unknown" sentinel must not leak into POSIX output:
        # `sources.describe` uses `readonly is None` to decide to probe.
        for text in (None, MOUNTS):
            info = platform_fs.describe_path("/mnt/kb", mounts_text=text)
            self.assertIsInstance(info["readonly"], bool)


class WindowsDriveTypeTests(unittest.TestCase):
    """2. ``GetDriveTypeW == DRIVE_REMOTE`` makes a drive letter a network path."""

    def test_windows_remote_drive_is_network(self):
        api = FakeWinApi(platform_fs.DRIVE_REMOTE)
        with windows(api):
            info = platform_fs.describe_path("Z:\\kb")
        self.assertTrue(info["network"])
        self.assertIn(info["fstype"], platform_fs.NETWORK_FSTYPES)
        self.assertEqual(info["platform"], "windows")
        self.assertEqual(api.drive_type_calls, ["Z:\\"])
        # No mount table can answer writability here.
        self.assertIsNone(info["readonly"])

    def test_windows_fixed_drive_is_local(self):
        api = FakeWinApi(platform_fs.DRIVE_FIXED)
        with windows(api):
            info = platform_fs.describe_path("C:\\kb")
        self.assertFalse(info["network"])
        self.assertEqual(info["fstype"], "local")
        self.assertIsNone(info["readonly"])
        self.assertFalse(info["stale"])

    def test_windows_missing_root_dir_is_stale(self):
        api = FakeWinApi(platform_fs.DRIVE_NO_ROOT_DIR)
        with windows(api):
            info = platform_fs.describe_path("Q:\\kb")
        self.assertFalse(info["network"])
        self.assertEqual(info["fstype"], "no-root")
        self.assertTrue(info["stale"])


class WindowsMappingTests(unittest.TestCase):
    """3. A mapped letter reports the UNC target ``WNetGetConnectionW`` returns."""

    def test_windows_mapped_drive_reports_unc_target(self):
        api = FakeWinApi(platform_fs.DRIVE_REMOTE, mapping="\\\\nas\\kb")
        with windows(api):
            info = platform_fs.describe_path("Z:\\kb")
        self.assertEqual(info["remote"], "\\\\nas\\kb")
        self.assertEqual(info["fstype"], "unc")
        self.assertTrue(info["network"])
        self.assertFalse(info["stale"])
        self.assertEqual(info["platform"], "windows")
        # WNetGetConnectionW takes the drive, not the full path.
        self.assertEqual(api.connection_calls, ["Z:"])


class WindowsStaleMappingTests(unittest.TestCase):
    """4. A disconnected mapping is ``stale``, and never an exception."""

    def test_windows_disconnected_mapping_is_stale(self):
        api = FakeWinApi(platform_fs.DRIVE_REMOTE, fail_connection=True)
        with windows(api):
            info = platform_fs.describe_path("Z:\\kb")
        self.assertTrue(info["stale"])
        self.assertTrue(info["network"])
        self.assertIsNone(info["remote"])
        self.assertEqual(info["platform"], "windows")

    def test_windows_mapping_returning_none_is_stale(self):
        api = FakeWinApi(platform_fs.DRIVE_REMOTE, mapping=None)
        with windows(api):
            info = platform_fs.describe_path("Z:\\kb")
        self.assertTrue(info["stale"])
        self.assertTrue(info["network"])

    def test_windows_stale_hint_is_actionable(self):
        with windows(FakeWinApi()):
            hint = platform_fs.stale_hint(win_oserror(53, "The network path was not found"))
        self.assertIn("stale", hint.lower())
        self.assertIn("net use", hint)

    def test_windows_access_denied_hint_differs_from_stale(self):
        with windows(FakeWinApi()):
            hint = platform_fs.stale_hint(win_oserror(5, "Access is denied"))
        self.assertIn("access denied", hint.lower())
        self.assertNotIn("net use", hint)


class WindowsUnknownTests(unittest.TestCase):
    """5. No ctypes at all → the documented fallback dict, no traceback."""

    def test_windows_without_ctypes_is_unknown(self):
        with windows(None):
            info = platform_fs.describe_path("Z:\\kb")
        self.assertEqual(info["platform"], "windows-unknown")
        self.assertEqual(info["fstype"], "local")
        self.assertFalse(info["network"])
        self.assertIsNone(info["readonly"])
        self.assertIsNone(info["remote"])
        self.assertFalse(info["stale"])

    def test_windows_failing_api_call_is_unknown(self):
        api = FakeWinApi(fail_drive_type=True)
        with windows(api):
            info = platform_fs.describe_path("Z:\\kb")
        self.assertEqual(info["platform"], "windows-unknown")
        self.assertFalse(info["network"])

    def test_real_win_api_returns_none_on_linux(self):
        # The seam itself must degrade rather than raise where there is no WinDLL.
        if sys.platform.startswith("win"):       # pragma: no cover - real Windows
            self.skipTest("the fallback cannot be exercised on Windows")
        self.assertIsNone(platform_fs._win_api())


class WindowsUncTests(unittest.TestCase):
    """7. A UNC path is recognised by spelling, no API required."""

    def test_unc_spelling_is_network(self):
        with windows(None):                      # deliberately no ctypes at all
            info = platform_fs.describe_path(r"\\server\share\kb")
        self.assertEqual(info["fstype"], "unc")
        self.assertTrue(info["network"])
        self.assertEqual(info["remote"], "\\\\server\\share")
        self.assertTrue(info["unc"])

    def test_forward_slash_unc_is_network(self):
        with windows(None):
            info = platform_fs.describe_path("//server/share/kb")
        self.assertEqual(info["fstype"], "unc")
        self.assertTrue(info["network"])

    def test_extended_length_unc_is_network(self):
        with windows(None):
            info = platform_fs.describe_path("\\\\?\\UNC\\server\\share\\kb")
        self.assertEqual(info["fstype"], "unc")
        self.assertEqual(info["remote"], "\\\\server\\share")


class FilesystemSourceWindowsTests(unittest.TestCase):
    """6. ``describe`` completes ``readonly`` from one real write probe."""

    WINDOWS_MOUNT = {
        "path": "Z:\\kb",
        "platform": "windows",
        "unc": False,
        "mountpoint": "Z:\\",
        "fstype": "unc",
        "device": "\\\\nas\\kb",
        "network": True,
        "readonly": None,
        "remote": "\\\\nas\\kb",
        "stale": False,
    }

    def _source(self, mount: dict) -> FilesystemSource:
        with mock.patch.object(sources, "describe_path", return_value=dict(mount)):
            return FilesystemSource(tempfile.mkdtemp(), label="win")

    def test_describe_fills_readonly_from_probe(self):
        source = self._source(self.WINDOWS_MOUNT)
        with mock.patch.object(
            FilesystemSource, "probe_writable", return_value=(True, "write probe succeeded")
        ) as probe:
            info = source.describe()
        probe.assert_called_once_with()
        self.assertIs(info["readonly"], False)
        self.assertEqual(info["probe_error"], "")
        self.assertEqual(info["label"], "win")

    def test_failed_probe_does_not_report_read_only(self):
        source = self._source(self.WINDOWS_MOUNT)
        reason = "permission denied on the share"
        with mock.patch.object(FilesystemSource, "probe_writable", return_value=(False, reason)):
            info = source.describe()
        self.assertIsNone(info["readonly"], "a probe that fails is not a read-only verdict")
        self.assertEqual(info["probe_error"], reason)

    def test_posix_describe_never_probes(self):
        mount = dict(self.WINDOWS_MOUNT, platform="posix", readonly=True, fstype="cifs")
        source = self._source(mount)
        with mock.patch.object(FilesystemSource, "probe_writable") as probe:
            info = source.describe()
        probe.assert_not_called()
        self.assertNotIn("probe_error", info)
        self.assertEqual(info, {**mount, "label": "win", "root": str(source.root)})

    def test_windows_probe_writes_a_zero_byte_file(self):
        source = self._source(self.WINDOWS_MOUNT)
        with mock.patch.object(sources, "probe_write", return_value=(True, "ok")) as probe_write:
            ok, why = source.probe_writable("_incoming")
        self.assertTrue(ok)
        self.assertEqual(why, "ok")
        probe_write.assert_called_once_with(source.root / "_incoming")

    def test_posix_probe_still_uses_the_fixed_probe_name(self):
        mount = dict(self.WINDOWS_MOUNT, platform="posix", readonly=False, fstype="cifs")
        source = self._source(mount)
        with mock.patch.object(sources, "probe_write") as probe_write:
            ok, why = source.probe_writable()
        self.assertTrue(ok)
        self.assertEqual(why, "write probe succeeded")
        probe_write.assert_not_called()
        self.assertFalse((source.root / sources.PROBE_NAME).exists())

    def test_windows_end_to_end_uses_the_real_probe(self):
        """No stub on ``probe_writable``: the genuine 0-byte write decides it.

        ``sources.describe_path`` *is* ``platform_fs.describe_path``, so the
        Windows stub reaches the source class the same way it reaches the
        module — this is the wiring that a hand-written fake would hide.
        """
        with tempfile.TemporaryDirectory() as tmp:
            with windows(None):                  # no ctypes → windows-unknown
                source = FilesystemSource(tmp, label="win-unknown")
                info = source.describe()
        self.assertEqual(info["platform"], "windows-unknown")
        self.assertFalse(info["network"])
        self.assertIs(info["readonly"], False, "the real probe succeeded on this tmpdir")
        self.assertEqual(info["probe_error"], "")


class ProbeWriteTests(unittest.TestCase):
    """``probe_write`` is the Windows writability primitive."""

    def test_probe_write_leaves_nothing_behind(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "_incoming"
            ok, why = platform_fs.probe_write(target)
            self.assertTrue(ok)
            self.assertEqual(why, "write probe succeeded")
            # The probe wrote a 0-byte file and removed it: nothing survives.
            self.assertEqual(list(target.iterdir()), [])

    def test_probe_write_creates_missing_subdir(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "a" / "b"
            self.assertTrue(platform_fs.probe_write(target)[0])
            self.assertTrue(target.is_dir())

    def test_probe_write_reports_failure_instead_of_raising(self):
        with tempfile.NamedTemporaryFile() as handle:
            ok, why = platform_fs.probe_write(Path(handle.name) / "sub")
        self.assertFalse(ok)
        self.assertTrue(why)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
