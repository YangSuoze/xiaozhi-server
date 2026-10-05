import asyncio
import json
from types import SimpleNamespace

from core.paper_podcast import bgm


class FakeWebSocket:
    def __init__(self):
        self.sent = []

    async def send(self, data):
        self.sent.append(data)


def test_music_stream_continues_while_device_listens_and_stops(tmp_path, monkeypatch):
    async def exercise():
        track = tmp_path / "track.ulaw"
        track.write_bytes(bytes(range(256)) * 8)
        monkeypatch.setattr(bgm, "TRACK", track)
        conn = SimpleNamespace(
            loop=asyncio.get_running_loop(),
            websocket=FakeWebSocket(),
            logger=SimpleNamespace(warning=lambda *_: None),
            client_listen_mode="auto",
        )

        bgm.set_enabled(conn, True)
        await asyncio.sleep(0.01)
        assert json.loads(conn.websocket.sent[0]) == {
            "type": "bgm", "state": "start", "codec": "mulaw", "sample_rate": 16000
        }
        before = sum(isinstance(item, bytes) for item in conn.websocket.sent)
        conn.client_listen_mode = "listening"
        await asyncio.sleep(0.13)
        after = sum(isinstance(item, bytes) for item in conn.websocket.sent)
        assert after > before
        assert all(
            item.startswith(bgm.MAGIC) and len(item) == len(bgm.MAGIC) + bgm.FRAME_BYTES
            for item in conn.websocket.sent if isinstance(item, bytes)
        )

        bgm.set_enabled(conn, False)
        await asyncio.sleep(0.02)
        assert {"type": "bgm", "state": "stop"} == json.loads(conn.websocket.sent[-1])
        count = len(conn.websocket.sent)
        await asyncio.sleep(0.08)
        assert len(conn.websocket.sent) == count

    asyncio.run(exercise())
