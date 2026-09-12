# 小智语音控制 Codex

这套桥接让音箱选择并监控 Codex 桌面任务。音频仍由现有 ASR 转成文字；服务器保存会话、可靠队列和播报计划；Mac 主动领取任务并通过 Codex App Server 操作现有任务。

## 支持的语音流程

1. “请切换到 Codex 模式”会请求电脑返回最近三个任务。
2. 用户用“第一个”或任务名称选择任务。
3. 小智播报当前状态并开始监控。
4. 用户可以发送指令、询问状态、切换任务或停止监控。
5. Codex 请求用户输入时，下一条语音会回答当前请求。
6. 指令真正被 Codex App Server 接收后，小智才会播报发送成功。
7. 用户可以开启每 1 至 120 分钟一次的状态播报。

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

## Mac Bridge

优先使用 Codex 桌面应用自带的新版二进制。桥接程序会启动 App Server，使用 `thread/list`、`thread/resume`、`turn/start` 和 `turn/steer`，并监听状态、用户输入和批准请求。

先用前台方式验证：

```bash
python3 tools/codex_voice_bridge.py \
  --token-file ~/.config/xiaozhi-codex-bridge/token \
  --server-url https://你的服务域名
```

公网连接必须使用 HTTPS。没有 HTTPS 域名时，通过 SSH 本地转发：

```bash
ssh -N -L 18003:127.0.0.1:8003 aliyun-dingqian

python3 tools/codex_voice_bridge.py \
  --token-file ~/.config/xiaozhi-codex-bridge/token \
  --server-url http://127.0.0.1:18003
```

## 常驻运行

为了让正在执行的 Turn 在桥接程序重启后仍由同一个 App Server 管理，先安装 Codex 的本地托管服务：

```bash
/Applications/ChatGPT.app/Contents/Resources/codex app-server daemon bootstrap
```

SSH 隧道用于常驻运行时需要配置免交互密钥登录，因为 LaunchAgent 无法输入 SSH 密码。然后生成两个 LaunchAgent：

```bash
ssh-keygen -t ed25519 -f ~/.ssh/xiaozhi_codex_bridge -N ""
# 将 ~/.ssh/xiaozhi_codex_bridge.pub 加入服务器 ~/.ssh/authorized_keys

python3 tools/install_codex_voice_bridge.py \
  --token-file ~/.config/xiaozhi-codex-bridge/token \
  --ssh-host aliyun-dingqian \
  --identity-file ~/.ssh/xiaozhi_codex_bridge
```

安装脚本只生成权限为 `0600` 的 plist，并打印 `launchctl bootstrap` 命令，不会自动启动。桥接日志位于：

```text
~/Library/Logs/com.xiaozhi.codex-bridge.log
~/Library/Logs/com.xiaozhi.codex-tunnel.log
```

## 状态和指令规则

- 空闲任务收到指令时调用 `turn/start`。
- 正在执行的任务默认把指令留在云端，当前 Turn 结束后再发送。
- 用户明确说“立即补充”时使用 `turn/steer`。
- Codex 正在请求用户输入时，语音内容会作为结构化答案返回，不会创建新 Turn。
- 命令执行、文件修改和额外权限批准必须听到明确的“批准”或“拒绝”。
- 关闭定时播报只关闭主动播报，用户仍可随时查询状态。

独立 App Server 会成为所选任务的执行端。选择任务后，应避免同时从另一 Codex 进程向同一任务启动 Turn；桌面应用仍能看到任务和历史记录。
