"""Application service for voice-controlled Codex tasks."""

from __future__ import annotations

import asyncio
import re
import threading
import time
from pathlib import Path
from typing import Any

from config.logger import setup_logging
from core.connection_manager import connection_manager

from .store import CodexStore

TAG = __name__
PROJECT_DIR = Path(__file__).resolve().parents[2]

STATUS_TEXT = {
    "unknown": "暂时无法确定",
    "not_loaded": "尚未加载",
    "idle": "空闲，等待新指令",
    "active": "正在执行",
    "waiting_user_input": "等待你的回答",
    "waiting_approval": "等待操作确认",
    "completed": "本轮已经完成",
    "failed": "执行失败",
    "interrupted": "已经中断",
    "system_error": "Codex发生系统错误",
}


def _short_text(value: Any, limit: int = 300) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def _normalize_status(value: Any) -> str:
    if isinstance(value, dict):
        status_type = value.get("type", "unknown")
        flags = set(value.get("activeFlags") or [])
        if "waitingOnUserInput" in flags:
            return "waiting_user_input"
        if "waitingOnApproval" in flags:
            return "waiting_approval"
        value = status_type
    mapping = {
        "notLoaded": "not_loaded",
        "not_loaded": "not_loaded",
        "idle": "idle",
        "active": "active",
        "running": "active",
        "waitingOnUserInput": "waiting_user_input",
        "waiting_user_input": "waiting_user_input",
        "waitingOnApproval": "waiting_approval",
        "waiting_approval": "waiting_approval",
        "completed": "completed",
        "failed": "failed",
        "interrupted": "interrupted",
        "systemError": "system_error",
        "system_error": "system_error",
    }
    return mapping.get(str(value), "unknown")


def _normalize_progress(value: Any) -> dict[str, Any] | None:
    """Accept only the short, speakable progress fields produced by the bridge."""
    if not isinstance(value, dict):
        return None
    progress: dict[str, Any] = {}
    limits = {
        "current_action": 220,
        "recent_result": 220,
        "next_step": 180,
        "revision": 180,
        "source_message_id": 180,
    }
    for field, limit in limits.items():
        text = _short_text(value.get(field), limit)
        if text:
            progress[field] = text
    if value.get("updated_at") is not None:
        try:
            progress["updated_at"] = float(value["updated_at"])
        except (TypeError, ValueError):
            pass
    progress["needs_input"] = bool(value.get("needs_input", False))
    return progress if len(progress) > 1 or progress.get("revision") else None


def _progress_details(progress: dict[str, Any] | None) -> str:
    if not progress:
        return "Codex最近没有提供新的步骤说明。"
    parts: list[str] = []
    seen: set[str] = set()
    labels = (
        ("current_action", "正在做"),
        ("recent_result", "刚完成"),
        ("next_step", "下一步"),
    )
    for field, label in labels:
        value = _short_text(progress.get(field), 180)
        comparable = value.rstrip("。！？.!? ")
        if not comparable or comparable in seen:
            continue
        seen.add(comparable)
        parts.append(f"{label}：{comparable}。")
    return "".join(parts) or "Codex最近没有提供新的步骤说明。"


def _status_message(
    title: str, status: str, progress: dict[str, Any] | None, unchanged: bool = False
) -> str:
    text = f"任务“{_short_text(title, 40)}”当前状态：{STATUS_TEXT[status]}。"
    if unchanged:
        return text + "当前步骤没有变化。" + _progress_details(progress)
    return text + _progress_details(progress)


class CodexControlService:
    """Coordinates device conversations, bridge jobs and status notifications."""

    def __init__(
        self,
        config: dict[str, Any],
        store: CodexStore | None = None,
        logger: Any | None = None,
    ):
        self.config = config
        self.settings = config.get("codex_bridge", {})
        database_path = Path(self.settings.get("database_path", "data/codex_bridge.db"))
        if not database_path.is_absolute():
            database_path = PROJECT_DIR / database_path
        self.store = store or CodexStore(database_path)
        self.logger = logger or setup_logging()
        self._running = False
        self._task: asyncio.Task | None = None

    @property
    def enabled(self) -> bool:
        return bool(self.settings.get("enabled", False))

    @property
    def bridge_token(self) -> str:
        return str(self.settings.get("token", "")).strip()

    def start(self, loop: asyncio.AbstractEventLoop) -> None:
        if not self.enabled or (self._task and not self._task.done()):
            return
        self._running = True
        self._task = loop.create_task(self._notification_loop())
        self.logger.bind(tag=TAG).info("Codex语音控制通知任务已启动")

    async def stop(self) -> None:
        self._running = False
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        self._task = None

    def _latest_bridge(self) -> dict[str, Any] | None:
        offline_seconds = max(10, int(self.settings.get("bridge_offline_seconds", 45)))
        return self.store.latest_bridge(time.time() - offline_seconds)

    def _touch_expiry(self) -> float:
        minutes = max(5, int(self.settings.get("session_ttl_minutes", 30)))
        return time.time() + minutes * 60

    def _require_enabled(self) -> str | None:
        if not self.enabled:
            return "Codex语音控制尚未启用，请先在服务器配置中开启。"
        if len(self.bridge_token) < 32 or "请替换" in self.bridge_token:
            return "Codex桥接密钥尚未配置。"
        return None

    def enter_mode(self, device_id: str) -> str:
        unavailable = self._require_enabled()
        if unavailable:
            return unavailable
        bridge = self._latest_bridge()
        bridge_id = bridge["bridge_id"] if bridge else None
        limit = min(5, max(1, int(self.settings.get("recent_task_limit", 3))))
        self.store.patch_session(
            device_id,
            active=True,
            stage="discovering",
            bridge_id=bridge_id,
            selected_thread_id=None,
            selected_thread_title=None,
            selected_thread_host_id=None,
            recent_threads=[],
            task_status="unknown",
            task_progress=None,
            current_turn_id=None,
            pending_request=None,
            announcements_enabled=False,
            next_announcement_at=None,
            last_announced_progress_revision=None,
            announcement_prompted=False,
            expires_at=self._touch_expiry(),
        )
        self.store.create_job(
            device_id,
            "list_threads",
            {"limit": limit},
            bridge_id=bridge_id,
        )
        if bridge:
            return "已经进入Codex模式，正在让电脑读取最近活跃的任务。"
        return "已经进入Codex模式，但电脑当前离线。我会等待电脑连接后读取任务。"

    def repeat_tasks(self, device_id: str) -> str:
        session = self.store.get_session(device_id)
        threads = session.get("recent_threads") or []
        if not threads:
            return "还没有收到任务列表，请稍等电脑连接。"
        return self._format_thread_list(threads)

    @staticmethod
    def _format_thread_list(threads: list[dict[str, Any]]) -> str:
        parts = ["最近活跃的任务有："]
        for index, thread in enumerate(threads, 1):
            parts.append(f"第{index}个，{_short_text(thread.get('title'), 40)}。")
        parts.append("请选择一个任务。")
        return "".join(parts)

    @staticmethod
    def _selection_index(selection: str) -> int | None:
        normalized = selection.strip().lower()
        chinese_numbers = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5}
        match = re.search(r"(?:第\s*)?([一二三四五1-5])(?:\s*个)?", normalized)
        if not match:
            return None
        token = match.group(1)
        return chinese_numbers.get(token, int(token) if token.isdigit() else None)

    def select_task(self, device_id: str, selection: str) -> str:
        session = self.store.get_session(device_id)
        if not session.get("active"):
            return "请先说切换到Codex模式。"
        threads = session.get("recent_threads") or []
        if not threads:
            return "任务列表还没有准备好，请稍等后再试。"

        selected = None
        index = self._selection_index(selection)
        if index and 1 <= index <= len(threads):
            selected = threads[index - 1]
        else:
            needle = re.sub(r"[^\w\u4e00-\u9fff]", "", selection.lower())
            matches = []
            for thread in threads:
                title = re.sub(
                    r"[^\w\u4e00-\u9fff]", "", str(thread.get("title", "")).lower()
                )
                if needle and (needle in title or title in needle):
                    matches.append(thread)
            if len(matches) == 1:
                selected = matches[0]
            elif len(matches) > 1:
                return "有多个相似任务，请使用第一个、第二个这样的序号选择。"

        if selected is None:
            return "没有找到对应任务，请说任务序号，或者让我重新播报任务列表。"

        bridge_id = session.get("bridge_id")
        self.store.patch_session(
            device_id,
            stage="monitoring",
            selected_thread_id=selected["id"],
            selected_thread_title=selected["title"],
            selected_thread_host_id=selected.get("host_id"),
            task_status=_normalize_status(selected.get("status")),
            task_progress=None,
            pending_request=None,
            expires_at=self._touch_expiry(),
        )
        self.store.create_job(
            device_id,
            "monitor_thread",
            {
                "thread_id": selected["id"],
                "title": selected["title"],
                "host_id": selected.get("host_id"),
            },
            bridge_id=bridge_id,
        )
        return f"正在连接并监控“{_short_text(selected['title'], 40)}”。"

    def get_status(self, device_id: str) -> str:
        session = self.store.get_session(device_id)
        if not session.get("active"):
            return "当前没有进入Codex模式。"
        title = session.get("selected_thread_title")
        if not title:
            return "当前还没有选择要监控的Codex任务。"
        status = _normalize_status(session.get("task_status"))
        text = _status_message(title, status, session.get("task_progress"))
        self.store.create_job(
            device_id,
            "get_status",
            {
                "thread_id": session["selected_thread_id"],
                "host_id": session.get("selected_thread_host_id"),
            },
            bridge_id=session.get("bridge_id"),
        )
        self.store.patch_session(device_id, expires_at=self._touch_expiry())
        return text

    def send_instruction(
        self,
        device_id: str,
        text: str,
        delivery_mode: str = "after_current",
    ) -> str:
        session = self.store.get_session(device_id)
        if not session.get("active") or not session.get("selected_thread_id"):
            return "请先进入Codex模式并选择一个任务。"
        instruction = _short_text(
            text, int(self.settings.get("max_instruction_chars", 4000))
        )
        if not instruction:
            return "没有识别到要发送的指令，请再说一次。"

        pending = session.get("pending_request")
        if pending:
            if pending.get("answering"):
                return "你的回答正在发送，请稍等电脑确认。"
            return self._answer_pending_request(
                device_id, session, pending, instruction
            )

        status = _normalize_status(session.get("task_status"))
        steer = delivery_mode == "steer"
        blocked = (
            status
            in {
                "active",
                "waiting_approval",
                "waiting_user_input",
            }
            and not steer
        )
        self.store.create_job(
            device_id,
            "send_message",
            {
                "thread_id": session["selected_thread_id"],
                "host_id": session.get("selected_thread_host_id"),
                "text": instruction,
                "mode": "steer" if steer else "start",
                "expected_turn_id": session.get("current_turn_id"),
            },
            bridge_id=session.get("bridge_id"),
            blocked=blocked,
        )
        self.store.patch_session(device_id, expires_at=self._touch_expiry())
        if blocked:
            return "Codex当前正在工作，指令已经排队，会在本轮完成后发送。"
        return "正在把指令发送给Codex。电脑确认接收后我会告诉你。"

    def _answer_pending_request(
        self,
        device_id: str,
        session: dict[str, Any],
        pending: dict[str, Any],
        answer: str,
    ) -> str:
        request_type = pending.get("request_type")
        approved = None
        if request_type == "approval":
            if any(
                word in answer for word in ("拒绝", "不同意", "取消", "不允许", "不要")
            ):
                approved = False
            elif any(word in answer for word in ("同意", "允许", "批准", "确认")):
                approved = True
            else:
                return "Codex正在等待操作确认，请明确说批准或者拒绝。"
        self.store.create_job(
            device_id,
            "respond_request",
            {
                "thread_id": session["selected_thread_id"],
                "host_id": session.get("selected_thread_host_id"),
                "request_id": pending.get("request_id"),
                "request_type": request_type,
                "answer": answer,
                "approved": approved,
            },
            bridge_id=session.get("bridge_id"),
        )
        pending = dict(pending)
        pending["answering"] = True
        self.store.patch_session(
            device_id, pending_request=pending, expires_at=self._touch_expiry()
        )
        return "正在把你的回答发送给Codex。"

    def set_announcements(
        self, device_id: str, enabled: bool, interval_minutes: int | None = None
    ) -> str:
        session = self.store.get_session(device_id)
        if not session.get("selected_thread_id"):
            return "请先选择要监控的Codex任务。"
        default_minutes = max(
            1, int(self.settings.get("default_announcement_interval_minutes", 5))
        )
        minutes = max(1, min(120, int(interval_minutes or default_minutes)))
        self.store.patch_session(
            device_id,
            announcements_enabled=enabled,
            announcement_interval=minutes * 60,
            next_announcement_at=time.time() + minutes * 60 if enabled else None,
            announcement_prompted=True,
            expires_at=self._touch_expiry(),
        )
        if enabled:
            return f"好的，我会每{minutes}分钟播报一次，状态变化时也会告诉你。"
        return "好的，我会继续监控，但不再定时播报。你可以随时询问状态。"

    def stop_monitoring(self, device_id: str) -> str:
        session = self.store.get_session(device_id)
        thread_id = session.get("selected_thread_id")
        if thread_id:
            self.store.create_job(
                device_id,
                "unmonitor_thread",
                {
                    "thread_id": thread_id,
                    "host_id": session.get("selected_thread_host_id"),
                },
                bridge_id=session.get("bridge_id"),
            )
        self.store.patch_session(
            device_id,
            stage="awaiting_selection",
            selected_thread_id=None,
            selected_thread_title=None,
            selected_thread_host_id=None,
            task_status="unknown",
            task_progress=None,
            current_turn_id=None,
            pending_request=None,
            announcements_enabled=False,
            next_announcement_at=None,
            expires_at=self._touch_expiry(),
        )
        return "已经停止监控当前任务。你可以选择其他任务。"

    def exit_mode(self, device_id: str) -> str:
        session = self.store.get_session(device_id)
        thread_id = session.get("selected_thread_id")
        if thread_id:
            self.store.create_job(
                device_id,
                "unmonitor_thread",
                {
                    "thread_id": thread_id,
                    "host_id": session.get("selected_thread_host_id"),
                },
                bridge_id=session.get("bridge_id"),
            )
        self.store.patch_session(
            device_id,
            active=False,
            stage="inactive",
            selected_thread_id=None,
            selected_thread_title=None,
            selected_thread_host_id=None,
            task_status="unknown",
            task_progress=None,
            pending_request=None,
            announcements_enabled=False,
            next_announcement_at=None,
            expires_at=None,
        )
        return "已经退出Codex模式。"

    def handle_action(
        self,
        device_id: str,
        action: str,
        text: str | None = None,
        enabled: bool | None = None,
        interval_minutes: int | None = None,
        delivery_mode: str = "after_current",
    ) -> str:
        actions = {
            "enter": lambda: self.enter_mode(device_id),
            "switch_task": lambda: self.enter_mode(device_id),
            "repeat_tasks": lambda: self.repeat_tasks(device_id),
            "select_task": lambda: self.select_task(device_id, text or ""),
            "send_instruction": lambda: self.send_instruction(
                device_id, text or "", delivery_mode
            ),
            "respond": lambda: self.send_instruction(
                device_id, text or "", delivery_mode
            ),
            "status": lambda: self.get_status(device_id),
            "set_announcements": lambda: self.set_announcements(
                device_id, bool(enabled), interval_minutes
            ),
            "stop_monitor": lambda: self.stop_monitoring(device_id),
            "exit": lambda: self.exit_mode(device_id),
        }
        handler = actions.get(action)
        if handler is None:
            return "没有识别到Codex控制动作，请再说一次。"
        return handler()

    def handle_job_result(self, job: dict[str, Any]) -> None:
        device_id = job["device_id"]
        self.store.patch_session(device_id, bridge_id=job.get("leased_by"))
        if job.get("error"):
            if job["kind"] == "respond_request":
                session = self.store.get_session(device_id)
                pending = dict(session.get("pending_request") or {})
                pending.pop("answering", None)
                self.store.patch_session(device_id, pending_request=pending or None)
            self.store.queue_notification(
                device_id,
                f"电脑处理Codex请求失败：{_short_text(job['error'], 120)}",
            )
            return

        response = job.get("response") or {}
        kind = job["kind"]
        session = self.store.get_session(device_id)
        if kind == "list_threads":
            threads = []
            for item in response.get("threads", [])[:5]:
                thread_id = _short_text(item.get("id"), 128)
                title = _short_text(item.get("title") or "未命名任务", 80)
                if thread_id:
                    threads.append(
                        {
                            "id": thread_id,
                            "title": title,
                            "status": _normalize_status(item.get("status")),
                            "updated_at": item.get("updated_at"),
                            "host_id": _short_text(item.get("host_id"), 128) or None,
                        }
                    )
            if not threads:
                self.store.patch_session(device_id, stage="awaiting_tasks")
                self.store.queue_notification(
                    device_id, "电脑上没有找到可用的Codex任务。"
                )
                return
            self.store.patch_session(
                device_id, stage="awaiting_selection", recent_threads=threads
            )
            self.store.queue_notification(device_id, self._format_thread_list(threads))
        elif kind in {"monitor_thread", "get_status"}:
            status = _normalize_status(response.get("status"))
            progress = _normalize_progress(response.get("progress"))
            previous_progress = session.get("task_progress") or {}
            self.store.patch_session(
                device_id,
                task_status=status,
                current_turn_id=response.get("turn_id"),
                task_progress=progress,
            )
            if kind == "monitor_thread":
                title = session.get("selected_thread_title") or "当前任务"
                self.store.queue_notification(
                    device_id,
                    "已开始监控。" + _status_message(title, status, progress),
                )
            elif progress and progress.get("revision") != previous_progress.get(
                "revision"
            ):
                title = session.get("selected_thread_title") or "当前任务"
                self.store.queue_notification(
                    device_id,
                    "刚刚获取到最新进展。" + _status_message(title, status, progress),
                )
        elif kind == "send_message":
            self.store.patch_session(
                device_id,
                task_status="active",
                current_turn_id=response.get("turn_id"),
            )
            text = "指令已经成功送达Codex。"
            if not session.get("announcement_prompted"):
                text += "是否需要我定时播报任务状态？"
                self.store.patch_session(device_id, announcement_prompted=True)
            self.store.queue_notification(device_id, text)
        elif kind == "respond_request":
            self.store.patch_session(
                device_id, task_status="active", pending_request=None
            )
            self.store.queue_notification(device_id, "回答已经送达，Codex已继续工作。")

    def handle_bridge_event(self, event: dict[str, Any]) -> None:
        thread_id = _short_text(event.get("thread_id"), 128)
        if not thread_id:
            return
        event_type = event.get("type")
        sessions = self.store.sessions_for_thread(thread_id)
        for session in sessions:
            device_id = session["device_id"]
            previous = _normalize_status(session.get("task_status"))
            if event_type == "user_input":
                question = _short_text(event.get("question"), 240)
                self.store.patch_session(
                    device_id,
                    task_status="waiting_user_input",
                    current_turn_id=event.get("turn_id"),
                    pending_request={
                        "request_id": event.get("request_id"),
                        "request_type": "user_input",
                        "question_ids": event.get("question_ids") or [],
                    },
                )
                self.store.queue_notification(
                    device_id,
                    f"Codex正在等待你的回答。{question or '请告诉我你的回答。'}",
                )
                continue
            if event_type == "approval":
                description = _short_text(event.get("description"), 220)
                self.store.patch_session(
                    device_id,
                    task_status="waiting_approval",
                    current_turn_id=event.get("turn_id"),
                    pending_request={
                        "request_id": event.get("request_id"),
                        "request_type": "approval",
                        "approval_method": event.get("method"),
                    },
                )
                self.store.queue_notification(
                    device_id,
                    f"Codex正在等待操作确认。{description}请明确说批准或者拒绝。",
                )
                continue
            if event_type == "turn_completed":
                summary = _short_text(event.get("summary"), 300)
                outcome = _normalize_status(event.get("status") or "completed")
                if outcome not in {"failed", "interrupted"}:
                    outcome = "idle"
                progress = _normalize_progress(event.get("progress"))
                if not progress and summary:
                    progress = {
                        "recent_result": summary,
                        "revision": _short_text(event.get("turn_id"), 180),
                        "needs_input": False,
                    }
                self.store.patch_session(
                    device_id,
                    task_status=outcome,
                    task_progress=progress,
                    current_turn_id=None,
                    pending_request=None,
                )
                self.store.unblock_jobs_for_thread(thread_id)
                if session.get("announcements_enabled"):
                    message = (
                        "Codex任务已经完成。"
                        if outcome == "idle"
                        else f"Codex任务{STATUS_TEXT[outcome]}。"
                    )
                    if summary:
                        message += f"结果：{summary}"
                    self.store.queue_notification(device_id, message)
                continue
            if event_type == "thread_progress":
                status = _normalize_status(event.get("status"))
                progress = _normalize_progress(event.get("progress"))
                self.store.patch_session(
                    device_id,
                    task_status=status,
                    task_progress=progress,
                    current_turn_id=event.get("turn_id"),
                )
                if status in {"idle", "completed", "failed", "interrupted"}:
                    self.store.unblock_jobs_for_thread(thread_id)
                if session.get("announcements_enabled") and status != previous:
                    title = session.get("selected_thread_title") or "当前任务"
                    self.store.queue_notification(
                        device_id,
                        "Codex任务状态已变化。"
                        + _status_message(title, status, progress),
                    )
                continue
            if event_type == "thread_status":
                status = _normalize_status(event.get("status"))
                self.store.patch_session(
                    device_id,
                    task_status=status,
                    current_turn_id=event.get("turn_id"),
                )
                if status in {"idle", "completed", "failed", "interrupted"}:
                    self.store.unblock_jobs_for_thread(thread_id)
                if session.get("announcements_enabled") and status != previous:
                    self.store.queue_notification(
                        device_id, f"Codex任务状态更新：{STATUS_TEXT[status]}。"
                    )

    async def _notification_loop(self) -> None:
        from core.device_notifier import DeviceNotReadyError, queue_text_notification

        next_cleanup_at = 0.0
        while self._running:
            try:
                now = time.monotonic()
                if now >= next_cleanup_at:
                    self.store.cleanup(int(self.settings.get("retention_hours", 72)))
                    next_cleanup_at = now + 3600
                for session in self.store.due_periodic_sessions():
                    title = session.get("selected_thread_title") or "当前任务"
                    status = _normalize_status(session.get("task_status"))
                    progress = session.get("task_progress") or {}
                    unchanged = bool(progress.get("revision")) and progress.get(
                        "revision"
                    ) == session.get("last_announced_progress_revision")
                    text = _status_message(title, status, progress, unchanged)
                    self.store.queue_notification(session["device_id"], text, 900)
                    self.store.advance_periodic_announcement(session["device_id"])

                for _ in range(10):
                    notification = self.store.claim_notification()
                    if notification is None:
                        break
                    connection = connection_manager.get_handler(
                        notification["device_id"]
                    )
                    try:
                        await queue_text_notification(connection, notification["text"])
                    except DeviceNotReadyError as exc:
                        self.store.finish_notification(
                            notification["notification_id"], False, str(exc)
                        )
                    except Exception as exc:  # noqa: BLE001 - keep notifier alive
                        self.logger.bind(tag=TAG).error(f"Codex状态播报失败: {exc}")
                        self.store.finish_notification(
                            notification["notification_id"], False, str(exc)
                        )
                    else:
                        self.store.finish_notification(
                            notification["notification_id"], True
                        )
                await asyncio.sleep(1)
            except asyncio.CancelledError:
                break
            except Exception as exc:  # noqa: BLE001 - keep manager alive
                self.logger.bind(tag=TAG).error(f"Codex通知循环异常: {exc}")
                await asyncio.sleep(1)


_service: CodexControlService | None = None
_service_lock = threading.Lock()


def get_codex_control_service(config: dict[str, Any]) -> CodexControlService:
    global _service
    with _service_lock:
        if _service is None:
            _service = CodexControlService(config)
        return _service
