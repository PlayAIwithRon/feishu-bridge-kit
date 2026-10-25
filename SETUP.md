# 安装指南 SETUP

前提：一台能长期在线的 Linux 机器（云服务器、家里的常开主机、或 agent 的运行环境）、Python 3.10+、一个你自己管理的飞书租户（个人注册即可，要能建自建应用）。

## 第 1 步：在飞书开放平台建应用（约 5 分钟）

1. 电脑浏览器打开 https://open.feishu.cn → 开发者后台 → 创建应用 → 企业自建应用，名称随意（比如「我的助手」）。
2. 应用内左侧「应用能力」→ 添加能力 → 启用「机器人」。
3. 左侧「权限管理」，搜索「消息」，开通：接收消息、发送消息相关的权限。
4. 左侧「凭证与基础信息」，记下 **App ID** 和 **App Secret** —— 这就是你的全部凭证，自己保管，不要发给任何人。
5. 「事件与回调」和「版本管理与发布」**先别动**，等第 3 步监听跑起来再回来（原因见 README 的“坑”）。

## 第 2 步：部署桥本体

```bash
mkdir -p ~/workspace/feishu-bridge
cp bridge/* ~/workspace/feishu-bridge/
cd ~/workspace/feishu-bridge
python3 -m venv venv
./venv/bin/pip install lark-oapi
cp config.example.json config.json      # 填入你的 App ID / App Secret
cp watch-state.example.json watch-state.json
chmod 600 config.json
```

手动前台跑一次验证凭证和网络：

```bash
./venv/bin/python listener.py
# 期望日志出现：connected to wss://msg-frontier.feishu.cn/ws/v2 ...
# 若报 SSL wrong version number：你的机器需要走代理上网，确认环境变量
# http_proxy/https_proxy 已设置（补丁会自动读取）；仍不行先用 curl 测代理。
# Ctrl+C 停掉，进入下一步装服务。
```

## 第 3 步：装成系统服务（保活）

`feishu-bridge.service` 是模板，`%h` 代表你的家目录：

- 普通用户服务（有用户级 systemd 会话时）：把模板原样放进 `~/.config/systemd/user/`，`systemctl --user daemon-reload && systemctl --user enable --now feishu-bridge`。
- 系统服务（root 环境）：把 `%h` 替换成实际家目录绝对路径，`WantedBy=default.target` 改为 `multi-user.target`，放进 `/etc/systemd/system/`，`systemctl daemon-reload && systemctl enable --now feishu-bridge`。若机器上网要走代理，再给单元加一行 `EnvironmentFile=<桥目录>/proxy.env`（里面写 http_proxy/https_proxy 等变量，chmod 600）。

验证：`systemctl status feishu-bridge` 为 active，`journalctl -u feishu-bridge -n 20` 里有 `connected to wss://`。

## 第 4 步：回飞书后台完成对接

1. 「事件与回调」→ 订阅方式选 **「使用长连接接收事件」** → 添加事件 **「接收消息」（im.message.receive_v1）** → 保存。（此时监听必须在线，否则保存会失败。）
2. 「版本管理与发布」→ 创建版本 → 发布（自有租户内发布，自己确认即可）。

## 第 5 步：端到端测试

1. 在飞书 App 里搜你的机器人名字，打开单聊，发一句话，如「测试」。
2. 服务器上 `tail ~/workspace/feishu-bridge/inbox.jsonl` —— 看到一行带 message_id 的 JSON = 收通了。
3. 回复它：
   ```bash
   cd ~/workspace/feishu-bridge
   ./venv/bin/python reply.py --message-id <上一步的 message_id> --text "收到，双向通道通了"
   ```
   飞书里收到这句 = 发通了。双向桥完工。

## 第 6 步（可选）：单向汇报通道

想让系统往某个飞书群推格式化报告：群设置 → 群机器人 → 添加自定义机器人 → 拿到 webhook URL → `cp webhook.json.example webhook.json`，把 URL 填进 `url` 字段，`chmod 600 webhook.json`。发送：

```bash
./feishu_notify.py --type 出稿 --status 待批准 --severity action --body "今天的稿子好了，请定稿"
# severity: urgent=🔴 / action=🟡 / info=🟢 ；成功输出 FEISHU_OK
```

## 第 7 步（可选）：接上你的 agent

把 `agent-integration/` 接到你的 agent 框架上：

- 检测脚本 `feishu_inbox_watch.sh` 是给 Muse 的 hooks 机制写的（脚本尾部调用 `silent`/`wake` 两个运行时函数）。其他框架等价做法：每 30 秒跑一次同样的比对逻辑（inbox.jsonl 里 message_id 不在 watch-state.json 的 processed 列表中 = 有新消息），有新消息就触发你的 agent。
- `PROMPTS.md` 里是两个任务提示词（新消息处理、健康值守），把里面的称呼和路径换成你自己的即可直接用。

## 排障速查

| 现象 | 多半是 |
| --- | --- |
| 后台保存事件订阅失败 | 监听没在线，先启动服务再保存 |
| 日志 SSL WRONG_VERSION_NUMBER | 机器要走代理：检查 proxy 环境变量是否进了服务环境 |
| 能连上但收不到消息 | 事件没订阅「接收消息」，或应用版本没发布 |
| reply.py 报权限错误 | 应用没开「发送消息」权限，或改完权限没重新发布版本 |
| 服务重启后不见了 | 单元文件放在了会被回收的位置；把模板和配置都留在持久目录，并准备一键重装脚本 |
