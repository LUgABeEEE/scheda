---
name: knowledge-base
description: Query the local scheda knowledge base for past decisions, environment facts, runbooks and project history. Use before deriving an answer from scratch, or when the user asks "how did we do X", "what did we decide about Y", "is there a write-up".
---

# Local knowledge base

You have a local Markdown knowledge base exposed over MCP as the server `kb`
(tools: `search`, `read`, `status`, `skills`).

## When to call `search`

- The user asks how something was done before, or why a decision was made.
- The task touches infrastructure, deployments, credentials *names*, or project
  history that may already be written down.
- You are about to say "I don't have information about that".

Do **not** call it per turn, and do not pre-load documents. One call when it is
actually needed is the whole design: a search returns ~2.4 KB regardless of how
large the base is.

## How to use the result

1. `search` returns `path`, `line`, `snippet` and a score. A snippet is 240
   characters — **never quote it as if it were the page**.
2. Expand with `read` (`path`, `start`, `lines`) before relying on it.
3. Cite the path and line in your answer. If the base contradicts itself, say so
   and show both.

## Zones

`zone=z01` (default) covers the public and restricted areas. `z2` is
sensitive and is **not** mirrored or indexed by default — if a search returns
nothing, `status` tells you whether the index is stale before you conclude the
topic is not covered.
