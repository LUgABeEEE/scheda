# 发布到 PyPI：配置与 runbook

仓库侧的活儿已经做完了（`publish.yml` 走 tag 触发 + OIDC，仓库里没有任何 token）。
下面只剩 **PyPI 那边一次性配置**，然后每次发布就是推一个 tag。

## 0. 前提

- PyPI 账号 `LUgABeEEE` 已开启 **2FA**（PyPI 强制要求，没开无法发布）。
- 账号是该项目的 owner。

## 1. 加 Trusted Publisher（一次性，5 分钟）

打开 <https://pypi.org/manage/account/publishing/>（Account settings → Publishing）。

因为 `scheda` 在 PyPI 上**还不存在**，所以用 **"Add a new pending publisher"**（不是 "Add a publisher"，那个只对已存在的项目可用）。逐字段：

| 字段 | 填什么 |
|---|---|
| PyPI Project Name | `scheda` |
| Owner | `LUgABeEEE` |
| Repository name | `scheda` |
| Workflow name | `publish.yml` |
| Environment name | `pypi` |

点 Add。列表里出现一行 pending 就对了。

> **pending 不占名字。** 只有第一次成功上传后才真正占用 `scheda`。所以就别拖——
> 现在这个名字是空的（实测 404），谁先发谁得。

## 2. GitHub 侧不需要手工建 environment

`publish.yml` 里写的是 `environment: pypi`。GitHub 在 job 第一次跑时会**自动创建**这个
environment（默认无保护规则）。如果想加保护（例如只允许 tag 触发、或加人工审批）：

Settings → Environments → `pypi` → 加 **Required reviewers** 或 **Deployment branches: tags `v*`**。

## 3. 先在 TestPyPI 试一次（建议，但不是必须）

同样在 <https://test.pypi.org/manage/account/publishing/> 加一个 pending publisher，
参数与上面**完全一样**（TestPyPI 是独立账号体系，要单独注册/登录）。

然后手动触发：

```bash
gh workflow run publish.yml -R LUgABeEEE/scheda -f target=testpypi
gh run watch -R LUgABeEEE/scheda
```

成功的话去 <https://test.pypi.org/project/scheda/> 能看到 0.10.0。

## 4. 正式发布

```bash
cd <scheda 仓库>

# tag 要指向当前 HEAD（发布时重打，别用旧的）
git -c user.name=LUgABeEEE -c user.email=LUgABeEEE@users.noreply.github.com \
  tag -f -a v0.10.0 -m "scheda 0.10.0"
git push scheda v0.10.0
```

`publish.yml` 会被 tag 触发：build → 校验 tag 与 `__version__` 一致 → OIDC 上传。
2–3 分钟后：<https://pypi.org/project/scheda/0.10.0/>

## 5. 失败排查

| 报错 | 原因 | 处理 |
|---|---|---|
| `invalid-publisher` / `OIDC` 相关 | pending publisher 的五个字段有一个对不上 | 逐字符核对，尤其 workflow 名是 `publish.yml` 不是 `publish` |
| `File already exists` | 同版本已上传过 | 版本号必须递增（PyPI 不允许覆盖） |
| `403` + `environment` | environment 名不匹配 | 确认两处都是 `pypi`（小写） |
| `tag ... does not match` | tag 与 `__version__` 不一致 | 重打 tag（第 4 步） |
| `invalid-publisher` + 私有仓库 | Trusted Publishing 要求仓库可公开读取 | `scheda` 是 public，这条不适用 |

## 6. 备选：不想用 OIDC 就退回 API token

不推荐（token 会过期、要轮换、泄露面更大），但如果 OIDC 一直不通：

1. PyPI → Account settings → API tokens → **Add API token**，scope 选 `scheda`。
2. GitHub → Settings → Secrets and variables → Actions → New repository secret：
   名字 `PYPI_API_TOKEN`。
3. 把 `publish.yml` 的 upload 步骤加回：
   ```yaml
   with:
     password: ${{ secrets.PYPI_API_TOKEN }}
   ```
   并去掉 `permissions: id-token: write`。

## 7. 发出去之后

- `dsh-kb` 那个旧包不用管：它的 0.9.0 是 shim（`shim/` 目录），需要时单独发。
- 每次发布后确认 GitHub Release 的 tag 与 PyPI 版本一致（workflow 已经强制校验）。
