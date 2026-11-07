# 架构

> 面向接手的人。设计理由与实测数字在 `design.md`，部署形态在 `deployment.md`，许可边界在 `licensing.md`。

## 一页图

```
┌──────────────────────────── 宿主（agent / 人 / 浏览器）────────────────────────────┐
│  CLI                MCP 客户端（Claude/Cursor/Cline/Codex/DSH）        浏览器        │
└───┬──────────────────────────┬──────────────────────────────────────────┬──────────┘
    │ 子进程                    │ stdio JSON-RPC（4 个工具）                 │ HTTP（只读）
    ▼                          ▼                                          ▼
┌───────────────────── 出口层 ─────────────────────────────────────────────────────┐
│  cli.py (1800)          mcp_server.py (361)                    webui.py (848)     │
└──────────────────────────────────────┬───────────────────────────────────────────┘
                                       │ 三种出口调同一套用例，输出不会漂移
                                       ▼
┌──────────────────────────── 用例层 ──────────────────────────────────────────────┐
│  service.py (670)   search_payload / read_payload / status_payload / audit 接线    │
└───────┬───────────────────────┬────────────────────────┬─────────────────────────┘
        │                       │                        │
        ▼                       ▼                        ▼
┌── 检索 ──────────┐  ┌── 同步与源 ──────┐  ┌── 可选语义臂（默认关）──────────────┐
│ index.py (1100)  │  │ mirror.py (469)  │  │ ragvec.py (869)   expansion.py (527) │
│ SQLite FTS5      │  │ sources.py (597) │  │ image_embed.py (857)                 │
│ 分块 / 两段式     │  │ local·mount·smb  │  │ 向量 / 重排 / 图片 / LLM 改写         │
│                  │  │ ·webdav          │  │ 需要 numpy/onnx/远端，全部 opt-in     │
└──────────────────┘  └──────────────────┘  └──────────────────────────────────────┘
        │                       │                        │
        └───────────┬───────────┴────────────┬───────────┘
                    ▼                        ▼
        ┌── 基础层（无内部依赖）──────────────────────────────┐
        │ config.py (617)   分区策略 / 配置优先级 / home 布局   │
        │ platform_fs.py (405)  POSIX 委托 · Windows Win32     │
        │ audit.py (507)     审计 JSONL · 事件 JSONL · 轮转     │
        │ supersede.py (372) 取代关系 A1–A5 · 写入门           │
        │ skills.py (169)    z3-skills 资产区查询              │
        │ ragcheck.py (929)  向量库 ↔ 索引一致性               │
        └─────────────────────────────────────────────────────┘

外部：handrolled（纯标准库工具箱，MIT，独立仓库）—— 唯一运行依赖
```

**单向依赖**：出口 → 用例 → 检索/同步 → 基础。基础层之间互不 import（`config` 不 import 任何内部模块），所以任何一层都能单独测试。

## 二、数据流

```
共享盘上的 bundle（唯一真源，Markdown）
        │  ① sync：按远端清单 + 本地 manifest 做增量拉取，可中断、幂等
        ▼
   ~/.scheda/mirror/           可丢弃副本；冲突在这里不可能发生
        │  ② index：分块 → FTS5（content=''，文本只存一份，膨胀 1.9×）
        ▼
   ~/.scheda/index/kb.sqlite   可删除重建；绝不放网络盘（SQLite 锁语义）
        │  ③ search / read：先 AND 严格匹配，弱命中才松弛并丢泛化词
        ▼
   返回 path + line + 240 字符片段（≈2.4 KB，与库大小无关）
```

三条**不变量**，破一条这套设计就散：

1. **真源唯一、镜像派生** — 镜像永远不是权威，所以没有合并冲突；refresh 是 `(远端清单, manifest)` 的纯函数。
2. **索引在本机** — 网络文件系统的锁语义不可靠；索引可重建，所以盘慢只影响新鲜度，不影响可用性。
3. **隐私分区编译进代码** — `config.mirror_allows/index_allows` 决定拉不拉、索不索；z2 连 `walk()` 都不列举，不落盘、不进索引、默认查询不返回。

## 三、三种出口为什么不会互相漂移

`cli.py`、`mcp_server.py`、`webui.py` 都调 `service.py` 的同一组函数：

| 用例函数 | CLI | MCP | Web |
|---|---|---|---|
| `search_payload` / `search_with_refresh` | `search` | `search` | `/search` |
| `read_payload` | `read` | `read` | `/page` |
| `status_payload` | `status` | `status` | `/` 与 `/healthz` |

`transport` 参数（`cli` / `mcp` / `web`）只影响审计记录的标签，不影响检索行为。加一个出口 = 调这三个函数 + 写一个 transport 字符串，不需要复制检索逻辑。

## 四、写入面（谁可以改东西）

只有三处会写：

| 动作 | 写哪里 | 门 |
|---|---|---|
| `sync` / `refresh` / `index` | 本机 home | 无（派生数据，可重建） |
| `push` | 共享盘 `_incoming/` | 取代关系门（`supersede.py` 的 A1–A5） |
| `web` 的配置保存（在级联插件里） | `config.json` | 校验 → 备份 → tmp+fsync+rename |

**agent 永远不能直接写正本**：它只能往 `_incoming/` 提提案，转正由人或定时任务做。这是「同一主题十几条渐进状态互相矛盾」的解法。

## 五、可选语义臂（默认全关）

| 层 | 需要 | 默认 | 联网 |
|---|---|---|---|
| 向量臂 | numpy + 向量库 + embedding 端点 | 关 | 取决于端点 |
| cross-encoder 重排 | onnxruntime + ~280 MB 模型 | 关 | 否 |
| ONNX embedding | ~610 MB 模型 | 关 | 否 |
| `deep=true` 深排 | OpenAI 兼容端点 + key | 关 | **是**（页面文本会离开机器） |
| 查询改写 | 本地模型（如 ollama） | 关 | 否 |

默认路径 `import scheda` 不碰任何第三方代码，一次 search 与 0.1.0 逐字节一致——`tests/test_optional_off.py` 在剥离所有环境变量的子进程里守着这条。

## 六、仓库矩阵

| 仓库 | 角色 | 许可 |
|---|---|---|
| **scheda** | 核心（本仓库） | AGPL-3.0-or-later / 商业 |
| **handrolled** | 纯标准库依赖库（文本/WebDAV/挂载/导入层抽出） | MIT |
| **dsh-kb** | 兼容 shim + DSH 适配 | AGPL / 商业 |
| **dsh-mnemon-source-kb** | DSH 宿主插件（mnemon Source，真代码） | MIT |
| *(计划)* scheda-enterprise | 企业治理层：auth / audit-sink / zone-policy | 商业 |

## 七、当前缺口（架构层面，不是 bug）

1. **没有进程内扩展点**：`entry_points` 零命中。企业版要么 fork（必然漂移），要么先加 `auth_provider` / `audit_sink` / `zone_policy` 三个钩子。
2. **`cli.py` 是巨石**（1800 行，43 个顶层函数）：参数解析 + push 事务 + import 适配 + doctor 探针混在一起，可按职责无损切到 `commands/`。
3. **无增量索引**：每次全量重建（33 MB 约 3 s）。库再大一个量级需要按 mtime 增量。
4. **无多用户/鉴权**：`web` 默认只绑 `127.0.0.1` 就是承认这一点——它没有认证。
