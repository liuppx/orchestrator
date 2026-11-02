# Machine Hub Staging 验收

这份清单用于真实 Backend + 两台机器 Hub 的 staging 验收。它不把 `--simulate` 或单元测试当作多机生产验收；每一项都要保存 Backend 命令/事件记录、Hub 日志和主机进程检查结果。

## 验收拓扑

```text
工作台/管理员 API -> Backend + PostgreSQL
                         /       \
                    Hub-A       Hub-B
                      |           |
                  OpenClaw-A  OpenClaw-B
```

要求：Backend 使用 staging PostgreSQL 并完成 Alembic migration；Hub-A 和 Hub-B 使用不同的 `HUB_ID`、凭证文件、状态 SQLite、OpenClaw 状态目录和实例根目录。公网或跨主机地址必须使用 HTTPS；只有本机回环开发地址允许 HTTP。

## 1. 准备与凭证

在 Backend 主机上确认 `HUB_ADMIN_TOKEN` 不是默认值，然后为两台机器分别签发凭证。明文 token 只写入目标机器的 `0600` 文件，不要放进命令历史、工单或日志：

```bash
export BACKEND_URL=https://staging.example.com
export ADMIN_TOKEN='从受控 Secret 来源读取'

curl --fail-with-body -sS "$BACKEND_URL/api/v1/internal/hubs/hub-staging-a/credentials" \
  -H "Authorization: Bearer $ADMIN_TOKEN" -H 'Content-Type: application/json' \
  -d '{"expires_in_seconds":604800}'
curl --fail-with-body -sS "$BACKEND_URL/api/v1/internal/hubs/hub-staging-b/credentials" \
  -H "Authorization: Bearer $ADMIN_TOKEN" -H 'Content-Type: application/json' \
  -d '{"expires_in_seconds":604800}'
```

把响应中的 token 分别写入两台主机，并确认文件权限为 `0600`。凭证 API 只返回一次明文；列表 API 不能返回 token。

## 2. 两台 Hub 预检与启动

在每台主机分别设置以下变量。路径必须是该主机自己的持久卷，不能共用 NFS 状态库：

```bash
export HUB_ID=hub-staging-a                 # B 主机改为 hub-staging-b
export HUB_BACKEND_URL=https://staging.example.com
export HUB_CREDENTIAL_FILE=/var/lib/orchestrator/hub/credential
export HUB_CLIENT_STATE_DB=/var/lib/orchestrator/hub/state.sqlite3
export HUB_OPENCLAW_RUNTIME_DIR=/var/lib/orchestrator/openclaw-runtime
export HUB_OPENCLAW_INSTANCES_ROOT=/var/lib/orchestrator/openclaw-instances
export HUB_OPENCLAW_STATE_FILE=/var/lib/orchestrator/openclaw-state.json
export HUB_OPENCLAW_SECRET_STORE_DIR=/var/lib/orchestrator/openclaw-secrets
export HUB_OPENCLAW_COMMAND_TIMEOUT_SECONDS=300

scripts/run_machine_hub.sh --check --openclaw
```

预检必须通过，随后用 systemd user 或 LaunchAgent 启动常驻 Hub。记录启动时间、Hub 版本、hostname、OS、Adapter capabilities，并在 Backend 数据库确认两台 Hub 都是 `online`。两台 Hub 的 inventory revision 应各自从 1 开始递增，不能互相覆盖。

## 3. 正常命令闭环

在每台机器各准备一个仅用于 staging 的 OpenClaw 实例，然后通过受控命令 API 执行 `inspect_instance`、`start_instance`、`stop_instance`。命令请求必须使用唯一 `idempotency_key` 和递增 `desired_revision`，不能通过 shell 字段绕过 typed payload。

逐项核对：命令经历 `accepted -> running -> succeeded/failed`，Backend 保存 attempt、lease、trace 和事件 sequence；重复提交同一幂等键返回同一 `command_id`；同一实例的旧 revision 不能覆盖新 revision；Hub 离线时命令不会被 Backend 直接改成本机成功。

## 4. 租约失效与取消

在 staging 临时制造一个可控的长 OpenClaw CLI 操作（例如仅对 staging profile 使用 wrapper 延迟，不修改生产 PATH），然后验证以下两种情况：

1. 让 Hub 续租收到 `409`，或阻断 Backend 一段时间直到 lease 接近到期。
2. 在命令执行期间向 Hub 发送 `SIGTERM`，再让服务恢复。

预期结果：

- Hub 向当前 Adapter 发出取消并等待本机操作结束，不领取下一条命令。
- OpenClaw 子进程组先收到 `SIGTERM`，3 秒后仍未退出则收到 `SIGKILL`。
- 命令结果为 `unknown`，错误码为 `LEASE_LOST`、`COMMAND_CANCELLED` 或 `COMMAND_TIMEOUT`，不能误报 `failed` 后自动重试。
- 用 `ps -eo pid,ppid,pgid,args` 和按 profile 的 `pgrep` 检查没有残留的 Hub 管理进程组。主动脱离原进程组的后代必须单独记录并阻断上线。

## 5. Hub 重启恢复

在 `start_instance` 已产生本机副作用、但终态事件尚未回报时停止 Hub，保留本机 SQLite 和 OpenClaw 状态目录，重新启动同一 `HUB_ID` 的服务。检查：

- 重启后先补报 outbox 事件，sequence 连续且不重复。
- Adapter 依据本机 PID/profile 状态识别已经运行的实例，不重复启动。
- `pair_whatsapp` 等无法安全证明外部副作用的操作保持 `unknown`，不会盲目重放。
- 删除或重建 Hub SQLite、改用另一台机器的状态库都应被拒绝或视为新的 Hub，不得静默接管旧实例。

## 6. 凭证轮换与隔离

为同一 Hub 签发第二个凭证，原子替换本机凭证文件，确认下一次 heartbeat 使用新 token；随后撤销旧凭证并确认旧 token 得到 `401`。再用 Hub-A 的 token 请求 Hub-B 的 heartbeat/inventory，必须得到 `401`，且 Hub-B 的状态、inventory 和事件没有变化。

## 7. 上线门槛

以下任一项未满足，就只允许 staging，不得宣称多机生产可用：

- 两台 Hub 均能完成预检、heartbeat、inventory 和至少一条真实 OpenClaw typed command。
- 租约失效、Hub 终止、CLI 超时三类场景均无残留受管进程组。
- 重启恢复不会重复 start/stop 或 pair 外部副作用，unknown 命令有人工处置路径。
- 凭证轮换、撤销、错误 Hub 隔离和状态 SQLite 持久化均有记录。
- Backend、Hub、OpenClaw profile、实例目录和日志的权限/备份策略经过值班人员确认。

当前实现仍不保证清理主动脱离进程组的后代，也不判断外部服务端副作用是否完成；这两项在验收记录中必须有明确结果，不能以本地进程退出代替证明。
