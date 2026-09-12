import importlib.util
from pathlib import Path

INSTALLER_PATH = (
    Path(__file__).resolve().parents[3] / "tools" / "install_codex_voice_bridge.py"
)
SPEC = importlib.util.spec_from_file_location(
    "install_codex_voice_bridge", INSTALLER_PATH
)
installer = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(installer)


def make_block():
    return installer.bridge_config_block(
        Path("/Applications/ChatGPT.app/node"),
        Path("/tmp/codex_voice_bridge.mjs"),
        "http://127.0.0.1:18003",
        Path("/tmp/token"),
        Path("/Applications/ChatGPT.app/codex"),
    )


def test_bridge_config_uses_token_file_and_desktop_pipe():
    block = make_block()
    assert "--token-file" in block
    assert "CODEX_APP_TOOLS_PIPE_PATH" in block
    assert "mcp_servers.xiaozhi_codex_voice_bridge" in block
    assert '--token"' not in block


def test_codex_config_update_is_idempotent(tmp_path):
    config = tmp_path / "config.toml"
    config.write_text('model = "gpt-5.6-sol"\n', encoding="utf-8")

    assert installer.update_codex_config(config, make_block()) is True
    first = config.read_text(encoding="utf-8")
    assert installer.update_codex_config(config, make_block()) is False
    assert config.read_text(encoding="utf-8") == first
    assert first.count(installer.CONFIG_BEGIN) == 1


def test_codex_config_update_replaces_managed_block(tmp_path):
    config = tmp_path / "config.toml"
    config.write_text(
        f"setting = true\n\n{installer.CONFIG_BEGIN}\nold = true\n"
        f"{installer.CONFIG_END}\n",
        encoding="utf-8",
    )
    block = make_block().replace("18003", "19003")

    assert installer.update_codex_config(config, block) is True
    updated = config.read_text(encoding="utf-8")
    assert "old = true" not in updated
    assert "19003" in updated
