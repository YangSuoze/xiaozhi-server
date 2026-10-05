"""The rehearsal must bypass LLMs and preserve the scripted reply order."""

import asyncio
import queue
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from core.connection import ConnectionHandler
from core.handle.intentHandler import handle_user_intent
from core.handle.sendAudioHandle import sendAudioMessage
from core.paper_podcast.fixed_demo import FixedPodcastDemo, load_script
from core.providers.tts.dto.dto import SentenceType


class FixedPodcastDemoTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.llm = Mock()
        self.intent = Mock()
        self.intent.detect_intent = AsyncMock(return_value=None)
        self.conn = ConnectionHandler(
            {"exit_commands": [], "wakeup_words": []},
            None, None, self.llm, None, self.intent,
        )
        self.conn.loop = asyncio.get_running_loop()
        self.conn.intent_type = "intent_llm"
        self.conn.tts = SimpleNamespace(
            tts_text_queue=queue.Queue(),
            tts_one_sentence=Mock(),
            tts_audio_first_sentence=False,
        )
        self.conn.websocket = SimpleNamespace(send=AsyncMock())
        self.conn.executor.submit = lambda func, *args: func(*args)
        self.wakeup = patch(
            "core.handle.intentHandler.checkWakeupWords", new=AsyncMock(return_value=False)
        )
        self.stt = patch(
            "core.handle.intentHandler.send_stt_message", new=AsyncMock()
        )
        self.bgm = patch("core.paper_podcast.fixed_demo.set_bgm_enabled")
        self.wakeup.start()
        self.stt.start()
        self.bgm.start()

    async def asyncTearDown(self):
        self.bgm.stop()
        self.stt.stop()
        self.wakeup.stop()
        self.conn.executor.shutdown(wait=True)

    async def finish_spoken_line(self):
        await sendAudioMessage(self.conn, SentenceType.LAST, [], None)

    async def test_every_user_turn_plays_the_next_exact_line_without_llm(self):
        turns = load_script()
        self.assertEqual(len(turns), 9)

        self.assertTrue(await handle_user_intent(self.conn, "进入播客模式。"))
        self.assertTrue(FixedPodcastDemo.is_active(self.conn))
        self.assertFalse(self.conn.close_after_chat)
        self.assertEqual(self.conn.tts.tts_one_sentence.call_args.kwargs["content_detail"], turns[0]["ai"])

        for turn in turns[1:]:
            await self.finish_spoken_line()
            self.assertTrue(await handle_user_intent(self.conn, turn["human"]))
            self.assertEqual(
                self.conn.tts.tts_one_sentence.call_args.kwargs["content_detail"],
                turn["ai"],
            )

        await self.finish_spoken_line()
        self.assertFalse(FixedPodcastDemo.is_active(self.conn))
        self.assertEqual(self.conn.tts.tts_one_sentence.call_count, len(turns))
        self.intent.detect_intent.assert_not_awaited()
        self.llm.response.assert_not_called()
        self.assertFalse(await handle_user_intent(self.conn, "你好"))
        self.intent.detect_intent.assert_awaited_once()

    async def test_playback_ignores_early_speech_and_exit_restores_normal_mode(self):
        self.conn.close_after_chat = True
        self.assertTrue(await handle_user_intent(self.conn, "进入播客模式"))
        self.assertTrue(await handle_user_intent(self.conn, load_script()[1]["human"]))
        self.assertEqual(self.conn.tts.tts_one_sentence.call_count, 1)

        await self.finish_spoken_line()
        self.assertTrue(await handle_user_intent(self.conn, "退出播客模式"))
        self.assertFalse(FixedPodcastDemo.is_active(self.conn))
        self.assertTrue(self.conn.close_after_chat)
        self.assertEqual(
            self.conn.tts.tts_one_sentence.call_args.kwargs["content_detail"],
            "已退出播客模式。",
        )


if __name__ == "__main__":
    unittest.main()
