# scheda 部署手册（企业内网版）

本页只讲**怎么装起来并保持新鲜**。原理与配置项见仓库根 `README.md`、
`docs/deployment.md`（那里的 systemd/cron/Samba 细节与本文一致，本文不重复）。

一个事实先定下来：`scheda` 的状态目录（下称 `<KB_HOME>`，容器里是 `/data`）只放
**派生物**——`mirror/`（源的副本）、`index/kb.sqlite`（SQLite FTS5 索引）、
`state/manifest.json`、`logs/`、`config.json`。删掉它只损失一次 `refresh`。
**不要把它放在共享盘上**：每台机器各自的镜像与索引互不影响，共享只会互相踩。

---

## 1. 三种形态，怎么选

| 形态 | 适用 | 不适合 | 交付物 |
|---|---|---|---|
| **Docker / Compose** | 一台常开 Linux 主机跑多个服务；镜像统一分发；`SCHEDA_SOURCE=webdav` 或只读 NAS 挂载；要给一堆 agent 提供同一个 MCP 端点 | 宿主机不给容器挂 NAS（容器里没有 mount 权限，只能走 WebDAV/SMB） | `Dockerfile`、`docker-compose.yml`、`.dockerignore` |
| **systemd（timer）** | 已经有 Linux 主机装了 pip 包，刷新当系统服务管：开机自启、journal 归集、`ProtectSystem=strict` 加固、`Nice`/`IOSchedulingClass=idle` 让路 | Windows；或不想为一个小工具引入 systemd 依赖 | `deploy/systemd/*.service`、`*.timer` |
| **Windows 计划任务** | 办公笔记本/Win 服务器：知识库源在 UNC 路径或盘符上，agent 就跑在这台机器上 | 需要跨用户、无人登录常驻（用 `-LogonType S4U`，但盘符映射会失效） | `deploy/windows/register-refresh-task.ps1` |

MCP 传输是 **stdio**（agent 宿主拉起进程，用 JSON-RPC 说话），不是端口。
所以容器不发布端口；宿主自己 `docker run -i` 或直接跑 `scheda mcp`。

---

## 2. 五条命令内跑起来

### Docker（推荐）

```bash
# ① 构建（上下文里只有 pyproject.toml + README.md + LICENSE + src/）
docker build -t scheda:0.8.0 .

# ② 挂上只读源 + 命名卷，起 MCP 服务（stdin_open 是必须的：mcp 读到 EOF 就退出）
SCHEDA_LOCAL_ROOT=/mnt/nas/kb docker compose up -d

# ③ 立刻刷一次，看是否真的读到了源（非零 = 失败）
docker compose run --rm scheda refresh --quiet

# ④ 体检：源可达、镜像没混进该排除的路径、索引存在、home 可写
docker compose run --rm scheda doctor

# ⑤ 定时：交给宿主 cron（容器里不放 scheduler）
*/15 * * * * cd /opt/scheda && docker compose run --rm -T scheda refresh --quiet
```

> `docker compose run --rm scheda refresh --quiet` 就是调度钩子——一条命令、
> 非零即失败，`docs/deployment.md §2` 的口径。`compose` 文件里刻意没有第二个
> `refresh` 服务，避免出现两套并存的刷新路径。
>
> agent 宿主侧的用法（同一个卷，短生命周期容器）：
> `docker run -i --rm -v scheda_kb-home:/data scheda:0.8.0 mcp`

### systemd

```bash
sudo install -d -o kb -g kb -m 0700 /var/lib/scheda        # ① 状态目录
sudo cp deploy/systemd/scheda-refresh.{service,timer} /etc/systemd/system/  # ②
sudo sed -i 's#<KB_HOME>#/var/lib/scheda#g' /etc/systemd/system/scheda-refresh.service  # ③
sudo systemctl daemon-reload && sudo systemctl enable --now scheda-refresh.timer        # ④
systemctl list-timers scheda-refresh.timer && journalctl -u scheda-refresh -n 20        # ⑤
```

`<KB_HOME>` 出现在 `Environment=SCHEDA_HOME` 与 `ReadWritePaths=` **两处**，必须一起换；
只换一处会让 `ProtectSystem=strict` 把刷新挡成只读而失败。

### Windows 计划任务

```powershell
# ① 注册（-Home 是必填的状态目录；-PythonExe 指能 import scheda 的解释器）
.\deploy\windows\register-refresh-task.ps1 -PythonExe C:\Python312\python.exe -Home C:\Users\<you>\.scheda

# ② 立刻跑一次并看结果（0x0 = 成功）
Start-ScheduledTask -TaskName 'scheda refresh'
Get-ScheduledTaskInfo -TaskName 'scheda refresh' | Format-List LastRunTime, LastTaskResult, NextRunTime

# ③ 手工复现同一条命令，确认是任务环境的问题还是源的问题
& C:\Python312\python.exe -m scheda refresh --quiet --home C:\Users\<you>\.scheda

# ④ 卸载
Unregister-ScheduledTask -TaskName 'scheda refresh' -Confirm:$false
```

该脚本是**纯 ASCII**：PowerShell 5.1 会把无 BOM 的 UTF-8 当 ANSI 读，中文注释足以
让解析器崩掉，所以注释也全用英文。改动后用
`LC_ALL=C grep -nP '[^\x00-\x7F]' deploy/windows/register-refresh-task.ps1` 复查（无输出即合格）。

---

## 3. NAS 挂载：只读还是可写

判断依据只有一条：**这条路径上的操作是读还是写。**

| 操作 | 对源的要求 | 结论 |
|---|---|---|
| `refresh` / `sync` / `index` / `search` / `read` / `mcp` | 只读。`refresh` 只往 `<KB_HOME>` 里写 | **`:ro` 足够**，生产默认就该是 `ro` |
| `doctor` 的 `source writable (_incoming)` 探针 | 写一个探针文件再删 | 只读时该行是 `[~~]` 警告，**不改变退出码**，不算故障 |
| `push`（把提案写进 `<源>/_incoming/`） | 需要写 | 单独把 `_incoming/` 挂成 `rw`，**不要**把整份共享放开 |
| `import` | 只读源，写 `<KB_HOME>` | `:ro` 足够 |

所以 Compose 里默认写的是：

```yaml
volumes:
  - kb-home:/data
  # - /mnt/nas/kb:/kb:ro                 # 读：refresh 用这个
  # - /mnt/nas/kb/outbox:/kb/_incoming:rw # 只有 push 才需要，单独挂子目录
```

两个常见的反向情况，别被挂载选项骗了：

1. **挂载写 `rw`，服务端账号没写权限** —— 只能实测：`scheda doctor` 的
   `source writable (_incoming)` 会真写一个探针再删（`docs/deployment.md §9.2`）。
2. **容器里挂不了 cifs** —— 容器没有 mount 权限，也没有 `uid/gid` 映射时，
   改用 `SCHEDA_SOURCE=webdav`（走 HTTPS，镜像里已带 `ca-certificates`）或
   `SCHEDA_SOURCE=smb`（需要构建时 `--build-arg WITH_SMBCLIENT=1` 装 `smbclient`）。

---

## 4. 配置项速查（名字以 `src/scheda/config.py` 为准）

| 变量 | 作用 | 建议值 |
|---|---|---|
| `SCHEDA_HOME` | 状态根目录 | 容器 `/data`；系统 `/var/lib/scheda`；Windows `C:\Users\<you>\.scheda` |
| `SCHEDA_SOURCE` | `auto`/`local`/`mount`/`smb`/`webdav` | `auto` 会按 mount → local → smb → webdav 依次探测 |
| `SCHEDA_LOCAL_ROOT` | 普通目录作源 | 容器里 `/kb` |
| `SCHEDA_MOUNT` | 已挂载的共享 | `/mnt/nas/kb` |
| `SCHEDA_SMB_SHARE` | 不挂载直连 Samba（需 `smbclient`） | `//192.0.2.10/kb` |
| `SCHEDA_WEBDAV_ROOT` | WebDAV 兜底源 | `https://nas.example.com:5000/webdav/kb` |
| `SCHEDA_STALE_MINUTES` | 检索前自动增量刷新的阈值 | `15`（默认） |

`SCHEDA_WEBDAV_USER` / `_PASSWORD` / `_CREDENTIALS` / `_CERT` / `_KEY` / `_CA`
见 `docs/deployment.md §8`。口令不要写进 compose 文件或 unit——用宿主
`systemd` 的 `EnvironmentFile=`（`600` 权限）或 compose 的 `secrets:`。

---

## 5. 交付后自检清单

```bash
scheda status --json     # 源解析成什么、镜像/索引多少页、多旧
scheda doctor            # [ok]/[~~]/[!!] 三档；[!!] 才让退出码非零
scheda refresh --quiet   # 调度器调的那一条；非零即失败
```

- 容器：`docker inspect --format '{{.State.Health.Status}}' scheda` 应为 `healthy`
  （健康检查就是 `scheda --home /data status --json`）。
- systemd：`systemctl list-timers scheda-refresh.timer` 能看到下次触发时间；
  `journalctl -u scheda-refresh` 里 `--quiet` 期只有失败才有输出，**空日志 = 正常**。
- Windows：`Get-ScheduledTaskInfo` 的 `LastTaskResult` 为 `0`。
- 索引是 FTS5 虚表；解释器没编进 FTS5 会在 `index` 阶段报错，镜像构建时已用
  `create virtual table probe using fts5(body)` 提前挡住这种情况。
