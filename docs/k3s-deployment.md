# k3s 手动部署记录

当前 fork 的 GitHub Actions 负责构建并发布镜像，没有自动部署 k3s 的 job，也没有在该镜像工作流中运行测试。Actions 成功后，仍需在能访问集群的机器上执行下面的脚本。本文用于更新已有 Deployment，不负责首次创建集群、持久卷或应用配置。

## 前提

- 本机安装 [uv](https://docs.astral.sh/uv/getting-started/installation/) 和 `kubectl`，可使用 Python 3.11；脚本只依赖 Python 标准库，无需同步项目依赖。
- kubeconfig 已配置且能访问目标集群；当前身份可读取 Deployment、执行容器内命令、更新镜像并观察 rollout。
- 已有 `dev/outlook-email-plus` Deployment，目标容器为 `outlook-email-plus`，维持单副本及原有 SQLite 持久卷。容器内需有 `python`，数据库所在目录需可写。
- 当前 GHCR 镜像公开，可匿名拉取。kubeconfig、OAuth 凭据和其他 secret 继续由现有环境管理，不写入部署记录或命令输出。

## 每次更新

1. 将准备部署的提交 push 到对应构建分支，等待该提交的 GitHub Actions 镜像构建成功。当前工作分支为 `fix/oauth-client-secret`，fork 的 `main` 仍是旧代码；不要把其他分支的成功记录当作本次构建结果。
2. 从成功 run 的摘要复制 **Pinned image**，格式必须是 `registry/name@sha256:<64位十六进制摘要>`。脚本要求 `--image` 显式传入固定 digest，不接受 `latest` 等可变 tag，也不自动解析 tag。
3. 在仓库根目录执行 server dry-run，再正式执行。以下是本次已部署镜像的可直接使用示例；下次更新替换为新 run 的 Pinned image。

```bash
uv run --no-project --python 3.11 python scripts/deploy_k3s.py --image ghcr.io/zeroornull/outlook-email-plus@sha256:287186891f8bafcb1c74143fbd153bb2acde078e15b86768d86ee4e3a1d13e72 --dry-run
uv run --no-project --python 3.11 python scripts/deploy_k3s.py --image ghcr.io/zeroornull/outlook-email-plus@sha256:287186891f8bafcb1c74143fbd153bb2acde078e15b86768d86ee4e3a1d13e72
```

默认使用 `kubectl` 当前 context，脚本会先打印 context、namespace、Deployment、容器和新旧镜像。执行前核对这些信息。若要显式选择 context，给两条命令都加上 `--context`，例如当前环境的 context 名为 `default`：

```bash
uv run --no-project --python 3.11 python scripts/deploy_k3s.py --context default --image ghcr.io/zeroornull/outlook-email-plus@sha256:287186891f8bafcb1c74143fbd153bb2acde078e15b86768d86ee4e3a1d13e72 --dry-run
```

其他参数默认值如下，目标环境不同时显式覆盖：

| 参数 | 默认值 |
| --- | --- |
| `--namespace` | `dev` |
| `--deployment` | `outlook-email-plus` |
| `--container` | `outlook-email-plus` |
| `--health-url` | `https://outlook-email-plus.173114.xyz/healthz` |
| `--timeout` | `180s`（rollout 等待时间） |

`--dry-run` 使用 `kubectl set image --dry-run=server -o name`，由 API server 检查更新请求，不创建备份、不实际更新 Deployment，也不验证新镜像能启动。

正式执行依次完成：

1. 读取当前目标容器的旧镜像并输出，供手动回退使用。
2. 在当前容器内通过 SQLite `Connection.backup` 在线备份数据库，不直接复制可能仍有 WAL 未 checkpoint 的数据库文件。
3. 设置指定镜像，等待 rollout 完成，再读取 Deployment 的目标容器镜像，确认与传入的完整 digest 引用一致。
4. 请求公网健康检查，要求 HTTP 200 且 JSON 的 `status` 为 `ok`。

任一步失败都会以非零状态退出并标明失败步骤；脚本不会自动回滚。保留输出中的旧镜像和备份路径。相同 digest 再次执行仍会备份及核对健康状态，但不会主动强制重启 Pod。

## SQLite 备份与手动回退

备份源取容器内的 `DATABASE_PATH`，未设置时为 `data/outlook_accounts.db`；脚本解析为绝对路径，在同目录生成带唯一 UTC 后缀的 `.bak` 文件，并输出完整路径及字节数。备份与数据库保存在同一 PV，能跨 Pod 更换保留，但不构成卷外灾备，容量也需由操作者管理。

本次部署前已有备份示例：

```text
/app/data/outlook_accounts.db.pre-oauth-5e84085-20261126T224922.bak
3010560 字节
```

需要退回镜像时，把下面的 `PREVIOUS_IMAGE` 替换为部署输出中记录的旧镜像完整引用，并核对 context；namespace、Deployment 或容器若有覆盖，也要同步修改：

```bash
kubectl --context default -n dev set image deployment/outlook-email-plus outlook-email-plus=PREVIOUS_IMAGE
kubectl --context default -n dev rollout status deployment/outlook-email-plus --timeout=180s
kubectl --context default -n dev get deployment outlook-email-plus -o jsonpath="{.spec.template.spec.containers[?(@.name=='outlook-email-plus')].image}"
```

回退后再次检查 `https://outlook-email-plus.173114.xyz/healthz` 的 HTTP 状态和 JSON `status`。镜像回退不恢复数据库；如新版本已迁移数据库，需单独评估旧版本兼容性。数据恢复还需评估停写、迁移、备份后的新增数据及 WAL/SHM 处理，不能在运行中的应用旁直接覆盖数据库。脚本不自动恢复或回退数据库。

## 2026-11-26 部署记录

| 项目 | 记录 |
| --- | --- |
| 分支 | `fix/oauth-client-secret` |
| Git commit | `5e8408540407a74380900ce731bbd9fc396dcac4` |
| Actions | [45842268084](https://github.com/zeroornull/outlookEmailPlus/actions/runs/45842268084)，成功，用时 2 分 12 秒 |
| 镜像 | `ghcr.io/zeroornull/outlook-email-plus@sha256:11397a151f21980f83a4bc8005cdf66134c18a508a97226120ddaabad54dcb53` |
| 目标 | namespace `dev`，Deployment / container `outlook-email-plus`，单副本 |
| 健康检查 | <https://outlook-email-plus.173114.xyz/healthz>，返回 `status=ok` |

当前 `/healthz` 的 `git_sha` / `build_time` 来自仓库历史 `.build-info`，其中 SHA 仍为旧值 `fc5844`；不要用它判定此次部署版本。脚本仅检查健康状态，版本以 Deployment 中固定的镜像 digest 核对，前端页脚可作辅助核对。

## 2026-11-26 Token 自动导入部署

| 项目 | 记录 |
| --- | --- |
| Git commit | `3c62e533cfffdd27dfed115b1fe65b7b7b44df09` |
| Actions | [45852423011](https://github.com/zeroornull/outlookEmailPlus/actions/runs/45852423011)，SPA、amd64、arm64 与多架构合并全部成功 |
| 镜像 | `ghcr.io/zeroornull/outlook-email-plus@sha256:287186891f8bafcb1c74143fbd153bb2acde078e15b86768d86ee4e3a1d13e72` |
| 部署前备份 | `/app/data/outlook_accounts.db.20261126T154845685521Z-331a439598d6400e842139c6d0675df1.bak`，3080192 字节 |
| 部署结果 | server dry-run、rollout、镜像 digest 核对通过；公网健康检查 HTTP 200、`status=ok` |
| 验证 | OAuth 工具 88 项测试、SPA TypeScript 与旧前端 JavaScript 语法检查通过；线上新旧页面已显示自动导入入口，实际授权链接包含 `openid profile email` |

完整 Microsoft 登录至自动写库的生产流程仍需账号持有人完成 Microsoft 登录与安全验证；本次未代替持有人完成新的授权。

## 2026-11-26 账号健康状态部署

| 项目 | 记录 |
| --- | --- |
| 分支 | `fix/oauth-client-secret` |
| Git commit | `9ede6547bee0cc5ea9f8cd668f4635b845c4f39f` |
| Actions | [45857223320](https://github.com/zeroornull/outlookEmailPlus/actions/runs/45857223320)，SPA、amd64、arm64 与多架构合并全部成功 |
| 镜像 | `ghcr.io/zeroornull/outlook-email-plus@sha256:06f05bca552258c50af8ae7a396caa5d4d348c32084f48c18818384cd5b68076` |
| 上一镜像 | `ghcr.io/zeroornull/outlook-email-plus@sha256:287186891f8bafcb1c74143fbd153bb2acde078e15b86768d86ee4e3a1d13e72`（Token 自动导入） |
| 部署前备份 | `/app/data/outlook_accounts.db.20261126T161857061588Z-fc661936a7fb4e49983f19ada39c508f.bak`，3080192 字节 |
| 部署结果 | server dry-run、rollout、镜像 digest 核对通过；公网健康检查 HTTP 200、`status=ok` |
| 验证 | 概览修复、覆盖导入待验证状态与账号健康状态合并发布；76 项后端定向测试、SPA TypeScript、13 项状态标签测试与旧前端语法检查通过；线上 `/api/accounts` 返回 `health_status`，账号管理页把最近刷新失败的账号显示为“授权过期”，不再显示“正常”，概览统计为 62 个授权过期 / 64 个活跃 |

## 每日 Token 刷新

线上已设置 `enable_scheduled_refresh=true`、`use_cron_schedule=false`、`refresh_interval_days=1`。现有任务每天北京时间 02:00 检查刷新，执行时读取数据库中的周期，无需为这项配置重启服务。

刷新成功后保存 Microsoft 轮换返回的新 refresh token；每日刷新无法恢复已撤销或已过期的授权。此类账号需重新登录 Microsoft，在 Token 工具中换取 Token 后自动导入对应邮箱。
