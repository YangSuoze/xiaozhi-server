import json
import shutil
import subprocess
from pathlib import Path

import pytest

BRIDGE_PATH = Path(__file__).resolve().parents[3] / "tools" / "codex_voice_bridge.mjs"


def test_bridge_extracts_speakable_progress_and_hides_sensitive_text():
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is unavailable")
    script = f"""
import {{ buildProgressSnapshot, buildTaskMemoryMessage }} from {json.dumps(BRIDGE_PATH.as_uri())};
const progress = buildProgressSnapshot({{
  thread: {{ status: {{ type: "active", activeFlags: [] }} }},
  latestTurn: {{ id: "turn-1", status: "inProgress" }},
  latestAssistantMessage: {{
    id: "message-2",
    phase: "commentary",
    text: "已经定位到 token=abcdefghijklmnopqrstuvwxyz0123456789。现在正在修改 /Users/demo/project/auth.py。下一步运行登录测试。"
  }}
}});
const completedOnly = buildProgressSnapshot({{
  thread: {{ status: {{ type: "active", activeFlags: [] }} }},
  latestAssistantMessage: {{
    id: "message-3",
    phase: "commentary",
    text: "已经完成配置修改。下一步运行完整测试。"
  }}
}});
const memory = buildTaskMemoryMessage({{
  latestAssistantMessage: {{
    id: "message-4",
    phase: "final_answer",
    text: "已完成修改，token=abcdefghijklmnopqrstuvwxyz0123456789，文件在 /Users/demo/private/result.md。"
  }}
}});
process.stdout.write(JSON.stringify({{ progress, completedOnly, memory }}));
"""
    result = subprocess.run(
        [node, "--input-type=module", "-e", script],
        check=True,
        capture_output=True,
        text=True,
    )
    parsed = json.loads(result.stdout)
    progress = parsed["progress"]
    assert progress["revision"] == "message-2"
    assert "现在正在修改本地文件" in progress["current_action"]
    assert "token已隐藏" in progress["recent_result"]
    assert "下一步运行登录测试" in progress["next_step"]
    assert "/Users/" not in result.stdout
    assert "abcdefghijklmnopqrstuvwxyz" not in result.stdout
    assert parsed["completedOnly"]["current_action"] is None
    assert parsed["memory"]["message_id"] == "message-4"
    assert parsed["memory"]["phase"] == "final_answer"
    assert "token已隐藏" in parsed["memory"]["text"]
    assert "本地文件" in parsed["memory"]["text"]


def test_bridge_reads_live_commentary_from_local_rollout(tmp_path):
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is unavailable")
    rollout = tmp_path / "rollout.jsonl"
    rollout.write_text(
        json.dumps(
            {
                "type": "event_msg",
                "payload": {
                    "type": "item_completed",
                    "turn_id": "turn-live",
                    "item": {
                        "type": "AgentMessage",
                        "id": "message-live",
                        "phase": "commentary",
                        "content": [
                            {"type": "Text", "text": "正在检查云端服务启动状态。"}
                        ],
                    },
                },
            },
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )
    script = f"""
import {{ RolloutProgressReader }} from {json.dumps(BRIDGE_PATH.as_uri())};
const reader = new RolloutProgressReader({json.dumps(str(tmp_path))});
process.stdout.write(JSON.stringify(reader.snapshot(
  "thread-live", {json.dumps(str(rollout))}, "turn-live"
)));
"""
    result = subprocess.run(
        [node, "--input-type=module", "-e", script],
        check=True,
        capture_output=True,
        text=True,
    )
    snapshot = json.loads(result.stdout)
    assert snapshot["latestAssistantMessage"]["id"] == "message-live"
    assert "检查云端服务" in snapshot["latestAssistantMessage"]["text"]
