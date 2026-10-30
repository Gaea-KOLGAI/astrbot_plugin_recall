# astrbot_plugin_recall

管理员**引用** bot 发的某条消息 + 说"撤回"，让 bot 撤回那条消息。

## 用法

| 操作 | 效果 |
|------|------|
| 引用 bot 的消息 + 发"撤回" | 撤回被引用的那条 |
| 引用 bot 的消息 + 发"撤回 3" | 撤回被引用的那条 + 之前 2 条（共 3 条） |
| 引用 bot 的消息 + 发"recall" | 同样触发（关键词可配置） |

> 仅**管理员**可用。普通成员引用不会触发。

## 触发条件（全部满足才执行）

1. 发送者是管理员
2. 消息中**引用**了一条消息（QQ 的"回复"功能）
3. 被引用的消息是 bot 自己发的
4. 消息文本包含触发关键词（默认"撤回"/"recall"/"删除"）

**不会**在普通聊天中误触发——只有引用 + 关键词同时满足才会撤回。

## 配置项（WebUI 可调）

| 配置项 | 说明 | 默认值 |
|--------|------|--------|
| `trigger_words` | 触发撤回的关键词，逗号分隔 | `撤回,recall,删除` |
| `max_recall_count` | 一次最多撤回多少条 | `10` |
| `track_count` | 每个会话最多追踪多少条消息 ID | `50` |

## 原理

1. 插件在 AstrBot 启动后获取 aiocqhttp 平台的 `bot` (CQHttp) 实例
2. 对 `bot.call_action` 做薄包装，拦截 `send_group_msg` / `send_private_msg` 的返回值，按会话存入 `message_id`
3. 管理员引用 bot 的消息 + 关键词时，从 Reply 组件取 `message_id`，调 OneBot `delete_msg` 撤回
4. 通过 Reply 组件的 `sender_id` 或追踪器双重校验确认被引用的消息确实来自 bot

## 兼容性

- **QQ (aiocqhttp / NapCat / Lagrange)**: ✅
- 其他平台: ❌（依赖 OneBot v11 `delete_msg` 接口）

数量写法：`撤回 3`、`撤回3条`、`撤回 7 个` 都会被识别；正文里的年份之类的长数字不会被当成条数（`撤回 2026 年的那条` 只撤 1 条），数量上限受 `max_recall_count` 约束。

## 依赖

无额外依赖。

## 测试

```bash
python -m unittest discover -s tests -v
```

`tests/` 用最小 astrbot 桩离线跑，不需要装 AstrBot。
