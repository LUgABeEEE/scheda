# Codex / other AGENTS.md-driven hosts

Add this paragraph to the project's `AGENTS.md` (or the equivalent instruction
file), and register the MCP server the way that host documents:

```markdown
## Local knowledge base

An MCP server named `kb` exposes `search`, `read`, `status`, `skills`.
Call `search` only when you need past decisions, environment facts or project
history — not routinely. Expand a hit with `read` before quoting it: the snippet
is 240 characters, not the page. Cite `path:line`.
```

```json
{
  "mcpServers": {
    "kb": { "command": "scheda", "args": ["mcp", "--home", "/home/you/.scheda"] }
  }
}
```

Hosts that only support shell commands can use the CLI instead of MCP:

```bash
scheda search "deploy" --json
scheda read path/to/page.md --lines 40 --json
```
