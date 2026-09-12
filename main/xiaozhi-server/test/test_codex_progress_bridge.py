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
import {{ buildProgressSnapshot }} from {json.dumps(BRIDGE_PATH.as_uri())};
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
process.stdout.write(JSON.stringify({{ progress, completedOnly }}));
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
