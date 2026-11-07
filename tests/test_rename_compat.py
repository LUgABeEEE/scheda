# SPDX-License-Identifier: AGPL-3.0-or-later
"""0.9.0 改名（dsh-kb → scheda）的兼容契约。

改名最危险的失败模式不是 import 报错（那会立刻被发现），而是**静默指向别处**：
环境变量被忽略 → 指向错误的 bundle；state 目录被搬走 → 看起来"知识库空了"；
老命令失效 → 凌晨三点的定时任务不再刷新。这三件事都在这里钉住。
"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from scheda import config  # noqa: E402


class TestEnvFallback(unittest.TestCase):
    def test_new_name_wins(self):
        with mock.patch.dict(os.environ, {"SCHEDA_SOURCE": "local",
                                          "DSH_KB_SOURCE": "webdav"}, clear=False):
            self.assertEqual(config._env("SOURCE"), "local")

    def test_old_name_still_read(self):
        with mock.patch.dict(os.environ, {"DSH_KB_SOURCE": "webdav"}, clear=False):
            os.environ.pop("SCHEDA_SOURCE", None)
            self.assertEqual(config._env("SOURCE"), "webdav",
                             "an upgraded unit file must keep working unedited")

    def test_empty_is_not_a_value(self):
        with mock.patch.dict(os.environ, {"SCHEDA_SOURCE": "", "DSH_KB_SOURCE": "smb"}, clear=False):
            self.assertEqual(config._env("SOURCE"), "smb")

    def test_default(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertIsNone(config._env("NOPE"))
            self.assertEqual(config._env("NOPE", "fallback"), "fallback")


class TestHomeLocality(unittest.TestCase):
    """升级不许看起来像「知识库没了」。"""

    def test_new_home_wins_when_it_exists(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            (home / ".scheda").mkdir()
            (home / ".dsh-kb").mkdir()
            with mock.patch.dict(os.environ, {"HOME": str(home)}, clear=False):
                os.environ.pop("SCHEDA_HOME", None)
                os.environ.pop("DSH_KB_HOME", None)
                self.assertTrue(config.default_home().endswith(".scheda"))

    def test_old_home_keeps_being_used(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            (home / ".dsh-kb").mkdir()
            with mock.patch.dict(os.environ, {"HOME": str(home)}, clear=False):
                os.environ.pop("SCHEDA_HOME", None)
                os.environ.pop("DSH_KB_HOME", None)
                self.assertTrue(config.default_home().endswith(".dsh-kb"),
                                "an existing deployment must not be relocated silently")

    def test_explicit_env_beats_both(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.dict(os.environ, {"HOME": tmp, "SCHEDA_HOME": "/srv/kb"}, clear=False):
                self.assertEqual(config.default_home(), "/srv/kb")


class TestCommandNames(unittest.TestCase):
    def test_pyproject_installs_both_commands(self):
        try:
            import tomllib
        except ModuleNotFoundError:  # Python 3.10
            self.skipTest("tomllib is 3.11+")
        root = Path(__file__).resolve().parents[1]
        scripts = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))["project"]["scripts"]
        self.assertEqual(scripts["scheda"], "scheda.cli:main")
        self.assertEqual(scripts["dsh-kb"], "scheda.cli:main",
                         "the old command is installed by this package during the migration")

    def test_cli_prog_is_the_new_name(self):
        from scheda.cli import build_parser
        self.assertEqual(build_parser().prog, "scheda")


class TestShim(unittest.TestCase):
    """老代码 ``import dsh_kb`` 与 ``from dsh_kb.cli import main`` 仍要工作。"""

    def test_old_module_path_resolves(self):
        shim = Path(__file__).resolve().parents[1] / "shim" / "src"
        if not shim.is_dir():
            self.skipTest("shim sources not present")
        sys.path.insert(0, str(shim))
        try:
            with mock.patch("warnings.warn"):
                import dsh_kb  # noqa: F401
                from dsh_kb.cli import main  # noqa: F401
            self.assertEqual(dsh_kb.__version__, __import__("scheda").__version__)
        finally:
            sys.path.remove(str(shim))
            for name in [m for m in sys.modules if m == "dsh_kb" or m.startswith("dsh_kb.")]:
                del sys.modules[name]


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
