"""`scheda init`：第一次接触的人走的那一步。

这组用例守的是「陌生人第一次运行」的四个承诺：

1. 一条命令就能得到一个可用的 home（目录 + config.json + 一页说明）；
2. 已经有配置时**不会**被静默覆盖（这是最容易造成事故的"友好"）；
3. `--json` 的形状稳定，脚本与安装器可以依赖它；
4. 机器上没有任何共享盘/凭据时也不能失败或改动别的目录。
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from scheda.cli import main  # noqa: E402

_ENV_KEYS = ("SCHEDA_HOME", "SCHEDA_SOURCE", "SCHEDA_LOCAL_ROOT",
             "SCHEDA_MOUNT", "SCHEDA_WEBDAV_ROOT", "SCHEDA_SMB_SHARE")


class TestInit(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="scheda-init-"))
        self.home = self.tmp / "home"
        self.bundle = self.tmp / "share"
        self.bundle.mkdir()
        self._env_backup = {key: os.environ.get(key) for key in _ENV_KEYS}
        for key in _ENV_KEYS:
            os.environ.pop(key, None)

    def tearDown(self):
        for key, value in self._env_backup.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        shutil.rmtree(self.tmp, ignore_errors=True)

    def run_cli(self, *argv: str) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                code = main(["--home", str(self.home), *argv])
            except SystemExit as exc:  # argparse --version etc.
                code = int(exc.code or 0)
        return code, out.getvalue(), err.getvalue()

    def test_creates_layout_config_and_readme(self):
        code, out, err = self.run_cli("init")
        self.assertEqual(code, 0, err)
        self.assertTrue((self.home / "config.json").is_file())
        for sub in ("mirror", "index", "logs"):
            self.assertTrue((self.home / sub).is_dir(), sub)
        readme = self.home / "README.md"
        self.assertTrue(readme.is_file())
        self.assertIn("config.json", readme.read_text(encoding="utf-8"))
        # 说明里必须出现下一步，否则它只是一份自述
        self.assertIn("scheda sync", out)

    def test_refuses_to_overwrite_an_existing_config(self):
        self.assertEqual(self.run_cli("init")[0], 0)
        config = self.home / "config.json"
        config.write_text('{"source": "webdav", "webdav_root": "https://x/y"}\n',
                          encoding="utf-8")
        before = config.read_text(encoding="utf-8")

        code, _, err = self.run_cli("init")
        self.assertEqual(code, 3)
        self.assertIn("already exists", err)
        self.assertEqual(config.read_text(encoding="utf-8"), before,
                         "an existing config must not be rewritten by a plain init")

    def test_force_rewrites(self):
        self.assertEqual(self.run_cli("init")[0], 0)
        config = self.home / "config.json"
        config.write_text('{"source": "webdav"}\n', encoding="utf-8")

        code, _, err = self.run_cli("init", "--force", "--local-root", str(self.bundle))
        self.assertEqual(code, 0, err)
        payload = json.loads(config.read_text(encoding="utf-8"))
        self.assertEqual(payload["local_root"], str(self.bundle))
        self.assertNotIn("webdav", json.dumps(payload))

    def test_json_shape_is_stable(self):
        code, out, err = self.run_cli("init", "--json", "--local-root", str(self.bundle))
        self.assertEqual(code, 0, err)
        payload = json.loads(out)
        for key in ("home", "config", "mirror", "index", "logs", "source"):
            self.assertIn(key, payload, key)
        self.assertEqual(payload["source"]["kind"], "local")
        self.assertTrue(payload["source"]["available"])
        self.assertEqual(Path(payload["config"]).name, "config.json")

    def test_local_root_lands_in_config_and_source_resolves(self):
        self.assertEqual(self.run_cli("init", "--local-root", str(self.bundle))[0], 0)
        payload = json.loads((self.home / "config.json").read_text(encoding="utf-8"))
        self.assertEqual(payload, {"local_root": str(self.bundle)})

        # 配置写完之后，后续命令必须能接着跑（同一个 home，不带任何 flag）
        code, out, err = self.run_cli("status", "--json")
        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(out)["source"]["kind"], "local")

    def test_safe_on_a_machine_with_no_source(self):
        code, out, err = self.run_cli("init")
        self.assertEqual(code, 0, err)
        self.assertIn("not resolved yet", out)
        payload = json.loads((self.home / "config.json").read_text(encoding="utf-8"))
        self.assertEqual(payload, {"source": "auto"})

    def test_init_never_touches_the_bundle(self):
        (self.bundle / "keep.md").write_text("# keep\n", encoding="utf-8")
        self.assertEqual(self.run_cli("init", "--local-root", str(self.bundle))[0], 0)
        self.assertEqual(sorted(p.name for p in self.bundle.iterdir()), ["keep.md"])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
