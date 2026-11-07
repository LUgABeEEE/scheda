# Host integrations

One directory per host. **Most of these are configuration, not code**: `scheda mcp`
speaks standard MCP over stdio, so any MCP-capable client can use it. Only two
hosts need real glue:

| Host | What is in here | Kind |
|---|---|---|
| [dsh](dsh/) | points at the `dsh-kb` package (mnemon Source: management page, vector map, health) | code — separate package |
| [claude](claude/) | `SKILL.md` so the model knows *when* to consult the base | skill file |
| [cursor](cursor/) | `.cursor/rules` + `mcp.json` fragment | config |
| [cline](cline/) | MCP settings fragment | config |
| [codex](codex/) | `AGENTS.md` paragraph + MCP fragment | config |

**One rule for all of them**: do not copy search logic into the host. Point the
host at `scheda mcp` and tell the model when to call it. The retrieval pipeline,
the privacy zones and the audit trail live in one place — that is the whole point.

## The one thing every integration must say

> Call `search` only when you need past decisions, environment facts or history —
> not routinely. Expand a hit with `read` before quoting it: the snippet is 240
> characters, not the page.

That sentence is the difference between a knowledge base that saves tokens and one
that burns them.
