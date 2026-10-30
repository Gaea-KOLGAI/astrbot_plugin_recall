"""不依赖 AstrBot 运行时的纯逻辑测试。

跑法：python -m unittest discover -s tests -v
"""

# pyright: reportAttributeAccessIssue=false

import asyncio
import logging
import sys
import types
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _passthrough(*args, **kwargs):
    def wrap(fn):
        return fn

    return wrap


class _Filter:
    class PlatformAdapterType:
        AIOCQHTTP = "aiocqhttp"

    class EventMessageType:
        ALL = "all"

    on_astrbot_loaded = staticmethod(_passthrough)
    event_message_type = staticmethod(_passthrough)
    llm_tool = staticmethod(_passthrough)


def _install_astrbot_stub():
    """把 AstrBot 的最小桩塞进 sys.modules，让 main.py 可以离线导入。"""
    if "astrbot" in sys.modules:
        return
    astrbot = types.ModuleType("astrbot")
    api = types.ModuleType("astrbot.api")
    api.logger = logging.getLogger("astrbot-test")
    event = types.ModuleType("astrbot.api.event")
    event.AstrMessageEvent = type("AstrMessageEvent", (), {})
    event.filter = _Filter
    star = types.ModuleType("astrbot.api.star")

    class Context:
        def __init__(self, *args, **kwargs):
            self.astrbot_config = {}

    class Star:
        def __init__(self, context=None):
            self.context = context

    star.Context = Context
    star.Star = Star
    for name, module in (
        ("astrbot", astrbot),
        ("astrbot.api", api),
        ("astrbot.api.event", event),
        ("astrbot.api.star", star),
    ):
        sys.modules[name] = module


_install_astrbot_stub()

from main import RecallPlugin, parse_recall_count  # noqa: E402


class FakeBot:
    def __init__(self):
        self.self_id = "10000"
        self.sent = []

    async def call_action(self, action, **params):
        if action in ("send_group_msg", "send_private_msg", "send_msg"):
            message_id = 100 + len(self.sent)
            self.sent.append(message_id)
            return {"message_id": message_id}
        return {}


class CountParsingTest(unittest.TestCase):
    def test_plain_keyword_means_one(self):
        self.assertEqual(parse_recall_count("撤回", 10), 1)
        self.assertEqual(parse_recall_count("recall", 10), 1)
        self.assertEqual(parse_recall_count("删除这个文件", 10), 1)
        self.assertEqual(parse_recall_count("撤回 abc", 10), 1)
        self.assertEqual(parse_recall_count("", 10), 1)
        self.assertEqual(parse_recall_count(None, 10), 1)

    def test_number_after_keyword(self):
        self.assertEqual(parse_recall_count("撤回 3", 10), 3)
        self.assertEqual(parse_recall_count("撤回3", 10), 3)
        self.assertEqual(parse_recall_count("recall 5", 10), 5)
        self.assertEqual(parse_recall_count("删除 2", 10), 2)

    def test_number_with_unit(self):
        self.assertEqual(parse_recall_count("撤回3条", 10), 3)
        self.assertEqual(parse_recall_count("撤回 7 个", 10), 7)
        self.assertEqual(parse_recall_count("帮我撤回 4 次", 10), 4)

    def test_year_is_not_a_count(self):
        # 回归：旧实现取全文第一个数字，会把年份当成条数，一次删 10 条
        self.assertEqual(parse_recall_count("撤回 2026 年的那条", 10), 1)
        self.assertEqual(parse_recall_count("撤回 2026", 10), 1)
        self.assertEqual(parse_recall_count("删除 1999 年发的", 10), 1)

    def test_count_is_clamped(self):
        self.assertEqual(parse_recall_count("撤回 99", 10), 10)
        self.assertEqual(parse_recall_count("撤回 100 条", 10), 10)
        self.assertEqual(parse_recall_count("撤回 3", 2), 2)
        self.assertEqual(parse_recall_count("撤回 3", 0), 1)


class ConfigClampTest(unittest.TestCase):
    def test_zero_and_junk_fall_back_to_one(self):
        plugin = RecallPlugin(None, {"track_count": 0, "max_recall_count": 0})
        self.assertEqual(plugin._max_recall, 1)
        self.assertEqual(plugin._msg_tracker.default_factory().maxlen, 1)

        plugin = RecallPlugin(None, {"track_count": "abc", "max_recall_count": None})
        self.assertEqual(plugin._max_recall, 10)
        self.assertEqual(plugin._msg_tracker.default_factory().maxlen, 50)

    def test_upper_bound(self):
        plugin = RecallPlugin(None, {"track_count": 99999, "max_recall_count": 9999})
        self.assertEqual(plugin._max_recall, 100)
        self.assertEqual(plugin._msg_tracker.default_factory().maxlen, 500)


class BotPatchTest(unittest.TestCase):
    def test_tracking_and_terminate_restore(self):
        plugin = RecallPlugin(None, {})
        bot = FakeBot()

        plugin._set_bot(bot)
        self.assertTrue(bot._recall_patched)
        self.assertIs(bot._recall_plugin_ref, plugin)

        asyncio.run(bot.call_action("send_group_msg", group_id=555, message=[]))
        asyncio.run(bot.call_action("send_private_msg", user_id=777, message=[]))
        self.assertEqual(list(plugin._msg_tracker["g:555"]), [100])
        self.assertEqual(list(plugin._msg_tracker["p:777"]), [101])

        asyncio.run(plugin.terminate())
        # 绑定方法每次访问都是新对象，比 __func__ 才能确认真被还原
        self.assertIs(bot.call_action.__func__, FakeBot.call_action)
        for attr in ("_recall_patched", "_recall_original_call_action", "_recall_plugin_ref"):
            self.assertFalse(hasattr(bot, attr), attr)
        self.assertEqual(len(plugin._msg_tracker), 0)

        # 还原后不再追踪
        asyncio.run(bot.call_action("send_group_msg", group_id=555, message=[]))
        self.assertEqual(len(plugin._msg_tracker), 0)

    def test_terminate_keeps_newer_owner(self):
        old = RecallPlugin(None, {})
        new = RecallPlugin(None, {})
        bot = FakeBot()
        old._set_bot(bot)
        new._set_bot(bot)  # 热重载后新实例接管

        asyncio.run(old.terminate())
        self.assertIs(bot._recall_plugin_ref, new)
        self.assertTrue(bot._recall_patched)


class QuotedSenderTest(unittest.TestCase):
    class _Event:
        class message_obj:  # 没有 self_id 字段的适配器
            pass

        def get_sender_id(self):
            return "1"

    class _Reply:
        sender_id = "10000"

    def test_self_id_falls_back_to_bot(self):
        plugin = RecallPlugin(None, {})
        bot = FakeBot()
        result = asyncio.run(plugin._quoted_message_is_from_bot(self._Event(), bot, self._Reply()))
        self.assertTrue(result)

    def test_other_sender_rejected(self):
        plugin = RecallPlugin(None, {})
        bot = FakeBot()

        class Reply:
            sender_id = "22222"

        result = asyncio.run(plugin._quoted_message_is_from_bot(self._Event(), bot, Reply()))
        self.assertFalse(result)


if __name__ == "__main__":
    unittest.main()