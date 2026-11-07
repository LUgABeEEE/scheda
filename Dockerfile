# syntax=docker/dockerfile:1
# =============================================================================
# scheda — container image (AGPL-3.0-or-later, see LICENSE)
#
# What this image is: the `scheda` console script (pyproject.toml
# `[project.scripts]`), which covers the whole product surface — mirror sync,
# SQLite FTS5 index build, search, and the MCP stdio server (`scheda mcp`).
#
# Three facts that shape every line below, all taken from the code:
#   1. `SCHEDA_HOME` is *state only* — mirror/, index/kb.sqlite, state/manifest,
#      logs/, config.json (src/scheda/config.py `Config.mirror_dir`,
#      `index_db`, `manifest_path`, `log_path`).  The code checkout is separate
#      on purpose, so it is never the volume.
#   2. The source bundle is read (never written) by `refresh`; it is selected
#      with SCHEDA_SOURCE / SCHEDA_LOCAL_ROOT / SCHEDA_MOUNT /
#      SCHEDA_WEBDAV_ROOT.  Mount it read-only.
#   3. The long-running mode is `mcp`, which reads newline-delimited JSON-RPC
#      from stdin until EOF (src/scheda/mcp_server.py `serve`).  A container
#      that must stay up therefore needs `-i` / `stdin_open: true`.
# =============================================================================
FROM python:3.12-slim

# --- image metadata ---------------------------------------------------------
# Keep the version in sync with src/scheda/__init__.py (`__version__`); it is
# a label only, nothing at runtime reads it.
ARG SCHEDA_VERSION=0.8.0
# Set to 1 to add `smbclient`, which is what SCHEDA_SOURCE=smb needs
# (docs/deployment.md §9.6).  Off by default: ~15 MB for a fallback path only.
ARG WITH_SMBCLIENT=0

LABEL org.opencontainers.image.title="scheda" \
      org.opencontainers.image.description="Personal Markdown knowledge base with a local mirror and SQLite FTS5 index, queried by AI agents over MCP stdio." \
      org.opencontainers.image.licenses="AGPL-3.0-or-later" \
      org.opencontainers.image.source="https://github.com/LUgABeEEE/scheda" \
      org.opencontainers.image.version="${SCHEDA_VERSION}"

# --- runtime environment ----------------------------------------------------
# PYTHONUNBUFFERED: logs from the MCP/sync path must reach `docker logs` at once.
# SCHEDA_HOME: the state root the CLI defaults to (config.py `load_config`).
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    SCHEDA_HOME=/data

# --- unprivileged account + the only OS dependency --------------------------
# uid/gid 1000 matches the first human user on a normal Linux host, so a NAS
# mount owned by that user stays readable without chown on this side.
# ca-certificates is required for the HTTPS/WebDAV source (docs §8).  `smbclient`
# is optional and only for SCHEDA_SOURCE=smb.
RUN useradd -m -u 1000 -s /usr/sbin/nologin kb \
 && apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates \
 && if [ "$WITH_SMBCLIENT" = "1" ]; then \
        apt-get install -y --no-install-recommends smbclient; \
    fi \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# --- layer 1: dependency metadata -------------------------------------------
# The project has exactly one dependency (`handrolled`, pure Python, no
# extensions — pyproject.toml `dependencies`).  Installing it in its own layer
# means editing src/** does not re-run it.  Bump this pin together with
# pyproject.toml; `pip install .` below would upgrade it either way, this layer
# only makes the download cacheable.
COPY pyproject.toml README.md LICENSE ./
RUN pip install --no-cache-dir "handrolled>=0.1.1"

# --- layer 2: the package ----------------------------------------------------
# No `-e`: the image carries an installed distribution, not a bind-mounted
# checkout.  There is no compiled extension, so nothing needs a build toolchain.
COPY src ./src
RUN pip install --no-cache-dir .

# Guard: the index is a `using fts5(...)` virtual table (src/scheda/index.py),
# so an interpreter without FTS5 would install cleanly and then fail on the
# first `index`.  Fail the build instead of failing in production.
RUN python -c "import sqlite3; \
c = sqlite3.connect(':memory:'); \
c.execute('create virtual table probe using fts5(body)'); \
print('sqlite', sqlite3.sqlite_version, '- fts5 ok')"

# --- state volume ------------------------------------------------------------
# Created before VOLUME so the anonymous volume inherits the ownership; a named
# volume mounted here is then usable by `kb` without a chown on first start.
RUN mkdir -p /data && chown -R kb:kb /data
VOLUME ["/data"]

USER kb

# `scheda --home /data status --json` is a real, side-effect-free command
# (cli.py `cmd_status`): it resolves config, probes the source, and opens the
# index read-only.  It exits 0 even when nothing has been synced yet, so a fresh
# container is healthy and a broken CLI/state directory is not.
HEALTHCHECK --interval=30s --timeout=10s --start-period=20s --retries=3 \
    CMD scheda --home "${SCHEDA_HOME}" status --json > /dev/null || exit 1

ENTRYPOINT ["scheda"]

# A bare `docker run scheda` should tell you what to type next, not hang on a
# stdio protocol nobody is speaking.  Real deployments override this with
# `mcp` (agent host), `refresh --quiet` (scheduler) or `doctor`.
CMD ["--help"]
