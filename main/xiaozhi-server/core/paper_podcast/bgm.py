"""Stream one podcast music bed over the authenticated device WebSocket.

The special binary prefix keeps music separate from normal Opus TTS packets.
Each following frame is 60 ms of 16 kHz mono G.711 mu-law audio.
"""

import asyncio
import json
from pathlib import Path


TRACK = Path(__file__).resolve().parents[2] / "data/podcast/lemon_summer_yoli_16k.ulaw"
FRAME_BYTES = 960
FRAME_SECONDS = 0.06
MAGIC = b"XBGMAUD1"


def set_enabled(conn, enabled: bool) -> None:
    """May be called by a synchronous tool running outside the event loop."""
    loop = getattr(conn, "loop", None)
    if loop is None or loop.is_closed():
        return

    def apply():
        task = getattr(conn, "_podcast_bgm_task", None)
        if enabled and task is not None and not task.done():
            return
        conn._podcast_bgm_generation = getattr(conn, "_podcast_bgm_generation", 0) + 1
        generation = conn._podcast_bgm_generation
        if task is not None:
            task.cancel()
        if enabled:
            conn._podcast_bgm_task = loop.create_task(_stream(conn, generation))
        else:
            conn._podcast_bgm_task = None
            loop.create_task(_send_stop(conn))

    loop.call_soon_threadsafe(apply)


async def _send_stop(conn):
    try:
        await conn.websocket.send(json.dumps({"type": "bgm", "state": "stop"}))
    except Exception:
        pass  # The device may already have disconnected.


async def _stream(conn, generation: int):
    if not TRACK.is_file() or TRACK.stat().st_size < FRAME_BYTES:
        conn.logger.warning(f"创业播客背景音乐文件不存在或无效: {TRACK}")
        return

    try:
        await conn.websocket.send(
            json.dumps({"type": "bgm", "state": "start", "codec": "mulaw", "sample_rate": 16000})
        )
        loop = asyncio.get_running_loop()
        deadline = loop.time()
        frame_number = 0
        with TRACK.open("rb") as track:
            while True:
                # Send three frames initially, then one frame every 60 ms.
                if frame_number >= 3:
                    deadline += FRAME_SECONDS
                    await asyncio.sleep(max(0, deadline - loop.time()))
                data = track.read(FRAME_BYTES)
                if len(data) < FRAME_BYTES:
                    track.seek(0)
                    data += track.read(FRAME_BYTES - len(data))
                await conn.websocket.send(MAGIC + data)
                frame_number += 1
    except asyncio.CancelledError:
        raise
    except Exception as error:
        conn.logger.warning(f"创业播客背景音乐中断: {error}")
    finally:
        if getattr(conn, "_podcast_bgm_generation", None) == generation:
            await _send_stop(conn)
