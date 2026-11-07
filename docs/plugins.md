# 插件：企业能力挂在哪里

`scheda` 的核心承诺是「一条 pip install、纯标准库、无守护进程」。企业要的东西（SSO、审计转发、按 zone 的目录级授权）**不进核心**，而是作为插件挂在三个固定钩子上——这样公开仓保持可审计，私有仓不必 fork。

## 三个钩子

| 协议 | 回答的问题 | 默认实现 |
|---|---|---|
| `AuthProvider.identify(context) -> str` | 这次调用是谁发起的 | 返回 `"local"` |
| `AuditSink.emit(record) -> None` | 审计记录送去哪里 | 只写 `<home>/logs/audit.jsonl` |
| `ZonePolicy.allow(rel, action) -> bool \| None` | 这个路径能不能被镜像/索引/查询 | `None` = 交回 `config.py` 的内置策略 |

`action` 是 `mirror` / `index` / `query` 三者之一。返回 `None` 表示"我不决定"，而不是"拒绝"——这很重要，否则任何插件都会意外接管全部路径。

## 注册

```toml
# 你的插件包的 pyproject.toml
[project.entry-points."scheda.plugins"]
acme-sso = "acme_scheda.plugin:Provider"
```

```python
# acme_scheda/plugin.py
class Provider:
    def identify(self, context: dict) -> str:
        return context.get("user") or "local"

    def emit(self, record: dict) -> None:
        ...  # 转发到 SIEM；不吞异常，scheda 会记一行并且继续

    def allow(self, rel: str, action: str) -> bool | None:
        return None
```

一个插件对象可以只实现其中一两个方法：`scheda` 用 `hasattr` 判定，不要求继承（`typing.Protocol` 是给类型检查器看的，不是给运行时看的）。

## 三条行为契约

1. **失败无害**：插件 import 或调用抛异常 → stderr 一行 `scheda: plugin <name> failed: …`，内置实现继续工作。一个坏插件不能让人搜不了自己的笔记。
2. **审计是转发，不是替代**：本地 `audit.jsonl` 永远写。企业 sink 挂掉不等于审计断档。
3. **默认零插件**：没装任何插件时，`load()` 返回三个 `None`，所有路径与不装插件前逐字节一致（`tests/test_plugins.py` 守着这条）。

## 排查

```bash
SCHEDA_PLUGINS=off scheda search "..."   # 完全跳过发现，用来确认问题是不是插件引起的
scheda status --json                      # 不含插件信息；插件是部署事实，不污染状态输出
```

## 为什么不把 SSO 放进核心

因为 AGPL 社区版的全部价值就是"可以自己审"。把登录、审计、权限做进核心，等于把企业最在意的那部分变成不可审计的黑盒；而把它们做成插件，企业拿到的是「同一套检索 + 一个他们能审的治理层」——这也是商业许可唯一站得住的位置。
