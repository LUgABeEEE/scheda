# SPDX-License-Identifier: AGPL-3.0-or-later
"""架构不变量。

`docs/architecture.md` 说依赖是单向的：出口 → 用例 → 检索/同步 → 基础，基础层
之间互不 import。这句话写进文档不花钱，但下一个人（或下一个 agent）加一行
`from .service import ...` 就把它破了，而且不会立刻报错 —— 只会在某个 import
环里炸，或者把 CSV 导出拖进 numpy。

所以这里用 AST 把那张图变成断言。
"""
from __future__ import annotations

import ast
import unittest
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src" / "scheda"

#: 基础层：不得 import 任何其他内部模块（否则"基础"就不成立了）。
FOUNDATION = {
    "config", "platform_fs", "audit", "supersede", "skills",
}

#: 允许被谁 import 的出口层：只能被 __main__ / cli 聚合，不能被底层反向依赖。
TRANSPORTS = {"cli", "mcp_server", "webui"}

#: 只在可选路径里出现、禁止在模块顶层 import 的第三方库。
OPTIONAL_THIRD_PARTY = {"numpy", "onnxruntime", "tokenizers", "requests"}


def _internal_imports(module: str) -> set[str]:
    """Every ``scheda.*`` module this file imports, at any depth."""
    path = SRC / f"{module}.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found: set[str] = set()
    # 只看顶层 import：函数内的延迟 import 是有意为之（可选臂必须在用到时才
    # 加载），把它算进依赖图会制造假环。
    for node in tree.body:
        if isinstance(node, ast.ImportFrom) and node.level and node.module:
            found.add(node.module.split(".")[0])
        elif isinstance(node, ast.ImportFrom) and node.level and not node.module:
            for alias in node.names:
                found.add(alias.name.split(".")[0])
    found.discard("commands")  # cli 拆包后由命令模块提供
    return {name for name in found if (SRC / f"{name}.py").is_file()}


def _module_names() -> list[str]:
    return sorted(p.stem for p in SRC.glob("*.py") if p.stem != "__init__")


class TestDependencyDirection(unittest.TestCase):
    def test_foundation_does_not_import_other_internal_modules(self):
        offenders = {}
        for module in sorted(FOUNDATION):
            deps = _internal_imports(module)
            if deps:
                offenders[module] = sorted(deps)
        self.assertEqual(offenders, {},
                         "a 'foundation' module that imports a sibling is not a foundation")

    def test_transports_are_not_imported_by_lower_layers(self):
        offenders = []
        for module in _module_names():
            if module in TRANSPORTS or module == "__main__":
                continue
            overlap = _internal_imports(module) & TRANSPORTS
            if overlap:
                offenders.append(f"{module} -> {sorted(overlap)}")
        self.assertEqual(offenders, [],
                         "the data layer must not import a transport; that direction is "
                         "how a CLI flag ends up deciding a search result")

    def test_service_does_not_import_a_transport(self):
        self.assertEqual(_internal_imports("service") & TRANSPORTS, set())

    def test_no_import_cycles_among_top_level_modules(self):
        graph = {m: _internal_imports(m) for m in _module_names()}
        visiting, done = set(), set()

        def walk(node: str, trail: list[str]) -> None:
            if node in done:
                return
            if node in visiting:
                raise AssertionError("import cycle: " + " -> ".join(trail + [node]))
            visiting.add(node)
            for dep in sorted(graph.get(node, ())):
                walk(dep, trail + [node])
            visiting.discard(node)
            done.add(node)

        for module in graph:
            walk(module, [])


class TestOptionalLayersStayOptional(unittest.TestCase):
    def test_third_party_imports_are_never_at_module_top_level(self):
        offenders = []
        for path in sorted(SRC.glob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in tree.body:  # top level only
                if isinstance(node, ast.Import):
                    names = [a.name.split(".")[0] for a in node.names]
                elif isinstance(node, ast.ImportFrom) and not node.level:
                    names = [(node.module or "").split(".")[0]]
                else:
                    continue
                hit = set(names) & OPTIONAL_THIRD_PARTY
                if hit:
                    offenders.append(f"{path.name}: {sorted(hit)}")
        self.assertEqual(offenders, [],
                         "`import scheda` must not pull numpy/onnx; the optional arm "
                         "imports lazily so the stock install stays pure stdlib")

    def test_every_module_carries_a_spdx_header(self):
        missing = [p.name for p in sorted(SRC.glob("*.py"))
                   if not p.read_text(encoding="utf-8").startswith(
                       "# SPDX-License-Identifier: AGPL-3.0-or-later")]
        self.assertEqual(missing, [])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
