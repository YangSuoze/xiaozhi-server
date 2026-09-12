import sqlite3
import time

from core.codex.service import CodexControlService
from core.codex.store import CodexStore


class SilentLogger:
    def bind(self, **_kwargs):
        return self

    def __getattr__(self, _name):
        return lambda *_args, **_kwargs: None


def make_service(tmp_path):
    config = {
        "codex_bridge": {
            "enabled": True,
            "token": "test-token-0123456789abcdefghijkl",
            "bridge_offline_seconds": 45,
            "job_lease_seconds": 60,
            "recent_task_limit": 3,
            "default_announcement_interval_minutes": 5,
        }
    }
    store = CodexStore(tmp_path / "codex.db")
    return CodexControlService(config, store=store, logger=SilentLogger())


def complete(service, bridge_id, job, response=None, error=None):
    completed = service.store.complete_job(bridge_id, job["job_id"], response, error)
    assert completed is not None
    service.handle_job_result(completed)
    return completed


def test_voice_session_discovers_selects_and_queues_busy_instruction(tmp_path):
    service = make_service(tmp_path)
    store = service.store
    store.heartbeat("mac-home", "home-mac", "1.0", {})

    assert "正在让电脑读取" in service.enter_mode("speaker-1")
    list_job = store.lease_job("mac-home", 60)
    assert list_job["kind"] == "list_threads"
    complete(
        service,
        "mac-home",
        list_job,
        {
            "threads": [
                {
                    "id": "thread-a",
                    "title": "设计可爱的小智外壳",
                    "status": {"type": "notLoaded"},
                    "updated_at": 100,
                },
                {
                    "id": "thread-b",
                    "title": "排查小智服务器",
                    "status": {"type": "idle"},
                    "updated_at": 90,
                },
            ]
        },
    )
    session = store.get_session("speaker-1")
    assert session["stage"] == "awaiting_selection"
    assert len(session["recent_threads"]) == 2

    assert "正在连接并监控" in service.select_task("speaker-1", "第一个")
    monitor_job = store.lease_job("mac-home", 60)
    assert monitor_job["kind"] == "monitor_thread"
    complete(
        service,
        "mac-home",
        monitor_job,
        {"status": {"type": "active", "activeFlags": []}, "turn_id": "turn-1"},
    )

    response = service.send_instruction("speaker-1", "把卡扣间隙改成0.3毫米")
    assert "已经排队" in response
    assert store.lease_job("mac-home", 60) is None

    service.handle_bridge_event(
        {
            "type": "turn_completed",
            "thread_id": "thread-a",
            "turn_id": "turn-1",
            "summary": "前一轮已经完成",
        }
    )
    instruction_job = store.lease_job("mac-home", 60)
    assert instruction_job["kind"] == "send_message"
    assert instruction_job["payload"]["text"] == "把卡扣间隙改成0.3毫米"


def test_pending_user_input_is_answered_instead_of_starting_new_turn(tmp_path):
    service = make_service(tmp_path)
    store = service.store
    store.heartbeat("mac-home", "home-mac", "1.0", {})
    store.patch_session(
        "speaker-1",
        active=True,
        stage="monitoring",
        bridge_id="mac-home",
        selected_thread_id="thread-a",
        selected_thread_title="外壳设计",
        task_status="active",
    )
    service.handle_bridge_event(
        {
            "type": "user_input",
            "thread_id": "thread-a",
            "turn_id": "turn-1",
            "request_id": 27,
            "question": "使用0.25还是0.3毫米？",
            "question_ids": ["gap"],
        }
    )

    assert "回答发送" in service.send_instruction("speaker-1", "使用0.3毫米")
    job = store.lease_job("mac-home", 60)
    assert job["kind"] == "respond_request"
    assert job["payload"]["request_id"] == 27
    assert job["payload"]["answer"] == "使用0.3毫米"
    assert store.get_session("speaker-1")["pending_request"]["answering"] is True
    complete(service, "mac-home", job, {"accepted": True})
    assert store.get_session("speaker-1")["pending_request"] is None


def test_approval_requires_explicit_decision(tmp_path):
    service = make_service(tmp_path)
    store = service.store
    store.patch_session(
        "speaker-1",
        active=True,
        bridge_id="mac-home",
        selected_thread_id="thread-a",
        selected_thread_title="服务器",
        task_status="waiting_approval",
        pending_request={"request_id": 9, "request_type": "approval"},
    )

    unclear = service.send_instruction("speaker-1", "你看着办")
    assert "明确说批准或者拒绝" in unclear
    assert store.get_session("speaker-1")["pending_request"] is not None

    denied = service.send_instruction("speaker-1", "不允许")
    assert "回答发送" in denied
    denied_job = store.lease_job("mac-home", 60)
    assert denied_job["payload"]["approved"] is False

    store.patch_session(
        "speaker-1",
        pending_request={"request_id": 10, "request_type": "approval"},
    )

    accepted = service.send_instruction("speaker-1", "批准")
    assert "回答发送" in accepted
    job = store.lease_job("mac-home", 60)
    assert job["payload"]["approved"] is True


def test_expired_lease_is_retried(tmp_path):
    store = CodexStore(tmp_path / "codex.db")
    store.create_job("speaker-1", "list_threads", {"limit": 3})
    first = store.lease_job("mac-home", 10)
    assert first["attempts"] == 1
    with sqlite3.connect(store.database_path) as connection:
        connection.execute(
            "UPDATE jobs SET lease_until=? WHERE job_id=?",
            (time.time() - 1, first["job_id"]),
        )
    second = store.lease_job("mac-home", 10)
    assert second["job_id"] == first["job_id"]
    assert second["attempts"] == 2


def test_announcement_configuration_is_bounded(tmp_path):
    service = make_service(tmp_path)
    service.store.patch_session(
        "speaker-1",
        active=True,
        selected_thread_id="thread-a",
        selected_thread_title="外壳设计",
    )
    response = service.set_announcements("speaker-1", True, 999)
    assert "120分钟" in response
    session = service.store.get_session("speaker-1")
    assert session["announcement_interval"] == 120 * 60
    assert session["next_announcement_at"] is not None
