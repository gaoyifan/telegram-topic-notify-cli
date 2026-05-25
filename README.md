# telegram-topic-notify

一个独立 CLI：把一条通知发到 Telegram 私聊里的独立 topic，随后等待该 topic 中对应用户的回复，并把回复内容输出到标准输出。

## 特性

- 每次调用都会创建一个新的 **私聊 topic**
- 多个 CLI 并发调用时，靠 **topic + 共享轮询协调层** 隔离回复
- 用 `uv` 管理项目与依赖，Telegram HTTP 调用层基于 `httpx`
- 默认只把匹配到的用户回复打印到 **stdout**

## 前提

1. 用户已经和 Bot 打开私聊。
2. 该私聊里已经启用 **Topics**。
3. 所有并发实例使用同一个本地状态目录（默认是 `~/.local/state/telegram-topic-notify/`）。

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
  "请回复 OK"
```

也可以通过环境变量提供凭证：

```bash
export TELEGRAM_BOT_TOKEN=...
export TELEGRAM_USER_ID=123456789
uv run telegram-topic-notify "请回复 OK"
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

- 成功时：stdout 只输出匹配 topic 的第一条用户回复。
- 失败时：错误打印到 stderr，并以非零状态码退出。
- 并发时：只有一个进程会实际调用 `getUpdates`；其它进程会复用共享 SQLite 里落盘的更新结果，不会互相抢更新。

## 常用参数

- `--timeout`：最长等待回复秒数，默认 `60`
- `--poll-timeout`：单次 `getUpdates` 长轮询秒数，默认 `20`
- `--topic-prefix`：topic 名前缀，默认 `notify`
- `--state-dir`：覆盖本地共享状态目录
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

暴露的核心工具是 `ask_user`：**询问用户一个问题，并返回用户回复。**

示例 Cursor/通用 MCP 配置：

```json
{
  "mcpServers": {
    "telegram-topic-notify": {
      "command": "uv",
      "args": [
        "--directory",
        "/home/yifan/telegram-notify-cli",
        "run",
        "telegram-topic-notify-mcp"
      ]
    }
  }
}
```
