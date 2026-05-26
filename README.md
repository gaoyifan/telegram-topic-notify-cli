# telegram-topic-notify

一个独立 CLI：通过 **Telethon / MTProto** 在 Telegram 私聊里**先创建一个 topic**，再把消息发到该 topic 中，随后等待同一 topic 里的用户回复，并把回复内容输出到标准输出。

## 特性

- 基于 **Telethon**，不再调用 Telegram Bot HTTP API
- 每次调用都会先创建一个新的 **私聊 topic**
- 提示消息会发送到该 topic 中，回复匹配也按 **topic ID** 过滤
- 通过 **持久化 Telethon session** 复用 bot 登录态，避免每次调用都重新创建 session
- 默认只把匹配到的用户回复打印到 **stdout**

## 前提

1. 用户已经和 Bot 打开私聊。
2. 需要提供：
   - `TELEGRAM_BOT_TOKEN`
   - `TELEGRAM_USER_ID`
   - `TG_API_ID`
   - `TG_API_HASH`
3. 需要为每个长期复用的 worker/client 准备一个**持久化 session 名称**。默认使用当前主机名。

## 安装

```bash
cd /home/yifan/telegram-notify-cli
uv sync
```

## 用法

```bash
uv run telegram-topic-notify \
  --token "$TELEGRAM_BOT_TOKEN" \
  --user-id 123456789 \
  --api-id "$TG_API_ID" \
  --api-hash "$TG_API_HASH" \
  --topic-prefix notify \
  "请回复 OK"
```

也可以通过环境变量提供凭证：

```bash
export TELEGRAM_BOT_TOKEN=...
export TELEGRAM_USER_ID=123456789
export TG_API_ID=...
export TG_API_HASH=...
uv run telegram-topic-notify "请回复 OK"
```

现在仓库已公开，也支持直接通过 `uvx` 从 GitHub 运行：

```bash
uvx --from git+https://github.com/gaoyifan/telegram-topic-notify-cli \
  telegram-topic-notify "请回复 OK"
```

项目根目录已支持自动读取 `.env`，所以本地开发时可以直接：

```bash
uv run telegram-topic-notify "请回复 OK"
```

如果没有传入消息参数，CLI 会在 stdin 非 TTY 时读取标准输入：

```bash
printf '构建完成，请确认。' | uv run telegram-topic-notify
```

## 行为说明

- 成功时：stdout 只输出**同一 topic** 中匹配到的第一条用户回复。
- 失败时：错误打印到 stderr，并以非零状态码退出。
- CLI 会先创建 topic；真正可用的 topic/thread id 来自创建服务消息的 `reply_to_top_id`，不是那条服务消息自己的 `id`。
- 提示消息会通过 Telethon raw `SendMessageRequest` 发送，并显式设置 `InputReplyToMessage(reply_to_msg_id=topic_id, top_msg_id=topic_id)`，这样消息才会真正落进该 topic。
- 匹配回复时，会检查消息的 `reply_to.forum_topic`，并按 `reply_to_top_id` 是否等于这次创建的 `topic_id` 过滤。
- 如果用户把消息发在主消息通道，或发到了别的 topic，CLI 不会把它当成匹配回复。
- 默认 session 名称是当前主机名；如果你要在**同一台机器上并发运行多个 Telethon worker**，请显式传入不同的 `--session-name`。
- 多台机器同时使用同一个 bot token + 多个 Telethon session 时，更新分发仍然是 **best-effort**：这种架构比 Bot API 轮询更接近原生客户端，但依然可能出现抢消息或漏消息。

## 常用参数

- `--timeout`：最长等待回复秒数，默认 `300`（5 分钟）
- `--api-id` / `--api-hash`：覆盖 `TG_API_ID` / `TG_API_HASH`
- `--topic-prefix`：生成 topic 名称时使用的前缀，默认 `notify`
- `--session-name`：指定当前 worker 的持久化 Telethon session 名称
- `--state-dir`：覆盖本地 Telethon session 目录
- `--json`：把匹配到的回复以 JSON 输出到 stdout

## 测试

```bash
cd /home/yifan/telegram-notify-cli
uv run pytest
```

## MCP 用法

项目提供了一个基于 **stdio** 的 MCP Server，适合 Cursor、Claude Code 等支持 MCP 的 Agent 客户端。

启动命令：

```bash
uv run telegram-topic-notify-mcp
```

也可以直接通过 `uvx` 从 GitHub 启动：

```bash
uvx --from git+https://github.com/gaoyifan/telegram-topic-notify-cli \
  telegram-topic-notify-mcp
```

暴露的核心工具是 `ask_user`：**询问用户一个问题，并返回同一 topic 中的用户回复。**

示例 `mcp.json` / Cursor 通用 MCP 配置：

```json
{
  "mcpServers": {
    "telegram-topic-notify": {
      "command": "uvx",
      "args": [
        "--from",
        "git+https://github.com/gaoyifan/telegram-topic-notify-cli",
        "telegram-topic-notify-mcp"
      ],
      "env": {
        "TELEGRAM_BOT_TOKEN": "123456:example",
        "TELEGRAM_USER_ID": "123456789",
        "TG_API_ID": "12345",
        "TG_API_HASH": "your_api_hash",
        "TG_SESSION_NAME": "telegram-topic-notify-mcp"
      }
    }
  }
}
```

如果你要在**同一台机器上运行多个 MCP server 实例**，请为每个实例设置不同的 `TG_SESSION_NAME`。
