# DSH

DSH already has a real plugin for this: **`dsh-mnemon-source-kb`** — a
`dsh-mnemon` memory Source with a management page (health, freshness, doctor
checks, the six retrieval parameters), an in-page search box, and a read-only
vector-space map.

It is a separate package on purpose: it depends on the DSH plugin API, while the
core (`scheda`) depends on nothing but the standard library plus `handrolled`.

- Package: `dsh-mnemon-source-kb` (`peerDependencies: dsh-mnemon >= 0.5.34`)
- Command it shells out to: `scheda` (still accepts `dsh-kb`)

Simple alternative, no plugin needed — wire the MCP server directly:

```yaml
- id: mcp-kb
  name: '@deepseek-ai/dsh-mcp-client'
  config:
    serverName: kb
    transport: stdio
    command: scheda
    args: [mcp, --home, /srv/scheda, --source, auto, --mount, /mnt/nas/kb]
```
