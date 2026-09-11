import asyncio
import os
import tempfile
import time
import unittest
from pathlib import Path

from core.music_cache import MusicCache


class MusicCacheTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.temp_dir.cleanup()

    def make_cache(self, **overrides):
        options = {
            "max_bytes": 4096,
            "max_files": 2,
            "ttl_seconds": 3600,
            "max_file_bytes": 4096,
            "protect_seconds": 60,
            "cleanup_interval_seconds": 0,
        }
        options.update(overrides)
        return MusicCache(self.temp_dir.name, **options)

    @staticmethod
    def write_track(cache, key, size=1500, age_seconds=0):
        path = cache.path_for(key)
        path.write_bytes(b"a" * size)
        timestamp = time.time() - age_seconds
        os.utime(path, (timestamp, timestamp))
        return path

    def test_cache_key_never_uses_untrusted_song_name(self):
        key = MusicCache.key_for("../../etc/passwd")
        self.assertRegex(key, r"^[0-9a-f]{32}$")
        self.assertNotIn("/", key)

    def test_cleanup_evicts_oldest_file_to_enforce_limits(self):
        cache = self.make_cache(max_files=2, max_bytes=4096)
        oldest = self.write_track(cache, "oldest", age_seconds=30)
        middle = self.write_track(cache, "middle", age_seconds=20)
        newest = self.write_track(cache, "newest", age_seconds=10)

        result = cache.cleanup(force=True)

        self.assertEqual(result["removed_files"], 1)
        self.assertFalse(oldest.exists())
        self.assertTrue(middle.exists())
        self.assertTrue(newest.exists())

    def test_cleanup_keeps_protected_playing_file(self):
        cache = self.make_cache(max_files=1, max_bytes=2000)
        playing = self.write_track(cache, "playing", age_seconds=30)
        other = self.write_track(cache, "other", age_seconds=20)
        cache.protect(playing)

        cache.cleanup(force=True)

        self.assertTrue(playing.exists())
        self.assertFalse(other.exists())

    def test_expired_file_and_abandoned_partial_are_removed(self):
        cache = self.make_cache(ttl_seconds=60)
        expired = self.write_track(cache, "expired", age_seconds=120)
        partial = Path(self.temp_dir.name) / ".download.part"
        partial.write_bytes(b"partial")
        old = time.time() - cache.PARTIAL_FILE_TTL_SECONDS - 1
        os.utime(partial, (old, old))

        cache.cleanup(force=True)

        self.assertFalse(expired.exists())
        self.assertFalse(partial.exists())

    def test_commit_evicts_old_entry_before_exceeding_hard_limit(self):
        cache = self.make_cache(max_files=1, max_bytes=2500)
        old = self.write_track(cache, "old", size=1500, age_seconds=30)
        incoming = Path(self.temp_dir.name) / ".incoming.part"
        incoming.write_bytes(b"b" * 1500)

        final_path = Path(cache.commit(incoming, "new"))

        self.assertFalse(old.exists())
        self.assertTrue(final_path.exists())
        self.assertLessEqual(final_path.stat().st_size, cache.max_bytes)

    def test_commit_rejects_growth_when_active_file_occupies_capacity(self):
        cache = self.make_cache(max_files=1, max_bytes=2500)
        playing = self.write_track(cache, "playing", size=1500)
        cache.protect(playing)
        incoming = Path(self.temp_dir.name) / ".incoming.part"
        incoming.write_bytes(b"b" * 1500)

        with self.assertRaisesRegex(ValueError, "currently in use"):
            cache.commit(incoming, "new")

        self.assertTrue(playing.exists())
        self.assertTrue(incoming.exists())

    async def test_download_lock_deduplicates_and_releases_keys(self):
        cache = self.make_cache()
        active = 0
        peak = 0

        async def worker():
            nonlocal active, peak
            async with cache.download_lock("same-song"):
                active += 1
                peak = max(peak, active)
                await asyncio.sleep(0.01)
                active -= 1

        await asyncio.gather(worker(), worker())

        self.assertEqual(peak, 1)
        self.assertEqual(cache._key_locks, {})
