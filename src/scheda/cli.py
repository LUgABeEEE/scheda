# SPDX-License-Identifier: AGPL-3.0-or-later
"""Command line interface: ``scheda <command>``.

Commands are grouped by whether they touch the network:

* **offline** — ``status``, ``index``, ``search``, ``read``, ``rag-check``,
  ``skills``: they only ever look at the local mirror, index and vector store,
  so they keep working with the share unreachable.  This is the property that
  makes the tool usable as a model tool.
* **network** — ``sync``, ``push``, ``doctor``: they talk to the share.
  ``doctor`` is diagnostic but **not strictly read-only**: it writes and deletes
  two probe files (one in ``home``, one under ``_incoming/`` on the source) to
  answer "is this writable?" — see :func:`cmd_doctor`.
* **read-only network** — ``diff``: it reads the source listing to report what
  changed, what is missing locally and what is drifting, but it never writes to
  the source, the mirror or the index.

Every command supports ``--json`` where a machine-readable result makes sense
(the MCP server and the agent both consume it).  ``scheda <command> --help``
lists the flags of each command; the README carries the same table.

Command overview (the full flag list lives in ``--help``)::

    status
    doctor
    diff        [--incoming-days DAYS] [--json]
    rag-check   [--prune] [--prune-chunks] [--force]
    supersede-check [--json] [--proposal FILE]
    sync        [--prune] [--full] [--dry-run]
    index
    refresh     [--prune] [--full]
    search      <query> [-n N] [--zone Z] [--snippet N] [--refresh] [--no-auto-refresh]
    read        <path> [--start N] [--lines N]
    skills      search|show|stats
    push        [files…] [--force] [--expect SHA256] [--log [N]] [--json]
    import      --from PATH [--format NAME] --prefix PREFIX [--zone Z] [--write]
    mcp
"""
from __future__ import annotations

import argparse
import sys

from . import __version__
from .sources import SourceError

# The implementations live in ``commands/``; the CLI is the only place that
# knows how to spell them.  Import order is one-way: cli -> commands, never
# the reverse (``tests/test_architecture.py`` enforces the direction).
# ``REGISTRY`` is re-exported: hosts and tests address commands from either
# ``scheda.cli`` or ``scheda.commands``; there is exactly one table.
from .commands import REGISTRY, register  # noqa: F401
from .commands.doctor import cmd_doctor
from .commands.init import cmd_init
from .commands.lifecycle import cmd_diff
from .commands.mcp import cmd_mcp
from .commands.query import (
    cmd_read,
    cmd_search,
    cmd_skills_search,
    cmd_skills_show,
    cmd_skills_stats,
)
from .commands.refresh import cmd_refresh
from .commands.status import cmd_status
from .commands.sync import cmd_index, cmd_sync
from .commands.tools import cmd_rag_check, cmd_supersede_check
from .commands.web import cmd_web
from .commands.write import cmd_import, cmd_push

# Re-exported on purpose: ``mock.patch("scheda.cli.open_configured_source")`` is
# the documented seam for the write path, and ``commands/*`` resolves it through
# ``commands.common.cli_seam`` so a patch installed here still takes effect.
from .service import open_configured_source  # noqa: F401

# --------------------------------------------------------------------------- #
# registry
# --------------------------------------------------------------------------- #
# Every command is registered by the name it is typed under, so an agent host
# (or a test) can address one without importing its module.  ``skills`` carries
# three entry points; they are registered under their sub-action name.
register("init", cmd_init)
register("status", cmd_status)
register("doctor", cmd_doctor)
register("diff", cmd_diff)
register("rag-check", cmd_rag_check)
register("supersede-check", cmd_supersede_check)
register("import", cmd_import)
register("sync", cmd_sync)
register("index", cmd_index)
register("mcp", cmd_mcp)
register("web", cmd_web)
register("refresh", cmd_refresh)
register("search", cmd_search)
register("read", cmd_read)
register("push", cmd_push)
register("skills-search", cmd_skills_search)
register("skills-show", cmd_skills_show)
register("skills-stats", cmd_skills_stats)


# --------------------------------------------------------------------------- #
# argument parsing
# --------------------------------------------------------------------------- #
def _add_common_flags(parser: argparse.ArgumentParser, *, suppress: bool) -> None:
    """Attach the flags shared by every command.

    They are declared on both the root parser and every sub-parser so that
    ``scheda --json search x`` and ``scheda search x --json`` both work.  On the
    sub-parsers the default is ``SUPPRESS`` so an omitted flag does not overwrite
    the value the root parser already stored.
    """
    default = argparse.SUPPRESS if suppress else False
    parser.add_argument("--home", default=argparse.SUPPRESS if suppress else None,
                        help="state directory (mirror, index, manifest, logs, config)")
    parser.add_argument("--json", action="store_true", default=default,
                        help="machine-readable output")
    parser.add_argument("--quiet", action="store_true", default=default,
                        help="suppress progress on stderr")
    parser.add_argument("--source", choices=["auto", "local", "mount", "smb", "webdav"],
                        default=argparse.SUPPRESS if suppress else None,
                        help="override the read source")
    for name, help_text in (
        ("--mount", "path of a mounted share (Samba/CIFS, NFS, or a local directory)"),
        ("--local-root", "path of a plain local bundle"),
        ("--smb-share", "Samba share reached with smbclient, e.g. //host/share (no mount needed)"),
        ("--webdav-root", "base URL of a WebDAV collection"),
    ):
        parser.add_argument(name, default=argparse.SUPPRESS if suppress else None, help=help_text)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="scheda",
        description="A personal Markdown knowledge base with a local mirror and index, "
                    "built to be queried cheaply by AI agents.",
    )
    parser.add_argument("--version", action="version", version=f"scheda {__version__}")
    _add_common_flags(parser, suppress=False)
    sub = parser.add_subparsers(dest="command", required=True)

    def command(name: str, help_text: str) -> argparse.ArgumentParser:
        child = sub.add_parser(name, help=help_text, parents=[])
        _add_common_flags(child, suppress=True)
        return child

    p = command("init", "create the state directory and a config skeleton")
    p.add_argument("--force", action="store_true",
                   help="rewrite an existing config.json (default: refuse)")
    p.set_defaults(func=cmd_init)

    command("status", "show configuration, mirror and index state").set_defaults(func=cmd_status)
    command("doctor", "probe the environment and report problems").set_defaults(func=cmd_doctor)

    p = command("diff", "read-only: remote changes, local mirror gaps, drift and backlog")
    p.add_argument("--incoming-days", type=float, default=7.0, metavar="DAYS",
                   help="pending _incoming proposals older than this count as backlog (default 7)")
    p.set_defaults(func=cmd_diff)

    p = command("rag-check",
                "vector store <-> index consistency, page + chunk level "
                "(report; --prune deletes orphan pages)")
    p.add_argument("--prune", action="store_true",
                   help="really delete orphan vector rows (default: report only, dry-run)")
    p.add_argument("--prune-chunks", action="store_true",
                   help="really delete vector rows whose chunk is no longer in the index "
                        "(re-chunking residue; default: report only, dry-run)")
    p.add_argument("--force", action="store_true",
                   help="with --prune/--prune-chunks: override the bulk-delete and empty-index guards "
                        "(the ownership guard still needs rag.allow_external_vectors_db=true)")
    p.set_defaults(func=cmd_rag_check)

    p = command("supersede-check",
                "supersession health check (spec A1-A5: reciprocal links, no cycles, "
                "no dangling targets, single successor); --proposal runs the R1 gate on a draft")
    p.add_argument("--proposal", metavar="FILE", default=None,
                   help="run the proposal gate (R1) on this draft: a title collision with a live "
                        "page and no declaration is reported as must_declare")
    p.set_defaults(func=cmd_supersede_check)

    from handrolled.importers import names as _adapter_names

    p = command("import", "import records from another tool into the bundle "
                          "(dry-run unless --write)")
    p.add_argument("--from", dest="source_path", required=True, metavar="PATH",
                   help="the export to read (a file or a directory, depending on --format)")
    p.add_argument("--format", default="records", choices=_adapter_names(), metavar="NAME",
                   help="input format: " + ", ".join(_adapter_names()))
    p.add_argument("--prefix", required=True, metavar="PREFIX",
                   help="directory inside the bundle, e.g. memory or notes/imported")
    p.add_argument("--zone", default="z1-restricted", metavar="ZONE",
                   help="privacy zone to write into (default z1-restricted)")
    p.add_argument("--group-by", default="first-tag", metavar="KEY",
                   help="first-tag (default) | none | field:<record attribute>")
    p.add_argument("--title-prefix", default="", metavar="TEXT",
                   help="prefix for the generated page titles")
    p.add_argument("--wikilinks", choices=("strip", "keep"), default="strip",
                   help="reduce [[wikilinks]] to their label (default) or keep them verbatim")
    p.add_argument("--allow-public", action="store_true",
                   help="permit writing into a z0 zone (refused by default)")
    p.add_argument("--max-page-bytes", type=int, default=2_000_000, metavar="N",
                   help="refuse to render a page larger than this (default 2000000)")
    p.add_argument("--as-of", default="", metavar="ISO8601",
                   help="record this timestamp in the pages' generated.at; omit it (the "
                        "default) and the page carries no timestamp, which keeps re-imports "
                        "byte-identical")
    p.add_argument("--write", action="store_true",
                   help="actually write; without it the command only reports")
    p.set_defaults(func=cmd_import)

    p = command("sync", "refresh the mirror from the source")
    p.add_argument("--prune", action="store_true", help="delete local files that vanished remotely")
    p.add_argument("--full", action="store_true",
                   help="ignore the manifest and re-fetch everything (see the mtime caveat in the docs)")
    p.add_argument("--dry-run", action="store_true", help="list what would change")
    p.set_defaults(func=cmd_sync)

    p = command("index", "bring the local full-text index up to date "
              "(incremental unless --full)")
    p.add_argument("--full", action="store_true",
                   help="rebuild every page instead of applying the delta")
    p.set_defaults(func=cmd_index)

    command("mcp", "run the MCP server on stdio (for agent hosts)").set_defaults(func=cmd_mcp)

    p = command("web", "read-only web UI: status, search and read (localhost by default)")
    p.add_argument("--bind", default="127.0.0.1", metavar="HOST",
                   help="interface to listen on (default 127.0.0.1; there is no auth)")
    p.add_argument("--port", type=int, default=8765, metavar="PORT",
                   help="port to listen on (default 8765)")
    p.add_argument("--no-rewrite", action="store_true",
                   help="skip the optional local-model query rewrite "
                        "(a page must not hang for 12 s)")
    p.set_defaults(func=cmd_web)

    p = command("refresh", "sync + index (what a scheduler should call)")
    p.add_argument("--prune", action="store_true")
    p.add_argument("--full", action="store_true", help="re-fetch everything, ignoring the manifest")
    p.set_defaults(func=cmd_refresh)

    p = command("search", "ranked full-text search over the mirror")
    p.add_argument("query")
    p.add_argument("-n", "--number", type=int, default=None, help="max hits (default 8)")
    p.add_argument("--zone", default="z01", help="z01 (default) | z0 | z1 | z2 | all")
    p.add_argument("--snippet", type=int, default=None, help="snippet width in characters")
    p.add_argument("--refresh", action="store_true", help="force a refresh before searching")
    p.add_argument("--no-auto-refresh", action="store_true", help="never auto-refresh")
    p.set_defaults(func=cmd_search)

    p = command("read", "read a line window from the mirror")
    p.add_argument("path")
    p.add_argument("--start", type=int, default=1)
    p.add_argument("--lines", type=int, default=80)
    p.set_defaults(func=cmd_read)

    p = command("push", "upload local _incoming/ proposals to the bundle")
    p.add_argument("files", nargs="*",
                   help="file names inside the local mirror's _incoming/ (default: all of them)")
    p.add_argument("--force", action="store_true",
                   help="overwrite a remote _incoming file whose content differs")
    p.add_argument("--expect", metavar="SHA256", default=None,
                   help="refuse unless the local file's sha256 matches this digest")
    p.add_argument("--log", nargs="?", type=int, const=10, default=None, metavar="N",
                   help="print the most recent N push receipts (default 10) and exit")
    p.add_argument("--allow-undeclared", action="store_true",
                   help="skip the supersede gate: push a proposal that collides with a current "
                        "page's title without declaring supersedes:/not_superseding:")
    p.set_defaults(func=cmd_push)

    # 资产区入口：z3-skills 不进主子索引，所以给它一条自己的查询路径，
    # 实现上转发库自带的 bin/find.py（检索实现只有一份，见 skills.py 的说明）。
    p = command("skills", "query the skill library living in the z3-skills asset zone")
    ssub = p.add_subparsers(dest="skills_action", required=True)

    sp = ssub.add_parser("search", help="ranked search over the skill library")
    sp.add_argument("query", nargs="+")
    sp.add_argument("-n", "--number", type=int, default=10, help="max hits (default 10)")
    sp.add_argument("--kind", default=None, help="filter by use (ops/frontend/research/…)")
    sp.add_argument("--verdict", default=None, help="filter by verdict (recommended/candidate/…)")
    sp.add_argument("--risky", action="store_true", help="only skills without a license")
    sp.add_argument("--json", action="store_true", help="machine-readable output")
    sp.set_defaults(func=cmd_skills_search)

    sp = ssub.add_parser("show", help="print the head of one skill")
    sp.add_argument("name")
    sp.add_argument("--lines", type=int, default=80)
    sp.set_defaults(func=cmd_skills_show)

    ssub.add_parser("stats", help="library size, verdicts and license risk").set_defaults(
        func=cmd_skills_stats)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except SourceError as exc:
        # Every source problem already reads as a sentence an operator can act on.
        print(f"scheda: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:  # pragma: no cover - interactive only
        print("scheda: interrupted", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
