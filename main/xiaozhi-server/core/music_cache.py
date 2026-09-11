"""Bounded on-disk cache used by the online music player."""

import asyncio
import hashlib
import os
import threading
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path


class MusicCache:
    """Store downloaded tracks with TTL and LRU-style size limits.

    File modification time is the last-access timestamp. A short protection window
    prevents cleanup from removing a file while the TTS worker is still reading it.
    """

    MIN_VALID_FILE_BYTES = 1024
    PARTIAL_FILE_TTL_SECONDS = 3600

    def __init__(
        self,
        directory,
        *,
        max_bytes,
        max_files,
        ttl_seconds,
        max_file_bytes,
        protect_seconds=3600,
        cleanup_interval_seconds=300,
    ):
        self.directory = Path(directory).expanduser().resolve()
        self.max_bytes = max(1, int(max_bytes))
        self.max_files = max(1, int(max_files))
        self.ttl_seconds = max(60, int(ttl_seconds))
        self.max_file_bytes = max(
            self.MIN_VALID_FILE_BYTES, int(max_file_bytes)
        )
        self.protect_seconds = max(60, int(protect_seconds))
        self.cleanup_interval_seconds = max(0, int(cleanup_interval_seconds))

        self._state_lock = threading.RLock()
        self._protected_until = {}
        self._last_cleanup_at = 0.0
        self._key_locks = {}
        self._key_locks_guard = asyncio.Lock()

        self.directory.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def key_for(song_id, url=""):
        identity = str(song_id or url).encode("utf-8", errors="replace")
        return hashlib.sha256(identity).hexdigest()[:32]

    def path_for(self, key):
        return self.directory / f"{key}.mp3"

    def temporary_path_for(self, key):
        return self.directory / f".{key}.{uuid.uuid4().hex}.part"

    @asynccontextmanager
    async def download_lock(self, key):
        """Deduplicate concurrent downloads without retaining locks forever."""
        async with self._key_locks_guard:
            entry = self._key_locks.get(key)
            if entry is None:
                entry = {"lock": asyncio.Lock(), "users": 0}
                self._key_locks[key] = entry
            entry["users"] += 1

        await entry["lock"].acquire()
        try:
            yield
        finally:
            entry["lock"].release()
            async with self._key_locks_guard:
                entry["users"] -= 1
                if entry["users"] == 0:
                    self._key_locks.pop(key, None)

    def get(self, key):
        path = self.path_for(key)
        try:
            size = path.stat().st_size
        except FileNotFoundError:
            return None

        if size < self.MIN_VALID_FILE_BYTES or size > self.max_file_bytes:
            self._safe_unlink(path)
            return None

        now = time.time()
        os.utime(path, (now, now))
        self.protect(path, now=now)
        return str(path)

    def commit(self, temporary_path, key):
        temporary_path = Path(temporary_path)
        size = temporary_path.stat().st_size
        if size < self.MIN_VALID_FILE_BYTES:
            raise ValueError("downloaded music file is too small")
        if size > self.max_file_bytes:
            raise ValueError("downloaded music file exceeds configured limit")

        final_path = self.path_for(key)
        now = time.time()
        with self._state_lock:
            self.cleanup(force=True, now=now)
            self._make_room_for(size, final_path, now)
            os.replace(str(temporary_path), str(final_path))
            os.utime(final_path, (now, now))
            self.protect(final_path, now=now)
        return str(final_path)

    def _make_room_for(self, incoming_bytes, final_path, now):
        """Evict unprotected LRU entries before an atomic cache commit."""
        self._protected_until = {
            path: deadline
            for path, deadline in self._protected_until.items()
            if deadline > now
        }
        protected = set(self._protected_until)
        final_path = final_path.resolve()
        entries = []
        for path in self.directory.glob("*.mp3"):
            if path.resolve() == final_path:
                continue
            try:
                stat = path.stat()
            except FileNotFoundError:
                continue
            entries.append(
                (stat.st_mtime, stat.st_size, path, str(path.resolve()) in protected)
            )

        total_bytes = sum(item[1] for item in entries)
        total_files = len(entries)
        evictable = sorted(
            (item for item in entries if not item[3]), key=lambda item: item[0]
        )
        while (
            total_files + 1 > self.max_files
            or total_bytes + incoming_bytes > self.max_bytes
        ) and evictable:
            _mtime, size, path, _protected = evictable.pop(0)
            if self._safe_unlink(path):
                total_files -= 1
                total_bytes -= size

        if (
            total_files + 1 > self.max_files
            or total_bytes + incoming_bytes > self.max_bytes
        ):
            raise ValueError("music cache is full of files currently in use")

    def protect(self, path, *, now=None):
        resolved = str(Path(path).resolve())
        now = time.time() if now is None else now
        with self._state_lock:
            self._protected_until[resolved] = now + self.protect_seconds

    def cleanup(self, force=False, now=None):
        """Remove stale/old files and return cleanup statistics."""
        now = time.time() if now is None else float(now)
        with self._state_lock:
            if (
                not force
                and self.cleanup_interval_seconds
                and now - self._last_cleanup_at < self.cleanup_interval_seconds
            ):
                return {"removed_files": 0, "removed_bytes": 0, "skipped": True}
            self._last_cleanup_at = now

            self._protected_until = {
                path: deadline
                for path, deadline in self._protected_until.items()
                if deadline > now
            }
            protected = set(self._protected_until)

            removed_files = 0
            removed_bytes = 0

            for partial in self.directory.glob(".*.part"):
                try:
                    stat = partial.stat()
                except FileNotFoundError:
                    continue
                if now - stat.st_mtime >= self.PARTIAL_FILE_TTL_SECONDS:
                    if self._safe_unlink(partial):
                        removed_files += 1
                        removed_bytes += stat.st_size

            candidates = []
            protected_files = 0
            protected_bytes = 0
            for path in self.directory.glob("*.mp3"):
                try:
                    stat = path.stat()
                except FileNotFoundError:
                    continue
                if str(path.resolve()) in protected:
                    protected_files += 1
                    protected_bytes += stat.st_size
                    continue
                candidates.append((stat.st_mtime, stat.st_size, path))

            retained = []
            for mtime, size, path in candidates:
                if now - mtime >= self.ttl_seconds:
                    if self._safe_unlink(path):
                        removed_files += 1
                        removed_bytes += size
                else:
                    retained.append((mtime, size, path))

            retained.sort(key=lambda item: item[0])
            total_bytes = protected_bytes + sum(item[1] for item in retained)
            total_files = protected_files + len(retained)
            for _mtime, size, path in retained:
                if total_files <= self.max_files and total_bytes <= self.max_bytes:
                    break
                if self._safe_unlink(path):
                    removed_files += 1
                    removed_bytes += size
                    total_files -= 1
                    total_bytes -= size

            return {
                "removed_files": removed_files,
                "removed_bytes": removed_bytes,
                "skipped": False,
            }

    @staticmethod
    def _safe_unlink(path):
        try:
            Path(path).unlink()
            return True
        except FileNotFoundError:
            return False
