# Claude Code

Two ways to connect, pick one.

## 1. MCP only (recommended)

```bash
claude mcp add kb -- scheda mcp --home "$HOME/.scheda"
```

Or in `.mcp.json` at the project root:

```json
{
  "mcpServers": {
    "kb": {
      "command": "scheda",
      "args": ["mcp", "--home", "/home/you/.scheda"]
    }
  }
}
```

## 2. MCP + skill (better trigger behaviour)

Copy `SKILL.md` to `~/.claude/skills/knowledge-base/SKILL.md` (or
`.claude/skills/` inside the project). The skill tells the model *when* to
consult the base; without it, a model tends to either ignore the tool or call it
every turn.

Verify:

```bash
scheda status            # index present and fresh?
scheda search "deploy"   # a real hit?
```
