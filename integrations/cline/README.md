# Cline

Cline reads MCP servers from its settings panel. Add a stdio server:

| Field | Value |
|---|---|
| Name | `kb` |
| Command | `scheda` |
| Arguments | `mcp --home /home/you/.scheda` |

Equivalent JSON (Cline's `cline_mcp_settings.json`):

```json
{
  "mcpServers": {
    "kb": {
      "command": "scheda",
      "args": ["mcp", "--home", "/home/you/.scheda"],
      "disabled": false,
      "autoApprove": ["search", "read", "status"]
    }
  }
}
```

`autoApprove` is safe for these four tools: none of them writes anything.
