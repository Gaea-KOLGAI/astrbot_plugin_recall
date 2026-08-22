import re
from collections import defaultdict, deque

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star


class RecallPlugin(Star):
    """管理员引用 bot 消息并发送撤回关键词时，直接调用 OneBot 撤回。"""

    def __init__(self, context: Context, config: dict = None):
        super().__init__(context)
        self.config = config or {}
        self._bot = None
        self._max_recall = int(self.config.get("max_recall_count", 10) or 10)
        track_size = int(self.config.get("track_count", 50) or 50)
        self._msg_tracker: dict[str, deque] = defaultdict(
            lambda: deque(maxlen=track_size)
        )
        words = str(self.config.get("trigger_words", "撤回,recall,删除"))
        self._trigger_words = [w.strip().lower() for w in words.split(",") if w.strip()]

    @filter.on_astrbot_loaded()
    async def on_loaded(self):
        try:
            platform = self.context.get_platform(filter.PlatformAdapterType.AIOCQHTTP)
            if platform:
                bot = getattr(platform, "bot", None)
                if bot:
                    self._set_bot(bot)
                    logger.info("[recall] 插件已加载，消息追踪已启用。")
                    return
            logger.info("[recall] 插件已加载，等待 QQ 平台连接。")
        except Exception as e:
            logger.warning(f"[recall] 启动时获取 QQ 平台失败，将在收到消息时重试: {e}")

    def _set_bot(self, bot):
        self._bot = bot
        self._patch_bot(bot)
        return bot

    def _patch_bot(self, bot):
        """捕获当前 CQHttp 实例发送成功后返回的 message_id。"""
        bot._recall_plugin_ref = self
        if getattr(bot, "_recall_patched", False):
            return

        original_call_action = bot.call_action

        async def patched_call_action(action, **params):
            result = await original_call_action(action, **params)
            if action in ("send_group_msg", "send_private_msg", "send_msg"):
                try:
                    if isinstance(result, dict) and result.get("message_id") is not None:
                        group_id = params.get("group_id")
                        user_id = params.get("user_id")
                        key = (
                            f"g:{group_id}"
                            if group_id
                            else f"p:{user_id}"
                            if user_id
                            else "unknown"
                        )
                        plugin = bot._recall_plugin_ref
                        plugin._msg_tracker[key].append(int(result["message_id"]))
                        logger.debug(
                            f"[recall] tracked msg_id={result['message_id']} session={key}"
                        )
                except Exception as e:
                    logger.debug(f"[recall] 记录已发送消息失败: {e}")
            return result

        bot.call_action = patched_call_action
        bot._recall_patched = True

    def _ensure_bot(self, event: AstrMessageEvent):
        # event.bot 一定对应当前收到消息的连接；优先它，避免启动时拿到旧 CQHttp 实例。
        event_bot = getattr(event, "bot", None)
        if event_bot is not None:
            if event_bot is not self._bot:
                logger.info("[recall] 已绑定当前 QQ 连接。")
            return self._set_bot(event_bot)
        if self._bot is not None:
            return self._bot
        try:
            platform = self.context.get_platform(filter.PlatformAdapterType.AIOCQHTTP)
            bot = getattr(platform, "bot", None) if platform else None
            if bot:
                return self._set_bot(bot)
        except Exception as e:
            logger.debug(f"[recall] 运行时获取 QQ 平台失败: {e}")
        return None

    def _is_admin(self, event: AstrMessageEvent) -> bool:
        if getattr(event, "role", "") == "admin":
            return True
        try:
            sender = str(event.get_sender_id())
            admins = self.context.astrbot_config.get("admins_id", [])
            return any(str(admin) == sender for admin in admins)
        except Exception:
            return False

    def _session_key(self, event: AstrMessageEvent) -> str:
        group_id = event.get_group_id()
        return f"g:{group_id}" if group_id else f"p:{event.get_sender_id()}"

    def _reply_component(self, event: AstrMessageEvent):
        for component in event.get_messages() or []:
            if component.__class__.__name__.lower() == "reply":
                return component
        return None

    def _requested_count(self, text: str) -> int:
        match = re.search(r"\d+", text)
        if not match:
            return 1
        return max(1, min(int(match.group()), self._max_recall))

    async def _quoted_message_is_from_bot(self, event, bot, reply) -> bool:
        self_id = str(getattr(event.message_obj, "self_id", "") or "")
        sender_id = str(getattr(reply, "sender_id", "") or "")
        if self_id and sender_id and sender_id != "0":
            return sender_id == self_id
        try:
            detail = await bot.call_action(
                action="get_msg", message_id=int(getattr(reply, "id"))
            )
            sender = detail.get("sender", {}) if isinstance(detail, dict) else {}
            quoted_sender = str(
                sender.get("user_id")
                or (detail.get("user_id") if isinstance(detail, dict) else "")
                or ""
            )
            return bool(self_id and quoted_sender == self_id)
        except Exception as e:
            logger.warning(f"[recall] 无法校验被引用消息的发送者: {e}")
            return False

    def _ids_from_quote(self, event, quoted_id: int, count: int) -> list[int]:
        if count <= 1:
            return [quoted_id]
        tracked = list(self._msg_tracker.get(self._session_key(event), []))
        if quoted_id not in tracked:
            return [quoted_id]
        index = tracked.index(quoted_id)
        start = max(0, index - count + 1)
        return list(reversed(tracked[start : index + 1]))

    async def _delete_ids(self, event, bot, message_ids: list[int]) -> str:
        ok = 0
        errors = []
        for message_id in message_ids:
            try:
                await bot.call_action(action="delete_msg", message_id=int(message_id))
                ok += 1
                tracked = self._msg_tracker.get(self._session_key(event))
                if tracked and message_id in tracked:
                    tracked.remove(message_id)
            except Exception as e:
                logger.error(f"[recall] delete_msg {message_id} failed: {e}")
                errors.append(str(e))

        if not errors:
            return f"✅ 已撤回 {ok} 条消息。"
        error_text = " ".join(errors).lower()
        if "c2c" in error_text or "recall beat" in error_text:
            reason = "NapCat/QQ 当前不支持这条私聊消息的撤回；群聊撤回不受影响"
        elif "timeout" in error_text or "1200" in error_text:
            reason = "QQ 拒绝撤回，消息可能已超过撤回时限"
        else:
            reason = errors[-1]
        if ok:
            return f"✅ 撤回 {ok} 条，❌ {len(errors)} 条失败：{reason}"
        return f"❌ 撤回失败：{reason}"

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def on_recall(self, event: AstrMessageEvent):
        """引用 bot 消息 + 撤回关键词时直接执行，不经过 LLM。"""
        text = (event.message_str or "").strip().lower()
        if not any(word in text for word in self._trigger_words):
            return
        reply = self._reply_component(event)
        if reply is None:
            return
        if not self._is_admin(event):
            return

        bot = self._ensure_bot(event)
        if bot is None:
            event.stop_event()
            yield event.plain_result("❌ 撤回功能不可用：QQ 平台尚未连接。")
            return
        if not await self._quoted_message_is_from_bot(event, bot, reply):
            event.stop_event()
            yield event.plain_result("❌ 只能撤回 bot 自己发送的消息。")
            return

        try:
            quoted_id = int(getattr(reply, "id"))
        except (TypeError, ValueError):
            event.stop_event()
            yield event.plain_result("❌ 无法读取被引用消息的 ID。")
            return

        count = self._requested_count(text)
        message_ids = self._ids_from_quote(event, quoted_id, count)
        event.stop_event()
        result = await self._delete_ids(event, bot, message_ids)
        yield event.plain_result(result)

    @filter.llm_tool(name="recall_bot_messages")
    async def recall_tool(self, event: AstrMessageEvent, count: str = "1") -> str:
        """撤回 bot 最近发送的消息；优先使用“引用 bot 消息 + 撤回”的直接方式。

        Args:
            count(string): 要撤回的消息数量，默认 1，最大 10。
        """
        if not self._is_admin(event):
            return "❌ 权限不足，仅管理员可撤回 bot 消息。"
        bot = self._ensure_bot(event)
        if bot is None:
            return "❌ 撤回功能不可用：QQ 平台尚未连接。"
        try:
            number = max(1, min(int(count), self._max_recall))
        except (TypeError, ValueError):
            number = 1
        tracked = list(self._msg_tracker.get(self._session_key(event), []))
        if not tracked:
            return "❌ 没有追踪到可撤回消息，请直接引用 bot 的消息并发送“撤回”。"
        return await self._delete_ids(event, bot, list(reversed(tracked[-number:])))

    async def terminate(self):
        pass
