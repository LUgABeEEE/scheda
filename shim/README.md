# dsh-kb (compatibility shim)

**This package was renamed to [`scheda`](https://pypi.org/project/scheda/).**

`pip install dsh-kb` still works: it installs `scheda` and keeps
`import dsh_kb` — with every submodule (`dsh_kb.cli`, `dsh_kb.config`, …) mapped
to its `scheda` counterpart — so existing code does not break. New code should
import `scheda`.

The `dsh-kb` **command** is installed by `scheda` itself, so a systemd timer or
script calling `dsh-kb refresh` keeps working after the upgrade.

Nothing here is retroactive: releases ≤ 0.6.0 stay MIT, 0.8.0 was the first
dual-licensed (AGPL-3.0-or-later + commercial) release.

## What version does it report?

`import dsh_kb` and `python -m dsh_kb --version` print **`scheda`'s** version,
not the shim's: there is one implementation and the shim forwards to it. So
`pip show dsh-kb` can say `0.10.1` while the running code is `scheda 0.10.0` —
that is the intended behaviour, not a stale install. Look at `scheda` for what you
actually have.

## What still works

```bash
pip install dsh-kb            # installs scheda, keeps the old name importable
python -m dsh_kb --version    # old module entry point
dsh-kb refresh                # old console script (installed by scheda)
```

All three are exercised in `.github/workflows/publish-shim.yml` before the shim
is allowed to upload — a rename that breaks a host's `python -m dsh_kb mcp` line
would be worse than no rename at all.
