# 命名：scheda（0.9.0 已执行）

> 状态：**已落地**（2026-11-07）。核心名 `scheda`，`dsh-kb` 转为「DSH 适配插件」。

## 1. 为什么叫 scheda

拉丁/意大利语 **scheda** —— 图书馆卡片目录里的一张卡：一条记录、可检索、可归档。比 `cairn`（路标）更贴这套东西的真实形态——它不是路标，是**目录**。

查重过程（都做过，不是拍脑袋）：

| 候选 | PyPI | .dev | .io | 结论 |
|---|---|---|---|---|
| cairn-kb | 404（当时查是空） | — | — | **你指出已被占，弃用** |
| folio / folio-kb | 200 | — | — | 已占 |
| waymark / lorekeep / concordance / trailhead / pinakes | 200 | — | — | 已占 |
| indicia | 404 | 占 | 占 | 可用，但无域名 |
| scheda | **404** | **free** | **free** | **采用** |

**教训**：单词级英文名在 PyPI 上基本被扫光了，可用的要么是带后缀的组合（`kbindex`），要么是拉丁/专业词汇（`scheda`）。

## 2. 命名体系

| 层 | 名字 | 说明 |
|---|---|---|
| 品牌 / 项目 | **scheda** | 卡片目录 |
| PyPI 包 | `scheda` | 已实测可用 |
| CLI | `scheda` | `scheda init` / `search` / `mcp` / `web` |
| Python 模块 | `scheda` | `import scheda` |
| 状态目录 | `~/.scheda` | 旧 `~/.dsh-kb` 继续被读取 |
| 环境变量 | `SCHEDA_*` | 旧 `DSH_KB_*` 作为回退 |
| 插件仓库/目录 | `integrations/<host>/` | `dsh` / `claude` / `cursor` / `cline` / `codex` |
| 插件发行包 | `dsh-kb`（DSH 专用）等 | 见 `integrations/README.md` |

**`dsh-kb` 不废弃**：它现在是「给 DSH 的适配插件」这个名字，正好符合「每个宿主一个插件」的结构。老的 `dsh-kb` PyPI 包 0.9.0 起是 shim（依赖 `scheda`，保留 `import dsh_kb`）。

## 3. 为什么插件大多是配置

MCP 宿主无关：同一个 `scheda mcp`（stdio JSON-RPC）任何客户端都能接走。所以：

| 宿主 | 需要什么 | 形态 |
|---|---|---|
| DSH | mnemon Source 插件（管理页、向量空间、健康检查） | 独立包 `dsh-mnemon-source-kb`（5332 行，真代码） |
| Claude Code | `SKILL.md` + `.mcp.json` | 文件 + 配置 |
| Cursor | `.cursor/rules/*.mdc` + `mcp.json` | 配置 |
| Cline | `cline_mcp_settings.json` | 配置 |
| Codex / 其他 | `AGENTS.md` 段落 + MCP 配置 | 配置 |

**统一原则**：`integrations/` 每个目录只做三件事——告诉模型「什么时候该查」、给配置、给一条自检命令。检索逻辑一行都不复制。

## 4. 已执行的兼容措施（都有测试）

| 措施 | 为什么必须 |
|---|---|
| `pyproject` 同时注册 `scheda` 与 `dsh-kb` 两个命令 | 定时任务/脚本里写的是 `dsh-kb refresh`，改名不该让它静默失效 |
| `SCHEDA_*` 优先、`DSH_KB_*` 回退 | 老部署的 systemd unit / compose 不改也能跑；否则会指向错误的 bundle |
| `~/.scheda` 不存在且 `~/.dsh-kb` 存在 → 用旧路径 | 升级不许看起来像「知识库空了」 |
| `shim/src/dsh_kb/` 把每个子模块映射到 `scheda.*` | `from dsh_kb.cli import main` 这类老路径必须还能用 |
| `tests/test_rename_compat.py`（10 用例） | 上面四条每一条都有断言，改名不会被下一次重构悄悄破坏 |

## 5. 还需要你手动做的

1. **建仓库**：`LUgABeEEE/scheda`（不需要同名组织，仓库名只在账号内唯一）。
2. **商标**：中国商标网 + USPTO 查 9 类（软件）/42 类（SaaS）的 `scheda`。意大利语里是常用词，软件类目通常没问题，但值得十分钟。
3. **域名**：`scheda.dev` / `scheda.io` 当时 free，注册前再确认一次（DNS 空闲 ≠ 可注册）。
4. **PyPI**：注册 `scheda` 项目 + 配 Trusted Publisher（owner/repo/workflow/env 同原方案）。
5. **旧包**：`dsh-kb` 的 0.9.0 用 `shim/` 构建并发布，README 已写好弃用说明。

## 6. 一句话

核心叫 **scheda**（目录卡），`dsh-kb` 降级为 DSH 适配层；兼容是四条硬措施 + 十个测试，不是一句「注意升级」。
