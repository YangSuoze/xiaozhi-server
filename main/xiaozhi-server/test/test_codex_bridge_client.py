import importlib.util
from pathlib import Path

BRIDGE_PATH = Path(__file__).resolve().parents[3] / "tools" / "codex_voice_bridge.py"
SPEC = importlib.util.spec_from_file_location("codex_voice_bridge", BRIDGE_PATH)
bridge_module = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(bridge_module)


def test_cloud_client_rejects_public_plain_http():
    try:
        bridge_module.CloudClient("http://example.com:8003", "token", "mac-home")
    except ValueError as exc:
        assert "HTTPS or an SSH tunnel" in str(exc)
    else:
        raise AssertionError("public plain HTTP must be rejected")


def test_local_tunnel_bypasses_environment_proxy(monkeypatch):
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:9")
    client = bridge_module.CloudClient("http://127.0.0.1:18003", "token", "mac-home")
    assert not any(
        isinstance(handler, bridge_module.urllib.request.ProxyHandler)
        and handler.proxies
        for handler in client._opener.handlers
    )


def test_app_server_notifications_become_voice_events():
    events = []
    client = bridge_module.AppServerClient("codex", events.append)
    client._monitored_threads.add("thread-a")

    client._handle_notification(
        "thread/status/changed",
        {
            "threadId": "thread-a",
            "status": {"type": "active", "activeFlags": ["waitingOnUserInput"]},
        },
    )
    client._handle_notification(
        "item/agentMessage/delta",
        {
            "threadId": "thread-a",
            "turnId": "turn-1",
            "delta": "修改已完成",
        },
    )
    client._handle_notification(
        "turn/completed",
        {"threadId": "thread-a", "turn": {"id": "turn-1", "items": []}},
    )

    assert events[0]["type"] == "thread_status"
    assert events[1] == {
        "type": "turn_completed",
        "thread_id": "thread-a",
        "turn_id": "turn-1",
        "status": "completed",
        "summary": "修改已完成",
    }


def test_user_input_response_uses_question_ids():
    events = []
    sent = []
    client = bridge_module.AppServerClient("codex", events.append)
    client._send = sent.append
    request = {
        "id": 17,
        "method": "item/tool/requestUserInput",
        "params": {
            "threadId": "thread-a",
            "turnId": "turn-1",
            "questions": [{"id": "gap", "question": "使用哪个间隙？", "options": None}],
        },
    }
    client._handle_message(request)

    assert events[0]["question_ids"] == ["gap"]
    result = client.respond_request(
        {"request_id": 17, "answer": "0.3毫米", "approved": None}
    )
    assert result == {"accepted": True}
    assert sent == [
        {"id": 17, "result": {"answers": {"gap": {"answers": ["0.3毫米"]}}}}
    ]
