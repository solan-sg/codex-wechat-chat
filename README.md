# Codex 微信聊天 Skill

让 Codex 接管 Windows 微信电脑版中指定联系人的**文本消息**。Codex 根据用户提前说明的人设、聊天背景、目的和风格生成回复，再由桥接脚本自动发送。监听会一直持续，直到用户输入停止指令或关闭停止窗口。

仅支持 Windows 微信电脑版（我的微信版本为3.9.9.35）和文本消息。图片、文件、语音和表情会直接忽略。

## 功能

- 只监听一个指定联系人，不会自动选择或监听全部会话。
- 收到文本后立即输出 JSON Lines 事件，Codex 按用户设定决定如何回复。
- 连续收到多条文本时，每条消息都有独立的 `id` 和回复，桥接器按到达顺序逐条发送，不会等待上一条消息才继续收取。
- 不调用 DeepSeek 或其他大模型 API，回复内容完全由 Codex 生成。
- 提供 Tkinter 停止窗口。
- 启动时忽略聊天窗口中已有的历史消息，停止时清空所有待处理队列。
- 监听默认每 50ms 轮询一次，只解析新增消息项，避免聊天记录变长后延迟累积。

## 环境要求

- Windows 10 或 Windows 11
- Python 3.10+
- 已登录的 Windows 微信电脑版（版本为微信 3.9.9.35 ）
- Codex

## 安装

将本目录放到 Codex 的 skill 目录中，然后安装依赖：

```powershell
cd <skill-directory>\codex-wechat-chat
python -m pip install -r requirements.txt
```

Codex 启动桥接脚本时，需要使用安装了这些依赖的同一个 Python 环境。

## 使用

在 Codex 中直接描述联系人和聊天设定，例如：

> 今天我刚认识了一个朋友，已经加了微信。她的备注叫“小美”，这次聊天目的是自然认识一下。我是 22 岁，刚刚工作，做 AI 应用开发，喜欢打游戏。聊天风格要自然，不要老是反问。

Codex 会把这些内容映射为聊天背景、人设、目的和风格，然后调用桥接脚本监听指定联系人。

桥接器启动完成前已经显示在聊天窗口中的消息会被视为历史记录，不会触发回复。每次监听结束时，命令、待回复和 UI 事件队列都会清空，未发送的回复不会带到下一次启动。

启动监听后，Codex 必须保持当前回合并持续读取终端事件。桥接脚本只负责微信收发，不会在 Codex 回合结束后自行生成回复。收到新消息时，Codex 应先立即写回 `reply`，再显示状态；不要在发送前插入额外的说明或工具调用。一次收到多条消息时，应在同一次 stdin 写入中逐条提交全部回复。

停止方式：

- 在 Codex 中输入“停止”。
- 点击“Codex 微信聊天”窗口中的“停止监听”。
- 关闭桥接终端会话。

## 协议

桥接脚本位于 `scripts/wechat_bridge.py`。它通过 stdout 输出以 `WECHAT_SKILL ` 开头的事件，通过 stdin 接收命令。

收到文本时：

```json
{"event":"message","id":"m1-123","contact":"联系人昵称","text":"你好呀"}
```

回复：

```json
{"action":"reply","id":"m1-123","text":"你好呀，今天过得怎么样？"}
```

选择不回复：

```json
{"action":"skip","id":"m1-123"}
```

如果连续收到多条消息，必须分别回复，不能合并：

```json
{"event":"message","id":"m1-123","text":"你好"}
{"event":"message","id":"m2-124","text":"在吗"}
```

```json
{"action":"reply","id":"m1-123","text":"在呢"}
{"action":"reply","id":"m2-124","text":"刚刚看到，怎么啦？"}
```

桥接器会按 `m1-123`、`m2-124` 的顺序发送两条独立微信消息。

停止：

```json
{"action":"stop"}
```

## 目录

```text
codex-wechat-chat/
|-- SKILL.md
|-- README.md
|-- agents/openai.yaml
|-- scripts/wechat_bridge.py
|-- requirements.txt
`-- vendor/wxauto/
```

`vendor/wxauto/` 内置了 [wxauto](https://github.com/cluic/wxauto)，用于操作 Windows 微信客户端。其 Apache License 2.0 文本保存在 `vendor/wxauto/LICENSE`。

## 安全说明

这不是微信官方 API。它通过 Windows UI Automation 控制已经登录的微信桌面客户端，行为可能受微信版本或客户端界面变化影响。请只在自己有权使用的微信账号上使用，并避免发送包含密码、验证码、资金操作或其他敏感承诺的内容。

## 许可证

本项目代码使用 MIT License。内置 `wxauto` 使用 Apache License 2.0。
