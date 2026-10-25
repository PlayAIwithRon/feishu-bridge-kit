# Muse × 飞书双向桥 · Muse Feishu Bridge

把 **Muse 打通飞书**的完整套件：让 Muse 能通过**飞书**和主人双向对话——一边是主人随时在飞书里发消息派活，另一边是 Muse 收到、确认、处理并把结果发回飞书。外加一条单向汇报通道（把任务报告按统一格式推到飞书群）。

本套件是 2026-10-25 在一套真实在用的系统上提取、脱敏而成的。它包含两层：

## 两层结构

**第 1 层 · 桥本体（bridge/，独立可用，不依赖任何特定 AI 产品）**
- `listener.py` — 常驻监听：用飞书官方 SDK（lark-oapi）的**长连接**（websocket）接收消息事件，不需要公网地址、不需要域名和证书。收到的每条消息追加一行 JSON 到 `inbox.jsonl`。
- `reply.py` — 发送工具（命令行）：按 message_id 回复某条消息，或按 chat_id 主动发消息。
- `feishu_notify.py` — 单向汇报（可选模块）：把报告推到飞书群的自定义机器人 webhook，统一格式（标记 + 任务类型 + 状态 + 🔴/🟡/🟢 严重等级）。
- `feishu-bridge.service` — systemd 服务模板（保活、开机自启）。
- `config.example.json` / `webhook.json.example` / `watch-state.example.json` — 配置样例，真实文件永远不要外传（见 SECURITY.md）。

**第 2 层 · 智能体集成（agent-integration/，需要一个能跑定时任务/事件钩子的 agent 环境）**
- `feishu_inbox_watch.sh` — 每 30 秒轮询收件箱的检测脚本：发现未处理的新消息就唤醒 agent。
- `PROMPTS.md` — 两个现成的任务提示词：①新消息处理（回确认 + 转交主会话）；②健康值守（服务自愈）。原系统跑在 Muse 上，其他 agent 框架照着改也能用。
- 没有第 2 层时，第 1 层照样能收能发，只是消息要靠别的方式被消费（比如自己写个轮询脚本）。

## 快速开始

完整步骤见 **SETUP.md**，概览：

1. 在飞书开放平台建一个企业自建应用，开机器人能力 + 收发消息权限，拿到 App ID / App Secret（每位使用者建自己的，凭证不通用）。
2. 把 `bridge/` 放到服务器上 `~/workspace/feishu-bridge/`，建虚拟环境装 `lark-oapi`，把样例配置复制成真实配置并填入凭证（chmod 600）。
3. 用 systemd 模板装好服务并启动，日志出现 `connected to wss://` 即长连接成功。
4. 回飞书后台：事件订阅选「使用长连接接收事件」，添加「接收消息」事件，发布应用版本。
5. 在飞书里给机器人发一句话，`inbox.jsonl` 里出现这行 = 收通了；用 `reply.py --message-id <id> --text "..."` 回一句 = 发通了。双向完工。

## 已知特性与坑（原系统实测）

- 飞书长连接验证：后台保存事件订阅时会检查客户端是否在线，所以**先启动监听、再去后台保存**，顺序反了会报错。
- SDK 的 ws 客户端默认强制直连（proxy=None）；如果服务器要走 HTTP 代理上网，会连不上（SSL wrong version number）。`listener.py` 里已带补丁：自动把环境变量里的代理传给 websocket，无代理环境下是空操作。
- 监听服务本身很轻（一个 Python 进程）；真正的“智能”在第 2 层。原系统实测：消息从发出到收到 agent 确认回复约 20 秒（含 30 秒轮询的等待）。
- 单向 webhook 机器人和双向自建应用是两个不同的东西：前者只能往群里推消息，后者才能收消息。两个可以并存。

## 安全

先读 **SECURITY.md**。一句话版本：包里没有任何真实凭证；你自己的 App Secret、webhook URL 只能存在 chmod 600 的本地配置里，永远不要提交到代码仓库或发到聊天里。
