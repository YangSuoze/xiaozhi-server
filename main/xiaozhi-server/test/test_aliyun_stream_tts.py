"""CosyVoice 会话时序回归测试；使用假 WebSocket，不调用付费 API。"""

import asyncio
import json
import queue
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from websockets.exceptions import ConnectionClosedError

from core.providers.tts.aliyun_stream import TTSProvider
from core.providers.tts.dto.dto import SentenceType


class FakeWebSocket:
    def __init__(self, auto_start=False):
        self.auto_start = auto_start
        self.messages = asyncio.Queue()
        self.sent = []
        self.start_sent = asyncio.Event()
        self.closed = False

    def event(self, name, **header):
        self.messages.put_nowait(json.dumps({"header": {"name": name, **header}}))

    async def send(self, raw):
        message = json.loads(raw)
        self.sent.append(message)
        name = message["header"]["name"]
        if name == "StartSynthesis":
            self.start_sent.set()
            if self.auto_start:
                self.event("SynthesisStarted")
        elif name == "StopSynthesis":
            self.messages.put_nowait(b"pcm")
            self.event("SynthesisCompleted")

    async def recv(self):
        message = await self.messages.get()
        if isinstance(message, Exception):
            raise message
        return message

    async def close(self):
        self.closed = True


class AliyunStreamTTSTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        # 绕过配置、Token 获取和本地编码器初始化，只替换网络和编码依赖。
        self.provider = p = TTSProvider.__new__(TTSProvider)
        p.conn = SimpleNamespace(
            client_abort=False, stop_event=threading.Event(), tts_MessageText=None
        )
        p.ws = FakeWebSocket()
        p._ensure_connection = AsyncMock(return_value=p.ws)
        p._monitor_task = None
        p._session_started = None
        p._audio_session_ended = True
        p.before_stop_play_files = []
        p.tts_audio_queue = queue.Queue()
        p.task_id = "test-task"
        p.appkey = "test-app"
        p.voice = "test-voice"
        p.format = "pcm"
        p.sample_rate = 16000
        p.volume = 50
        p.speech_rate = p.pitch_rate = 0
        p.opus_encoder = SimpleNamespace(
            encode_pcm_to_opus_stream=lambda pcm, end, callback: callback(b"opus")
        )

    async def asyncTearDown(self):
        await self.provider.close()

    def audio_types(self):
        return [event[0] for event in self.provider.tts_audio_queue.queue]

    async def test_immediate_tool_reply_waits_for_synthesis_started(self):
        p = self.provider

        async def speak():
            await p.start_session(p.task_id)
            await p.text_to_speak("论文播客开始。", None)
            await p.finish_session(p.task_id)

        speaking = asyncio.create_task(speak())
        await p.ws.start_sent.wait()
        self.assertFalse(speaking.done())
        self.assertEqual([m["header"]["name"] for m in p.ws.sent], ["StartSynthesis"])
        p.ws.event("SynthesisStarted")
        await asyncio.wait_for(speaking, 1)
        self.assertEqual(
            [m["header"]["name"] for m in p.ws.sent],
            ["StartSynthesis", "RunSynthesis", "StopSynthesis"],
        )
        self.assertEqual(self.audio_types(), [SentenceType.FIRST, SentenceType.MIDDLE, SentenceType.LAST])

    async def test_start_rejection_reports_reason_and_ends_playback_once(self):
        p = self.provider
        ws = p.ws
        starting = asyncio.create_task(p.start_session(p.task_id))
        await ws.start_sent.wait()
        ws.event("TaskFailed", status=40000002, status_text="MESSAGE_INVALID:ROUTING")
        with self.assertRaisesRegex(RuntimeError, "40000002.*ROUTING"):
            await asyncio.wait_for(starting, 1)
        self.assertEqual(self.audio_types(), [SentenceType.LAST])
        self.assertTrue(ws.closed)
        self.assertIsNone(p.ws)
        self.assertIsNone(p._monitor_task)

    async def test_connection_closed_before_start_unblocks_worker(self):
        p = self.provider
        starting = asyncio.create_task(p.start_session(p.task_id))
        await p.ws.start_sent.wait()
        p.ws.messages.put_nowait(ConnectionClosedError(None, None))
        with self.assertRaisesRegex(RuntimeError, "会话完成前关闭"):
            await asyncio.wait_for(starting, 1)
        self.assertEqual(self.audio_types(), [SentenceType.LAST])

    async def test_start_timeout_closes_connection_and_ends_playback(self):
        p = self.provider
        ws = p.ws
        p.SESSION_START_TIMEOUT = 0.02
        with self.assertRaises(TimeoutError):
            await asyncio.wait_for(p.start_session(p.task_id), 1)
        self.assertTrue(ws.closed)
        self.assertIsNone(p._monitor_task)
        self.assertEqual(self.audio_types(), [SentenceType.LAST])

    async def test_failure_after_start_ends_playback_and_next_session_recovers(self):
        p = self.provider
        p.ws.auto_start = True
        await p.start_session(p.task_id)
        monitor = p._monitor_task
        p.ws.event("TaskFailed", status=50000000, status_text="backend failure")
        await asyncio.wait_for(monitor, 1)
        self.assertEqual(self.audio_types(), [SentenceType.FIRST, SentenceType.LAST])
        self.assertIsNone(p.ws)

        p.ws = FakeWebSocket(auto_start=True)
        await p.start_session(p.task_id)
        await p.text_to_speak("你好", None)
        await p.finish_session(p.task_id)
        self.assertEqual(self.audio_types().count(SentenceType.LAST), 2)
        self.assertIn(SentenceType.MIDDLE, self.audio_types())

    async def test_each_reused_connection_session_waits_for_its_own_ack(self):
        p = self.provider
        p.ws.auto_start = True
        await p.start_session(p.task_id)
        await p.finish_session(p.task_id)
        p.ws.auto_start = False
        p.ws.start_sent.clear()
        starting = asyncio.create_task(p.start_session(p.task_id))
        await p.ws.start_sent.wait()
        self.assertFalse(starting.done())
        p.ws.event("SynthesisStarted")
        await asyncio.wait_for(starting, 1)
        await p.finish_session(p.task_id)
        self.assertEqual(self.audio_types().count(SentenceType.LAST), 2)

    async def test_connection_failure_also_ends_playback(self):
        p = self.provider
        p._ensure_connection.side_effect = OSError("connection failed")
        with self.assertRaisesRegex(OSError, "connection failed"):
            await p.start_session(p.task_id)
        self.assertEqual(self.audio_types(), [SentenceType.LAST])

    async def test_abort_during_start_cancels_wait_without_stale_playback(self):
        p = self.provider
        starting = asyncio.create_task(p.start_session(p.task_id))
        await p.ws.start_sent.wait()
        p.conn.client_abort = True
        await p.close()
        with self.assertRaises(asyncio.CancelledError):
            await asyncio.wait_for(starting, 1)
        self.assertEqual(self.audio_types(), [])


if __name__ == "__main__":
    unittest.main()
