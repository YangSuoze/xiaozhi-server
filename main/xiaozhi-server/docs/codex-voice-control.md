# 小智语音控制 Codex

这套桥接让音箱选择、监控并继续 Codex 桌面应用中的现有任务。音频仍由小智服务器现有的 ASR 转成文字；云端保存语音会话、可靠任务队列和播报计划；Mac 端桥接由 Codex 桌面应用作为 MCP 后台进程启动，并通过桌面工具操作任务。

## 支持的语音流程

1. “请切换到 Codex 模式”会请求电脑返回最近三个任务。
2. 用户用“第一个”或任务名称选择任务。
3. 小智播报当前状态、正在处理的步骤和最近结果，并开始监控。
4. 用户可以发送指令、询问状态、切换任务或停止监控。
5. 指令真正被 Codex 桌面应用接收后，小智才会播报发送成功。
6. 用户可以开启每 1 至 120 分钟一次的进度播报。

进度来自 Codex 桌面任务最近的过程说明和完成结果，包含“正在做”、
“刚完成”和“下一步”三个可选字段。桥接会删除代码块、路径、网址和疑似密钥，
再将短文本发送给服务器；没有可靠信息时会明确说最近没有新的步骤说明，
不会根据耗时猜测完成百分比。

服务器区分云端已排队、电脑已领取和 Codex 已接收。电脑离线时指令保留在 SQLite；租约过期后会自动重试。完成和失败的记录默认保留 72 小时，然后自动删除。

## 服务器配置

生成至少 32 字节的随机密钥，并把 Mac 副本保存为仅本人可读：

```bash
mkdir -p ~/.config/xiaozhi-codex-bridge
openssl rand -hex 32 > ~/.config/xiaozhi-codex-bridge/token
chmod 600 ~/.config/xiaozhi-codex-bridge/token
```

把密钥写入 `main/xiaozhi-server/data/.config.yaml`。该文件已被 Git 忽略，不要把密钥写入仓库：

```yaml
codex_bridge:
  enabled: true
  token: "替换成刚才生成的密钥"
  database_path: data/codex_bridge.db
  bridge_offline_seconds: 45
  job_lease_seconds: 60
  recent_task_limit: 3
  default_announcement_interval_minutes: 5
  retention_hours: 72

Intent:
  intent_llm:
    functions:
      - codex_control
      # 保留原来已经使用的其他函数
      - get_weather
      - get_news_from_newsnow
      - play_music
      - set_alarm
```

自定义配置中的 `functions` 会覆盖默认列表，所以必须同时保留原有函数。

重启小智服务后，以下接口会出现在 HTTP 端口：

- `POST /codex/bridge/v1/heartbeat`
- `POST /codex/bridge/v1/jobs/lease`
- `POST /codex/bridge/v1/jobs/complete`
- `POST /codex/bridge/v1/events`
- `GET /codex/bridge/v1/health`

所有接口都要求 `X-Codex-Bridge-Token`，服务器不接受未认证的桥接请求。

## Mac 桥接

Mac 端使用 Codex 桌面应用自带的 Node 和动态 App Tools 管道，调用 `list_threads`、`read_thread`、`wait_threads` 和 `send_message_to_thread`。桥接和桌面应用使用同一个任务控制端，因此不会争抢任务写锁。

公网接口必须使用 HTTPS。没有 HTTPS 域名时，使用 SSH 本地转发：

```bash
ssh -N -L 18003:127.0.0.1:8003 aliyun-dingqian
```

常驻隧道需要免交互密钥登录，然后运行安装脚本：

```bash
ssh-keygen -t ed25519 -f ~/.ssh/xiaozhi_codex_bridge -N ""
# 将 ~/.ssh/xiaozhi_codex_bridge.pub 加入服务器 ~/.ssh/authorized_keys

python3 tools/install_codex_voice_bridge.py \
  --token-file ~/.config/xiaozhi-codex-bridge/token \
  --ssh-host aliyun-dingqian \
  --identity-file ~/.ssh/xiaozhi_codex_bridge
```

安装脚本会：

- 把桥接程序复制到 `~/Library/Application Support/XiaozhiCodexBridge`；
- 在 `~/.codex/config.toml` 中写入一段带边界标记的 MCP 配置，重复运行只会替换这段配置；
- 为 SSH 隧道生成 `~/Library/LaunchAgents/com.xiaozhi.codex-tunnel.plist`；
- 删除旧版独立 Python 桥接的 LaunchAgent 文件。

安装后重启一次 Codex 桌面应用。以后 Codex 启动时会加载桥接，SSH 隧道由 macOS 自动保持连接。

## 状态和指令规则

- 正在执行的任务默认把新指令留在云端，状态变为空闲后自动发送。
- 用户明确说“立即补充”时，指令会立即交给 Codex 桌面应用处理。
- 关闭定时播报只关闭主动播报，用户仍可随时查询状态和具体进度。
- Codex 的文件修改、命令和权限确认仍保留在桌面应用中，避免语音误确认敏感操作。
- 桥接只保存任务 ID、状态和必要的短文本，不复制项目文件，也不下载音乐资源。
