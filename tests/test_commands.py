# SPDX-License-Identifier: AGPL-3.0-or-later
"""`commands/` 包的契约。

把 1800 行的 `cli.py` 拆成包，只有在**接缝**没动的前提下才是纯搬家。这里钉住四件
事，任何一件松动都说明拆分改了行为而不是只搬了代码：

1. 注册表覆盖解析器里的每一个子命令，并且两边指向**同一个函数对象**（不是复制
   出来的同名函数 —— 那种"拆完有两个 cmd_doctor"的树，测试永远测不到线上那份）；
2. 命令模块不 import `cli`：依赖箭头只能 `cli -> commands`，反向就是环；
3. `scheda.cli` 仍是那条猴子补丁接缝（`mock.patch("scheda.cli.open_configured_source")`
   在 test_cli 里用着），命令模块必须让补丁继续生效；
4. `cli.py` 只留装配代码 —— 行数上限是这次拆分的**验收指标**，写进测试才拦得住
   下一个人往里加第二个 `cmd_push`。
"""
from __future__ import annotations

import argparse
import ast
import sys
import unittest
from pathlib import Path
from unittest import mock

SRC = Path(__file__).resolve().parents[1] / "src" / "scheda"
COMMANDS = SRC / "commands"

sys.path.insert(0, str(SRC.parent))

from scheda import cli  # noqa: E402
from scheda.commands import REGISTRY  # noqa: E402
from scheda.commands import common, write  # noqa: E402


def _wired(parser: argparse.ArgumentParser, prefix: str = "") -> list[tuple[str, object]]:
    """Every ``set_defaults(func=...)`` in the parser tree, as (qualified name, func)."""
    found: list[tuple[str, object]] = []
    for action in parser._actions:
        if not isinstance(action, argparse._SubParsersAction):
            continue
        for name, child in action.choices.items():
            qualified = f"{prefix} {name}".strip()
            found.append((qualified, child._defaults.get("func")))
            found.extend(_wired(child, qualified))
    return found


class TestRegistryMatchesParser(unittest.TestCase):
    def setUp(self):
        self.wired = [(name, func) for name, func in _wired(cli.build_parser()) if func]

    def test_every_command_is_registered(self):
        self.assertEqual(len(self.wired), 18, "命令表少了一条或重复注册")
        missing = [name for name, func in self.wired if func not in REGISTRY.values()]
        self.assertEqual(missing, [], "解析器用了一个没注册的函数：拆分后会出现两份实现")

    def test_registry_has_no_orphans(self):
        wired = {func for _, func in self.wired}
        orphans = sorted(name for name, func in REGISTRY.items() if func not in wired)
        self.assertEqual(orphans, [], "注册了却没有命令指向它，等于死代码")

    def test_skill_actions_are_registered_by_sub_action(self):
        for action in ("search", "show", "stats"):
            self.assertIn(f"skills-{action}", REGISTRY)

    def test_every_registered_value_lives_in_the_package(self):
        for name, func in REGISTRY.items():
            module = getattr(func, "__module__", "")
            self.assertTrue(module.startswith("scheda.commands."),
                            f"{name} 仍定义在 {module}，没有搬进 commands/")
            self.assertTrue(func.__name__.startswith("cmd_"), f"{name} -> {func.__name__}")


class TestDependencyDirection(unittest.TestCase):
    """`commands/*` 只允许 import `common` 与底层模块，绝不 import `cli`。"""

    def _imported(self, path: Path) -> set[str]:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        names: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                names.add(node.module)
                if node.level and not node.module:
                    names.update(alias.name for alias in node.names)
            elif isinstance(node, ast.Import):
                names.update(alias.name for alias in node.names)
        return names

    def test_no_command_module_imports_the_cli(self):
        offenders = []
        for path in sorted(COMMANDS.glob("*.py")):
            for module in self._imported(path):
                tail = module.split(".")[-1]
                if tail == "cli" or module in ("..cli", "scheda.cli"):
                    offenders.append(f"{path.name} -> {module}")
        self.assertEqual(offenders, [], "cli -> commands 是单向箭头；反向 import 就是环")

    def test_command_modules_share_one_helper_module(self):
        for path in sorted(COMMANDS.glob("*.py")):
            if path.name in ("__init__.py", "common.py"):
                continue
            siblings = {m for m in self._imported(path) if m == "common"}
            self.assertTrue(siblings, f"{path.name} 没有从 common 取共享 helper")

    def test_every_module_carries_a_spdx_header(self):
        missing = [p.name for p in sorted(COMMANDS.glob("*.py"))
                   if not p.read_text(encoding="utf-8").startswith(
                       "# SPDX-License-Identifier: AGPL-3.0-or-later")]
        self.assertEqual(missing, [])


class TestPatchSeam(unittest.TestCase):
    """`scheda.cli.open_configured_source` 是 test_cli 依赖的补丁点，必须继续生效。"""

    def test_cli_still_exposes_the_seam(self):
        self.assertTrue(hasattr(cli, "open_configured_source"))

    def test_a_patch_on_the_cli_module_reaches_the_command(self):
        sentinel = object()
        with mock.patch("scheda.cli.open_configured_source", return_value=sentinel) as patched:
            got = write.open_configured_source("CFG")
        self.assertIs(got, sentinel, "命令模块用了自己那份引用，补丁就被绕过了")
        patched.assert_called_once_with("CFG")

    def test_the_seam_falls_back_when_the_cli_is_absent(self):
        module = sys.modules.pop("scheda.cli", None)
        try:
            self.assertTrue(callable(write.open_configured_source))
        finally:
            if module is not None:
                sys.modules["scheda.cli"] = module
        # 兜底路径本身不该抛：说明名（cli_seam）解析不到时用的是真实实现。
        self.assertTrue(callable(common.cli_seam("open_configured_source", write._open_configured_source)))


class TestCliStaysAssemblyOnly(unittest.TestCase):
    MAX_LINES = 400

    def test_cli_py_is_only_assembly(self):
        lines = cli.__file__ and Path(cli.__file__).read_text(encoding="utf-8").splitlines()
        self.assertLessEqual(
            len(lines), self.MAX_LINES,
            f"cli.py 又长回 {len(lines)} 行；命令实现应放在 commands/ 里")

    def test_cli_py_defines_no_command(self):
        tree = ast.parse(Path(cli.__file__).read_text(encoding="utf-8"))
        defined = [n.name for n in tree.body
                   if isinstance(n, ast.FunctionDef) and n.name.startswith("cmd_")]
        self.assertEqual(defined, [], "命令实现不该再定义回 cli.py")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
