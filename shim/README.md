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
