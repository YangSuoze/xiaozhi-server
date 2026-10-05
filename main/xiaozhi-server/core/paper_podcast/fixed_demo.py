"""A connection-scoped, verbatim podcast demo without LLM generation."""

import json
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from core.paper_podcast.bgm import set_enabled as set_bgm_enabled


SCRIPT_PATH = Path(__file__).parent / "content" / "fixed_demo.json"
START_COMMANDS = frozenset(("进入播客模式", "进入博客模式"))
STOP_COMMANDS = frozenset(("退出播客模式", "结束播客模式", "退出博客模式"))


@lru_cache(maxsize=1)
def load_script():
    return json.loads(SCRIPT_PATH.read_text(encoding="utf-8"))["turns"]


@dataclass
class _Session:
    next_reply: int
    original_close_after_chat: bool
    speaking: bool = True
    finishing: bool = False


class FixedPodcastDemo:
    """Each completed user utterance advances one scripted AI line."""

    @staticmethod
    def is_active(conn):
        return getattr(conn, "fixed_podcast_demo", None) is not None

    @staticmethod
    def is_speaking(conn):
        state = getattr(conn, "fixed_podcast_demo", None)
        return bool(state and state.speaking)

    @staticmethod
    def start(conn):
        if getattr(conn, "paper_podcast_active", False):
            from core.paper_podcast.service import PaperPodcastService

            PaperPodcastService().stop(conn)
        old_state = getattr(conn, "fixed_podcast_demo", None)
        original_close = (
            old_state.original_close_after_chat
            if old_state else conn.close_after_chat
        )
        conn.fixed_podcast_demo = _Session(1, original_close)
        conn.close_after_chat = False
        settings = conn.config.get("paper_podcast") or {}
        set_bgm_enabled(conn, settings.get("bgm_enabled", True))
        return load_script()[0]["ai"]

    @staticmethod
    def advance(conn):
        state = conn.fixed_podcast_demo
        turns = load_script()
        if state is None or state.speaking or state.next_reply >= len(turns):
            return None
        reply = turns[state.next_reply]["ai"]
        state.next_reply += 1
        state.speaking = True
        state.finishing = state.next_reply == len(turns)
        return reply

    @staticmethod
    def stop(conn):
        state = getattr(conn, "fixed_podcast_demo", None)
        if state is None:
            return
        conn.fixed_podcast_demo = None
        conn.close_after_chat = state.original_close_after_chat
        set_bgm_enabled(conn, False)

    @staticmethod
    def audio_complete(conn):
        state = getattr(conn, "fixed_podcast_demo", None)
        if state is None:
            return
        state.speaking = False
        if state.finishing:
            FixedPodcastDemo.stop(conn)

    @staticmethod
    def audio_interrupted(conn):
        state = getattr(conn, "fixed_podcast_demo", None)
        if state is None:
            return
        state.speaking = False
        if state.finishing:
            FixedPodcastDemo.stop(conn)
