"""Exercise both production streaming paths without cloud or audio calls."""

import asyncio
import queue
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from core.connection import ConnectionHandler
from core.paper_podcast import PaperPodcastService
from core.providers.tts.dto.dto import SentenceType


class PaperPodcastChatTests(unittest.IsolatedAsyncioTestCase):
    async def test_budget_and_streaming_are_preserved_in_both_llm_paths(self):
        for intent_type in ("intent_llm", "function_call"):
            with self.subTest(intent_type=intent_type):
                llm = Mock()
                llm.max_tokens = 500
                llm.temperature = 1.0
                llm.response.side_effect = lambda *args, **kwargs: iter(["观点。", "证据。"])
                llm.response_with_functions.side_effect = lambda *args, **kwargs: iter(
                    [("观点。", None), ("证据。", None)]
                )
                conn = ConnectionHandler({"exit_commands": []}, None, None, llm, None, None)
                conn.loop = asyncio.get_running_loop()
                conn.intent_type = intent_type
                conn.func_handler = SimpleNamespace(get_functions=lambda: [])
                conn.tts = SimpleNamespace(tts_text_queue=queue.Queue())
                conn.change_system_prompt("普通聊天角色")
                service = PaperPodcastService()
                selected = llm.response_with_functions if intent_type == "function_call" else llm.response
                try:
                    with patch("core.connection.textUtils.get_emotion", new=AsyncMock()):
                        service.start(conn)
                        await asyncio.to_thread(conn.chat, "解释一下创业策略的依据")
                        self.assertEqual(selected.call_args.kwargs["max_tokens"], 1200)
                        self.assertEqual(selected.call_args.kwargs["temperature"], 0.5)
                        types = [m.sentence_type for m in conn.tts.tts_text_queue.queue]
                        self.assertEqual(types, [SentenceType.FIRST, SentenceType.MIDDLE, SentenceType.MIDDLE, SentenceType.LAST])
                        self.assertEqual(conn.dialogue.dialogue[-1].content, "观点。证据。")
                        service.stop(conn)
                        await asyncio.to_thread(conn.chat, "你好")
                        self.assertNotIn("max_tokens", selected.call_args.kwargs)
                        self.assertNotIn("temperature", selected.call_args.kwargs)
                        self.assertEqual(llm.max_tokens, 500)
                        self.assertEqual(llm.temperature, 1.0)
                finally:
                    conn.executor.shutdown(wait=True)

    async def test_interruption_stops_podcast_text_without_waiting_for_full_answer(self):
        llm = Mock()
        conn = ConnectionHandler({"exit_commands": []}, None, None, llm, None, None)
        conn.loop = asyncio.get_running_loop()
        conn.tts = SimpleNamespace(tts_text_queue=queue.Queue())
        conn.change_system_prompt("普通聊天角色")

        def response(*args, **kwargs):
            yield "先解释基线。"
            conn.client_abort = True
            yield "这部分不应该继续播报。"
            raise AssertionError("The interrupted stream was still consumed")

        llm.response.side_effect = response
        try:
            PaperPodcastService().start(conn)
            with patch("core.connection.textUtils.get_emotion", new=AsyncMock()):
                await asyncio.to_thread(conn.chat, "详细解释")
            self.assertEqual(conn.dialogue.dialogue[-1].content, "先解释基线。")
            self.assertTrue(conn.client_abort)
            self.assertTrue(conn.paper_podcast_active)
        finally:
            conn.executor.shutdown(wait=True)


if __name__ == "__main__":
    unittest.main()
