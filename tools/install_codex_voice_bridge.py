#!/usr/bin/env python3
"""Install the Xiaozhi bridge as a Codex MCP process and an SSH tunnel."""

from __future__ import annotations

import argparse
import json
import os
import plistlib
import shutil
import stat
from pathlib import Path

TUNNEL_LABEL = "com.xiaozhi.codex-tunnel"
LEGACY_BRIDGE_LABEL = "com.xiaozhi.codex-bridge"
CONFIG_BEGIN = "# BEGIN XIAOZHI CODEX VOICE BRIDGE"
CONFIG_END = "# END XIAOZHI CODEX VOICE BRIDGE"


def write_plist(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    with temporary.open("wb") as stream:
        plistlib.dump(payload, stream, sort_keys=True)
    os.chmod(temporary, stat.S_IRUSR | stat.S_IWUSR)
    os.replace(temporary, path)


def launch_agent(label: str, arguments: list[str], log_path: Path) -> dict[str, object]:
    return {
        "Label": label,
        "ProgramArguments": arguments,
        "RunAtLoad": True,
        "KeepAlive": True,
        "ProcessType": "Background",
        "ThrottleInterval": 10,
        "StandardOutPath": str(log_path),
        "StandardErrorPath": str(log_path),
    }


def toml_string(value: str | Path) -> str:
    return json.dumps(str(value), ensure_ascii=False)


def bridge_config_block(
    node_bin: Path,
    bridge_script: Path,
    server_url: str,
    token_file: Path,
    codex_bin: Path,
) -> str:
    arguments = [
        bridge_script,
        "--server-url",
        server_url,
        "--token-file",
        token_file,
        "--codex-bin",
        codex_bin,
    ]
    rendered_arguments = ", ".join(toml_string(item) for item in arguments)
    return "\n".join(
        [
            CONFIG_BEGIN,
            "[mcp_servers.xiaozhi_codex_voice_bridge]",
            f"command = {toml_string(node_bin)}",
            f"args = [{rendered_arguments}]",
            "enabled = true",
            "startup_timeout_sec = 15",
            "tool_timeout_sec = 3600",
            (
                'env_vars = ["CODEX_APP_TOOLS_PIPE_PATH", '
                '"CODEX_MCP_NODE_PATH", "HOME", "PATH"]'
            ),
            CONFIG_END,
        ]
    )


def update_codex_config(config_path: Path, block: str) -> bool:
    current = config_path.read_text(encoding="utf-8") if config_path.exists() else ""
    start = current.find(CONFIG_BEGIN)
    end = current.find(CONFIG_END)
    if (start == -1) != (end == -1):
        raise ValueError("Codex config contains an incomplete Xiaozhi managed block")
    if start != -1:
        end += len(CONFIG_END)
        current = f"{current[:start].rstrip()}\n{current[end:].lstrip()}".strip()
    if "[mcp_servers.xiaozhi_codex_voice_bridge]" in current:
        raise ValueError(
            "Codex config already defines mcp_servers.xiaozhi_codex_voice_bridge "
            "outside the managed block"
        )
    updated = f"{current.rstrip()}\n\n{block}\n" if current.strip() else f"{block}\n"
    if (
        updated == config_path.read_text(encoding="utf-8")
        if config_path.exists()
        else False
    ):
        return False
    config_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = config_path.with_suffix(".tmp")
    temporary.write_text(updated, encoding="utf-8")
    os.chmod(temporary, stat.S_IRUSR | stat.S_IWUSR)
    os.replace(temporary, config_path)
    return True


def parse_args() -> argparse.Namespace:
    repository = Path(__file__).resolve().parents[1]
    resources = Path("/Applications/ChatGPT.app/Contents/Resources")
    parser = argparse.ArgumentParser(description=__doc__)
    token_source = parser.add_mutually_exclusive_group(required=True)
    token_source.add_argument("--token", help="same random token as the server")
    token_source.add_argument(
        "--token-file", type=Path, help="user-only file containing the token"
    )
    parser.add_argument("--server-url", default="http://127.0.0.1:18003")
    parser.add_argument("--ssh-host")
    parser.add_argument("--local-port", type=int, default=18003)
    parser.add_argument("--remote-port", type=int, default=8003)
    parser.add_argument("--identity-file", type=Path)
    parser.add_argument(
        "--bridge-script",
        type=Path,
        default=repository / "tools" / "codex_voice_bridge.mjs",
    )
    parser.add_argument(
        "--install-dir",
        type=Path,
        default=Path.home() / "Library/Application Support/XiaozhiCodexBridge",
    )
    parser.add_argument(
        "--node-bin", type=Path, default=resources / "cua_node/bin/node"
    )
    parser.add_argument("--codex-bin", type=Path, default=resources / "codex")
    parser.add_argument(
        "--codex-config", type=Path, default=Path.home() / ".codex/config.toml"
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    install_dir = args.install_dir.expanduser().resolve()
    install_dir.mkdir(parents=True, exist_ok=True)

    source = args.bridge_script.expanduser().resolve()
    if not source.is_file():
        raise SystemExit(f"bridge script not found: {source}")
    bridge_script = install_dir / "codex_voice_bridge.mjs"
    temporary_script = install_dir / ".codex_voice_bridge.mjs.tmp"
    shutil.copy2(source, temporary_script)
    os.chmod(temporary_script, stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR)
    os.replace(temporary_script, bridge_script)

    if args.token_file:
        token_file = args.token_file.expanduser().resolve()
        token = token_file.read_text(encoding="utf-8").strip()
    else:
        token = str(args.token).strip()
        token_file = install_dir / "token"
        token_file.write_text(f"{token}\n", encoding="utf-8")
        os.chmod(token_file, stat.S_IRUSR | stat.S_IWUSR)
    if len(token) < 32:
        raise SystemExit("token must contain at least 32 characters")

    node_bin = args.node_bin.expanduser().resolve()
    codex_bin = args.codex_bin.expanduser().resolve()
    if not node_bin.is_file() or not codex_bin.is_file():
        raise SystemExit("Codex desktop Node or App Server binary was not found")
    config_path = args.codex_config.expanduser().resolve()
    block = bridge_config_block(
        node_bin, bridge_script, args.server_url, token_file, codex_bin
    )
    config_changed = update_codex_config(config_path, block)

    paths = []
    if args.ssh_host:
        tunnel_arguments = [
            "/usr/bin/ssh",
            "-N",
            "-o",
            "BatchMode=yes",
            "-o",
            "PreferredAuthentications=publickey",
            "-o",
            "IdentitiesOnly=yes",
            "-o",
            "ExitOnForwardFailure=yes",
            "-o",
            "ServerAliveInterval=30",
            "-o",
            "ServerAliveCountMax=3",
        ]
        if args.identity_file:
            identity_file = args.identity_file.expanduser().resolve()
            if not identity_file.is_file():
                raise SystemExit(f"SSH identity file not found: {identity_file}")
            tunnel_arguments.extend(["-i", str(identity_file)])
        tunnel_arguments.extend(
            [
                "-L",
                f"{args.local_port}:127.0.0.1:{args.remote_port}",
                args.ssh_host,
            ]
        )
        home = Path.home()
        logs = home / "Library/Logs"
        logs.mkdir(parents=True, exist_ok=True)
        tunnel_path = home / "Library/LaunchAgents" / f"{TUNNEL_LABEL}.plist"
        write_plist(
            tunnel_path,
            launch_agent(
                TUNNEL_LABEL,
                tunnel_arguments,
                logs / f"{TUNNEL_LABEL}.log",
            ),
        )
        paths.append(tunnel_path)

    legacy_plist = Path.home() / "Library/LaunchAgents" / f"{LEGACY_BRIDGE_LABEL}.plist"
    if legacy_plist.exists():
        legacy_plist.unlink()

    print(f"Bridge installed at:\n  {bridge_script}")
    print(
        f"Codex MCP config {'updated' if config_changed else 'already current'}:\n  {config_path}"
    )
    if paths:
        print("SSH tunnel LaunchAgent:")
        for path in paths:
            print(f"  {path}")
    print("Restart Codex desktop once to load the bridge configuration.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
