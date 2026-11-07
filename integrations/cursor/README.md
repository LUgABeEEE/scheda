# Cursor

1. Copy `rules.mdc` to `.cursor/rules/scheda.mdc` in your project (or
   `~/.cursor/rules/` for all projects).
2. Add the MCP server — `~/.cursor/mcp.json`:

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

3. Reload the window; the server appears under Settings → MCP.
