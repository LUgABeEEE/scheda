"""包装契约：一个依赖、无编译扩展、entry point、版本单一来源。

这些断言存在的理由很具体：这个项目的对外承诺是「装起来不疼」——一条
`pip install scheda`、没有编译扩展、没有守护进程。承诺一旦只写在 README 里，
下一次有人顺手加一个 `requests` 就没人拦得住。`docs/release.md` 把这张单子
当作发布门槛，所以它得能被机器检查。
"""
from __future__ import annotations

import sys
import tomllib
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SRC = REPO / "src"


class TestPackagingContract(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.data = tomllib.loads((REPO / "pyproject.toml").read_text(encoding="utf-8"))
        cls.project = cls.data["project"]

    def test_exactly_one_runtime_dependency(self):
        self.assertEqual(self.project["dependencies"], ["handrolled>=0.1.1"],
                         "the one-dependency promise is load-bearing; "
                         "a new runtime dependency needs a discussion, not a commit")

    def test_no_compiled_extensions_in_the_tree(self):
        stray = [str(p.relative_to(REPO)) for p in SRC.rglob("*")
                 if p.suffix in {".so", ".pyd", ".pyx", ".c", ".cpp"}]
        self.assertEqual(stray, [], "pure Python is part of the install promise")

    def test_console_script_is_wired(self):
        self.assertEqual(self.project["scripts"]["scheda"], "scheda.cli:main")

    def test_python_floor_matches_the_ci_matrix(self):
        self.assertEqual(self.project["requires-python"], ">=3.10")
        versions = {c.rsplit(" :: ", 1)[-1] for c in self.project["classifiers"]
                    if c.startswith("Programming Language :: Python :: 3.")}
        self.assertEqual(versions, {"3.10", "3.11", "3.12", "3.13"})

    def test_version_has_a_single_source(self):
        dynamic = self.data["tool"]["setuptools"]["dynamic"]
        self.assertEqual(dynamic["version"], {"attr": "scheda.__version__"})
        self.assertNotIn("version", self.project,
                         "a literal version in pyproject would drift from __init__")

    def test_installed_version_matches_the_source(self):
        sys.path.insert(0, str(SRC))
        import scheda
        self.assertRegex(scheda.__version__, r"^\d+\.\d+\.\d+$")

    def test_license_is_declared_and_the_file_exists(self):
        # Dual-licensed from 0.8.0 (docs/licensing.md). A bare classifier is not
        # enough (setuptools>=77 / PEP 639), and the LICENSE file must actually
        # say AGPL rather than still describing the MIT era.
        self.assertEqual(self.project["license"], "AGPL-3.0-or-later")
        self.assertIn("LICENSE", self.project["license-files"])
        licence = (REPO / "LICENSE").read_text(encoding="utf-8")
        self.assertIn("GNU AFFERO GENERAL PUBLIC LICENSE", licence)
        self.assertIn("Version 3, 19 November 2007", licence)

    def test_every_source_file_carries_its_spdx_identifier(self):
        missing = [p.name for p in sorted((SRC / "scheda").glob("*.py"))
                   if not p.read_text(encoding="utf-8").startswith(
                       "# SPDX-License-Identifier: AGPL-3.0-or-later")]
        self.assertEqual(missing, [], "SBOM tooling reads the SPDX header, not LICENSE")

    def test_typed_marker_ships(self):
        self.assertIn("py.typed", self.data["tool"]["setuptools"]["package-data"]["scheda"])

    def test_integrations_and_shim_are_in_the_tree(self):
        self.assertTrue((REPO / "integrations" / "README.md").is_file())
        for host in ("dsh", "claude", "cursor", "cline", "codex"):
            self.assertTrue((REPO / "integrations" / host).is_dir(), host)
        self.assertTrue((REPO / "shim" / "pyproject.toml").is_file(),
                        "the old package name needs a shim to install and forward to")

    def test_sdist_manifest_covers_the_deliverable(self):
        manifest = (REPO / "MANIFEST.in").read_text(encoding="utf-8")
        for entry in ("README.md", "README.zh.md", "CHANGELOG.md", "LICENSE",
                      "SECURITY.md", "COMMERCIAL.md", "recursive-include docs",
                      "recursive-include tests", "recursive-include integrations",
                      "recursive-include shim"):
            self.assertIn(entry, manifest, entry)

    def test_ci_matrix_covers_every_claimed_version(self):
        ci = (REPO / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
        for version in ("3.10", "3.11", "3.12", "3.13"):
            self.assertIn(f'"{version}"', ci, version)
        self.assertIn("windows-latest", ci,
                      "Windows is a claimed target; it needs a job, not a sentence")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
