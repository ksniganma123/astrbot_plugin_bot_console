# Bot Console · AstrBot 插件

以 **Bot 为发送方视角** 的 WebUI 聊天控制台。在 AstrBot 插件页里打开一个 macOS
风格的聊天界面，按备注名选中群聊/私聊，然后**以 Bot 本人身份主动发送消息**。

- 目标平台：**QQ 官方机器人（qq_official / qq_official_webhook）**，同时兼容 aiocqhttp 等
  实现了 `send_by_session` 的适配器
- 不需要手填 `group_openid` 或 UMO —— 会话由插件"学习"并登记，备注名用群里的
  `/bind` 命令绑定
- 每次发送都写审计日志；UI 上显示本地频控估算

---

## 1. 安装

把 `astrbot_plugin_bot_console` 整个目录放进 AstrBot 的 `data/plugins/` 下，
或在 WebUI「插件 → 从文件安装」里上传 `astrbot_plugin_bot_console.zip`。

```
data/plugins/astrbot_plugin_bot_console/
├─ main.py
├─ metadata.yaml
├─ _conf_schema.json
├─ requirements.txt
├─ .astrbot-plugin/i18n/{zh-CN,en-US}.json
├─ pages/bot-console/{index.html,app.js,style.css}
└─ tests/{selftest.py,preview.html}        # 开发辅助，不影响插件加载
```

`tests/preview.html` 用浏览器直接打开（或本地静态服务器）就能看到用假数据渲染的
完整界面，不需要 AstrBot、也不会连 QQ。`tests/selftest.py` 是后端逻辑自检脚本。

要求 **AstrBot ≥ 4.15**（需要 Plugin Pages 能力：`pages/<name>/index.html` +
`window.AstrBotPluginPage` bridge）。本插件在 AstrBot 4.27.2 上开发与验证。

依赖：无。仅使用 AstrBot 自带的 `fastapi` / `aiofiles` / `qq-botpy`。

## 2. 启用

1. WebUI「插件」里确认 `bot-console` 已启用
2. 进入插件详情，打开 **Bot Console** 这个 Page
3. 如果页面提示"还没有可用会话"，继续下一步

## 3. 学习会话（必须做一次）

QQ 官方机器人拿到的 `group_openid` **与群号无关**，同一个群在不同 Bot 下是不同的值，
所以只能从真实消息里学：

1. 在目标 QQ 群里随便发一条消息（或 @Bot 一次）
2. 在同一个群里发送：

```
/bind 高三(2)班通知群
```

3. 回到 WebUI 刷新，左侧列表出现「高三(2)班通知群」

其它命令：

| 命令 | 作用 |
|---|---|
| `/bind <备注名>` | 把当前会话绑定到该备注名 |
| `/bind` | 查看当前会话的 UMO 与已绑定的备注名 |
| `/bind -` | 清除备注名（清除后不再允许发送） |

别名：`/绑定`、`/bindme`。

## 4. 发送

1. 左侧选会话
2. 输入文本
3. 点发送按钮（或 `⌘/Ctrl + Enter`）
4. **二次确认条**会显示「将以 Bot 身份发送到：{备注名}（群聊）」，点「确认发送」

这一步不可省略——备注名错配的代价是往错误的群里发消息。

输入区上方的 segmented control 可以切换模式：

- **直接以 Bot 发送**（默认）：消息作为 Bot 本人发到目标会话
- **模拟用户消息（调试）**：把输入伪装成一条用户消息注入 AstrBot 消息管道，
  触发正常的唤醒/插件/LLM 流程，用来观察 Bot 的回复

### 模拟模式的注意点

模拟消息的回复走 QQ 的**被动回复**接口，依赖最近一条真实消息的 `msg_id`
（有效期 5 分钟）。超过 5 分钟或 Bot 没有群主动消息权限时，Bot 的回复可能发不出去。
这是 QQ 平台限制，不是插件 bug。

## 5. 配置项

WebUI 插件配置页可改，全部有默认值：

| 配置 | 默认 | 说明 |
|---|---|---|
| `enabled` | true | 总开关 |
| `auto_register` | true | 自动登记收到的会话 |
| `allow_send` | true | 允许 Web 端主动发送 |
| `allow_simulate` | true | 允许模拟注入 |
| `send_fallback_enabled` | true | 允许 QQ 官方群聊兜底发送 |
| `qq_group_qpm_limit` | 60 | 单群每分钟上限（本地估算） |
| `qq_group_daily_limit` | 1000 | 单群每天上限（本地估算） |
| `quota_warn_ratio` | 0.8 | 频控告警阈值 |
| `audit_max_lines` | 200 | 中间消息流回显条数 |
| `audit_retention_days` | 90 | 审计日志保留天数，0 = 永久 |
| `require_bind_before_send` | true | 未命名会话禁止发送 |

## 6. 数据与审计

目录：`data/plugin_data/astrbot_plugin_bot_console/`

| 文件 | 内容 |
|---|---|
| `conversations.json` | 会话登记表（UMO / 备注名 / openid / 最后活跃时间） |
| `audit.jsonl` | 发送审计日志，每行一个 JSON |
| `quota.json` | 按天、按会话的发送计数 |

审计记录字段：`ts`、`time`、`mode`（`direct`/`simulate`）、`alias`、
`unified_msg_origin`、`scene`、`target_id`、`content_preview`、`success`、
`error`、`path`（`primary`/`fallback`/`simulate`）、`message_id`、`operator`。

```powershell
# 查看最近 5 条审计
Get-Content data/plugin_data/astrbot_plugin_bot_console/audit.jsonl -Tail 5
```

## 7. 实现要点（改代码前请读）

### 主发送路径

```python
await self.context.send_message(umo, MessageChain().message(text))
```

不使用 `send_message_by_id` / `create_event` 作为主路径——它们在 AstrBot 核心里对
`AiocqhttpAdapter` 硬编码，对 `qq_official` 不是通用接口。

### QQ 官方群聊的静默失败陷阱

`QQOfficialPlatformAdapter.send_by_session()` 只有在内存字典
`_session_scene[session_id] == "group"` 时才真正调用 `post_group_message`，
否则它**只打一条 warning 然后 return**——不抛异常，而
`context.send_message()` 只要找到平台实例就返回 `True`。

也就是说：**"发送成功但群里没有消息"是默认行为**，而 `_session_scene` 是进程内存
里的 dict，**AstrBot 一重启就空了**。

本插件每次发送前都补一次
`platform.remember_session_scene(target_id, "group")`，见
`BotConsolePlugin._ensure_scene()`。

### 兜底路径

主路径不可用（适配器声明不许主动发群消息，或抛异常）时，直接调用：

```python
await platform.client.api.post_group_message(
    group_openid=target_id, msg_type=0, content=text, msg_seq=...
)
```

复用 botpy 已有的 HTTP 客户端，access_token 刷新与重试都由 botpy 负责，
插件不自己拼 `Authorization` 头。

### 为什么不能只靠 `send_message` 的返回值判断成功

它只表示"找到并调用了平台实例"。所以插件额外用
`_primary_can_send()` 预判适配器内部的跳过条件，从而区分"发送失败"和
"适配器压根没发"。

### 前端约定

- 页面必须放在 `pages/<page_name>/index.html`（不是 `views/`）
- bridge 是 `window.AstrBotPluginPage`（不是 `AstrBotPluginView`），由平台自动注入
- 相对资源路径（`href="style.css"`）由平台自动重写，不用写绝对路径

**endpoint 不能带插件名前缀**（这一条踩过坑，务必注意）：

```js
// ✅ 正确：只写插件内子路径
AstrBotPluginPage.apiGet("conversations")
// ❌ 错误：又加了一次插件名
AstrBotPluginPage.apiGet("astrbot_plugin_bot_console/conversations")
```

因为 dashboard 自己会把 endpoint 拼成：

```
/api/v1/plugins/extensions/<pluginName>/<endpoint>
```

核心再用 `<pluginName>/<endpoint>` 这个**整串**去匹配 `register_web_api` 注册的路由。
自己再加一次插件名，就变成
`astrbot_plugin_bot_console/astrbot_plugin_bot_console/conversations`，
整串失配 → 浏览器报「未找到该路由」，左侧会话列表显示「读取会话列表失败」。

服务端因此同时注册了带前缀与不带前缀两种路由写法，前端路由未命中时也会自动
再试一次，两个方向都不会挂。

## 8. Web API

| Method | Path | 说明 |
|---|---|---|
| GET | `/{plugin}/conversations` | 已登记会话列表 |
| POST | `/{plugin}/conversations/bind` | `{unified_msg_origin, alias, note?}` |
| POST | `/{plugin}/conversations/forget` | `{unified_msg_origin}` 删除登记 |
| POST | `/{plugin}/send` | `{unified_msg_origin, content}` → `{ok, path, message_id, error?, warnings, quota}` |
| POST | `/{plugin}/simulate` | `{unified_msg_origin, content}` |
| GET | `/{plugin}/history` | 审计历史，支持 `limit` / `unified_msg_origin` |
| GET | `/{plugin}/status` | 插件状态、平台列表、当前配置 |

## 9. 验收清单

1. 插件加载后，WebUI 插件详情页能打开 `bot-console`，呈现 macOS 风格三栏布局
2. 在目标群发 `/bind 测试群`，Web 端会话列表出现「测试群」
3. 选中「测试群」→ 输入文本 → 确认 → QQ 群内实际收到（发送方是 Bot）
4. 切到"模拟用户消息"，注入一条文本 → Bot 的 LLM 回复出现在群里
5. `audit.jsonl` 里能看到每次发送的成功/失败记录
6. UI 符合 macOS 语言：16px 窗口圆角、毛玻璃、SF 字体栈、8pt 间距、系统蓝 `#007AFF`

## 10. 排查

| 现象 | 原因 / 处理 |
|---|---|
| 页面提示「未找到该路由」、左侧「读取会话列表失败」 | 前端 endpoint 带上了插件名前缀，导致 dashboard 拼接后路径重复。本版本已修正；若自行改过 `app.js` 请确认 `apiGet("conversations")` 这种裸写法 |
| 发送返回 `ok:true` 但群里没有消息 | 查看审计 `path` 字段是否为 `primary`。若是，说明适配器接受了但 QQ 侧丢弃——检查 Bot 是否有该群主动消息权限 |
| 返回"找不到 UMO 对应的平台适配器" | AstrBot 重载后 `platform_id` 变了。在目标群重新发一条消息让插件刷新登记 |
| 会话列表为空 | 该群还没发过消息，或 `auto_register` 被关闭。发条消息再 `/bind` |
| 提示"该会话还没有备注名" | 受 `require_bind_before_send` 保护，先 `/bind` |
| 页面白屏 / 提示找不到 bridge | 必须是 AstrBot ≥ 4.15，且从插件 Page 入口打开，不要直接双击 `index.html` |
| 兜底也失败 | 看返回的 `error`，通常是 QQ 侧限流或权限；`quota` 字段可看本地估算用量 |

## 11. 明确不做的事

- 不实现"顶号"或模拟真人 QQ 登录
- 不用群号反查 `group_openid`
- 不依赖 `send_message_by_id` 作为 qq_official 主发送路径
- 不在前端硬编码任何 openid 或 UMO

## 12. 许可

与 AstrBot 生态一致，按你的需要自由使用与修改。

## 13. 注意

- 此项目为AI Coding项目，本人不保证质量以及安全性
- 该插件可能违反QQ开放平台相关使用协议，请用户自行斟酌
- 本人Bilibili主页https://space.bilibili.com/3546855692568823