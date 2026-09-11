"""Online music search, bounded download cache, and playback orchestration."""

import asyncio
import json
import os
import random
import re
import threading
from pathlib import Path
from urllib.parse import quote

import aiohttp

from core.handle.sendAudioHandle import send_stt_message
from core.music_cache import MusicCache
from core.providers.tts.dto.dto import ContentType, SentenceType, TTSMessageDTO
from core.utils.dialogue import Message


TAG = __name__
PROJECT_DIR = Path(__file__).resolve().parents[2]

MUSIC_API_SEARCH = (
    "https://music.163.com/api/search/get/web?csrf_token=&hltplt=STR_ID"
    "&s={keyword}&type=1&offset=0&total=true&limit=15"
)
MUSIC_API_SONG_URL = (
    "https://music.163.com/song/media/outer/url?id={song_id}.mp3"
)
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 "
        "Safari/537.36"
    ),
    "Referer": "https://music.163.com/",
}

_CACHE_INSTANCES = {}
_CACHE_INSTANCES_LOCK = threading.Lock()


def _bounded_number(value, default, minimum, maximum):
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    return min(max(number, minimum), maximum)


def _music_config(conn):
    return conn.config.get("plugins", {}).get("play_music", {})


def _get_music_cache(conn):
    config = _music_config(conn)
    cache_dir = Path(config.get("cache_dir", "./data/music_cache")).expanduser()
    if not cache_dir.is_absolute():
        cache_dir = PROJECT_DIR / cache_dir

    max_size_mb = _bounded_number(
        config.get("cache_max_size_mb"), 256, 16, 10240
    )
    max_files = _bounded_number(config.get("cache_max_files"), 30, 1, 10000)
    ttl_hours = _bounded_number(config.get("cache_ttl_hours"), 24, 1, 8760)
    max_file_mb = _bounded_number(
        config.get("cache_max_file_size_mb"), 50, 1, 1024
    )
    protect_seconds = _bounded_number(
        config.get("cache_protect_seconds"), 3600, 60, 86400
    )
    cleanup_interval = _bounded_number(
        config.get("cache_cleanup_interval_seconds"), 300, 0, 86400
    )

    cache_key = (
        str(cache_dir.resolve()),
        max_size_mb,
        max_files,
        ttl_hours,
        max_file_mb,
        protect_seconds,
        cleanup_interval,
    )
    with _CACHE_INSTANCES_LOCK:
        cache = _CACHE_INSTANCES.get(cache_key)
        if cache is None:
            cache = MusicCache(
                cache_dir,
                max_bytes=max_size_mb * 1024 * 1024,
                max_files=max_files,
                ttl_seconds=ttl_hours * 3600,
                max_file_bytes=max_file_mb * 1024 * 1024,
                protect_seconds=protect_seconds,
                cleanup_interval_seconds=cleanup_interval,
            )
            _CACHE_INSTANCES[cache_key] = cache
        return cache


async def search_music_online(conn, keyword, userintent):
    """Search online and ask the configured LLM to select one playable result."""
    try:
        url = MUSIC_API_SEARCH.format(keyword=quote(keyword, safe=""))
        conn.logger.bind(tag=TAG).debug(f"正在搜索音乐: {keyword}")
        timeout = aiohttp.ClientTimeout(total=10, connect=5, sock_read=5)

        async with aiohttp.ClientSession(headers=HEADERS, timeout=timeout) as session:
            async with session.get(url) as response:
                if response.status != 200:
                    conn.logger.bind(tag=TAG).warning(
                        f"音乐搜索失败，状态码: {response.status}"
                    )
                    return None
                data = await response.json(content_type=None)

        songs = data.get("result", {}).get("songs", [])
        playable_songs = [song for song in songs if song.get("fee") != 1]
        if not playable_songs:
            return None

        candidates = []
        songs_map = {}
        for song in playable_songs:
            song_id = song.get("id")
            if song_id is None:
                continue
            candidates.append(
                {
                    "id": song_id,
                    "song": song.get("name"),
                    "artist": ",".join(
                        artist.get("name", "")
                        for artist in song.get("artists", [])
                    ),
                    "album": song.get("album", {}).get("name", "未知专辑"),
                }
            )
            songs_map[song_id] = song

        if not candidates:
            return None

        system_prompt = (
            "你是一个音乐播放助手。请从候选列表中选出最符合用户意图的一首歌曲。"
            "分析用户是否要求翻唱、现场版、原唱或特定歌手；没有特殊要求时优先原唱。"
            "只返回所选歌曲的数字 ID，不要返回解释。"
        )
        user_prompt = (
            f"用户意图: {userintent or keyword}\n"
            f"候选歌曲: {json.dumps(candidates, ensure_ascii=False)}\n"
            "请选择最佳匹配的歌曲 ID："
        )
        llm_timeout = _bounded_number(
            _music_config(conn).get("llm_timeout_seconds"), 10, 1, 60
        )

        try:
            llm_response = await asyncio.wait_for(
                asyncio.to_thread(
                    conn.llm.response_no_stream,
                    system_prompt=system_prompt,
                    user_prompt=user_prompt,
                ),
                timeout=llm_timeout,
            )
            match = re.search(r"\d+", str(llm_response))
            if match:
                selected_id = int(match.group())
                selected = songs_map.get(selected_id)
                if selected is not None:
                    conn.logger.bind(tag=TAG).info(
                        f"LLM 选中歌曲: {selected.get('name')} (ID:{selected_id})"
                    )
                    return _format_song_result(selected)
            conn.logger.bind(tag=TAG).warning("LLM 选歌结果无效，使用首个可播结果")
        except asyncio.TimeoutError:
            conn.logger.bind(tag=TAG).warning("LLM 选歌超时，使用首个可播结果")
        except Exception as error:
            conn.logger.bind(tag=TAG).warning(
                f"LLM 选歌失败，使用首个可播结果: {error}"
            )

        return _format_song_result(playable_songs[0])
    except Exception as error:
        conn.logger.bind(tag=TAG).error(f"搜索音乐失败: {error}")
        return None


def _format_song_result(song):
    song_id = song.get("id")
    name = song.get("name")
    artists = ",".join(
        artist.get("name", "") for artist in song.get("artists", [])
    )
    return (
        name,
        artists,
        MUSIC_API_SONG_URL.format(song_id=song_id),
        song_id,
    )


def _flush_and_close(file_handle):
    file_handle.flush()
    os.fsync(file_handle.fileno())
    file_handle.close()


async def download_music(conn, url, song_id, real_name, artist):
    """Download one track through a bounded, atomic on-disk cache."""
    cache = _get_music_cache(conn)
    key = cache.key_for(song_id, url)

    async with cache.download_lock(key):
        cached_path = cache.get(key)
        if cached_path:
            await asyncio.to_thread(cache.cleanup)
            conn.logger.bind(tag=TAG).info(
                f"命中音乐缓存: {real_name} - {artist}"
            )
            return cached_path

        await asyncio.to_thread(cache.cleanup)
        temporary_path = cache.temporary_path_for(key)
        file_handle = None
        try:
            config = _music_config(conn)
            timeout_seconds = _bounded_number(
                config.get("download_timeout_seconds"), 45, 5, 300
            )
            timeout = aiohttp.ClientTimeout(
                total=timeout_seconds, connect=10, sock_read=15
            )
            conn.logger.bind(tag=TAG).info(
                f"开始下载音乐: {real_name} - {artist}"
            )

            async with aiohttp.ClientSession(
                headers=HEADERS, timeout=timeout
            ) as session:
                async with session.get(url) as response:
                    if response.status != 200:
                        conn.logger.bind(tag=TAG).error(
                            f"音乐下载失败，状态码: {response.status}"
                        )
                        return None

                    content_type = response.headers.get("Content-Type", "").lower()
                    if "text/html" in content_type or "application/json" in content_type:
                        conn.logger.bind(tag=TAG).error("音乐下载链接返回了非音频内容")
                        return None

                    content_length = response.headers.get("Content-Length")
                    if content_length:
                        try:
                            if int(content_length) > cache.max_file_bytes:
                                conn.logger.bind(tag=TAG).error(
                                    "音乐文件超过单文件大小上限"
                                )
                                return None
                        except ValueError:
                            pass

                    file_handle = await asyncio.to_thread(
                        open, temporary_path, "xb"
                    )
                    downloaded_bytes = 0
                    async for chunk in response.content.iter_chunked(64 * 1024):
                        if not chunk:
                            continue
                        downloaded_bytes += len(chunk)
                        if downloaded_bytes > cache.max_file_bytes:
                            raise ValueError("音乐文件超过单文件大小上限")
                        await asyncio.to_thread(file_handle.write, chunk)

            await asyncio.to_thread(_flush_and_close, file_handle)
            file_handle = None
            final_path = await asyncio.to_thread(cache.commit, temporary_path, key)
            cleanup_result = await asyncio.to_thread(cache.cleanup, True)
            if cleanup_result["removed_files"]:
                conn.logger.bind(tag=TAG).info(
                    "音乐缓存清理完成: "
                    f"删除 {cleanup_result['removed_files']} 个文件，"
                    f"释放 {cleanup_result['removed_bytes'] // 1024 // 1024} MB"
                )
            conn.logger.bind(tag=TAG).info(
                f"音乐下载完成: {real_name} - {artist}"
            )
            return final_path
        except Exception as error:
            conn.logger.bind(tag=TAG).error(f"音乐下载异常: {error}")
            return None
        finally:
            if file_handle is not None:
                await asyncio.to_thread(file_handle.close)
            try:
                await asyncio.to_thread(temporary_path.unlink)
            except FileNotFoundError:
                pass


def _get_play_prompt(song_name, artist):
    return random.choice(
        [
            f"为您找到 {artist} 的《{song_name}》，正在准备播放。",
            f"这就为您播放《{song_name}》。",
            f"请欣赏 {artist} 带来的《{song_name}》。",
        ]
    )


async def handle_online_music_command(conn, song_name, userintent=""):
    """Search, download, and queue one online track for playback."""
    clean_text = re.sub(r"[^\w\s]", "", song_name).strip()
    if not clean_text:
        clean_text = "热门歌曲"

    result = await search_music_online(conn, clean_text, userintent)
    if not result:
        error_text = f"很抱歉，没有找到能播放的《{clean_text}》。要不试试别的歌曲？"
        await send_stt_message(conn, error_text)
        conn.tts.tts_text_queue.put(
            TTSMessageDTO(
                conn.sentence_id,
                SentenceType.MIDDLE,
                ContentType.TEXT,
                content_detail=error_text,
            )
        )
        return

    real_name, artist, mp3_url, song_id = result
    prompt_text = _get_play_prompt(real_name, artist)
    await send_stt_message(conn, prompt_text)
    conn.dialogue.put(Message(role="assistant", content=prompt_text))

    conn.tts.tts_text_queue.put(
        TTSMessageDTO(
            sentence_id=conn.sentence_id,
            sentence_type=SentenceType.FIRST,
            content_type=ContentType.ACTION,
        )
    )
    conn.tts.tts_text_queue.put(
        TTSMessageDTO(
            conn.sentence_id,
            SentenceType.MIDDLE,
            ContentType.TEXT,
            content_detail=prompt_text,
        )
    )

    local_file_path = await download_music(
        conn, mp3_url, song_id, real_name, artist
    )
    if local_file_path:
        conn.tts.tts_text_queue.put(
            TTSMessageDTO(
                sentence_id=conn.sentence_id,
                sentence_type=SentenceType.MIDDLE,
                content_type=ContentType.FILE,
                content_file=local_file_path,
            )
        )
    else:
        failure_text = "抱歉，音乐下载失败，无法播放。要不试试其他歌曲？"
        await send_stt_message(conn, failure_text)
        conn.tts.tts_text_queue.put(
            TTSMessageDTO(
                conn.sentence_id,
                SentenceType.MIDDLE,
                ContentType.TEXT,
                content_detail=failure_text,
            )
        )

    if conn.intent_type == "intent_llm":
        conn.tts.tts_text_queue.put(
            TTSMessageDTO(conn.sentence_id, SentenceType.LAST, ContentType.ACTION)
        )
