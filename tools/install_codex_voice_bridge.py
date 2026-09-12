#!/usr/bin/env python3
"""Create macOS LaunchAgents for the Codex voice bridge and SSH tunnel."""

from __future__ import annotations

import argparse
import os
import plistlib
import shutil
import stat
import sys
from pathlib import Path

BRIDGE_LABEL = "com.xiaozhi.codex-bridge"
TUNNEL_LABEL = "com.xiaozhi.codex-tunnel"


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


def parse_args() -> argparse.Namespace:
    repository = Path(__file__).resolve().parents[1]
    desktop_codex = Path("/Applications/ChatGPT.app/Contents/Resources/codex")
    parser = argparse.ArgumentParser(description=__doc__)
    token_source = parser.add_mutually_exclusive_group(required=True)
    token_source.add_argument("--token", help="same random token as the server")
    token_source.add_argument(
        "--token-file", type=Path, help="user-only file containing the token"
    )
    parser.add_argument(
        "--server-url", default="http://127.0.0.1:18003", help="bridge API base URL"
    )
    parser.add_argument(
        "--ssh-host",
        help="optional SSH config host; creates a persistent localhost tunnel",
    )
    parser.add_argument("--local-port", type=int, default=18003)
    parser.add_argument("--remote-port", type=int, default=8003)
    parser.add_argument(
        "--identity-file",
        type=Path,
        help="private SSH key used by the tunnel LaunchAgent",
    )
    parser.add_argument(
        "--bridge-script",
        type=Path,
        default=repository / "tools" / "codex_voice_bridge.py",
    )
    parser.add_argument("--python-bin", default=sys.executable)
    parser.add_argument(
        "--codex-bin",
        default=(
            str(desktop_codex)
            if desktop_codex.exists()
            else shutil.which("codex") or "codex"
        ),
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    token_file = args.token_file.expanduser().resolve() if args.token_file else None
    token = token_file.read_text(encoding="utf-8").strip() if token_file else args.token
    if len(token) < 32:
        raise SystemExit("token must contain at least 32 characters")
    bridge_script = args.bridge_script.expanduser().resolve()
    if not bridge_script.is_file():
        raise SystemExit(f"bridge script not found: {bridge_script}")

    home = Path.home()
    agents = home / "Library" / "LaunchAgents"
    logs = home / "Library" / "Logs"
    logs.mkdir(parents=True, exist_ok=True)

    bridge_arguments = [
        str(Path(args.python_bin).expanduser()),
        str(bridge_script),
        "--server-url",
        args.server_url,
        "--codex-bin",
        str(Path(args.codex_bin).expanduser()),
        "--managed-app-server",
    ]
    if token_file:
        bridge_arguments.extend(["--token-file", str(token_file)])
    else:
        bridge_arguments.extend(["--token", token])
    bridge_path = agents / f"{BRIDGE_LABEL}.plist"
    write_plist(
        bridge_path,
        launch_agent(BRIDGE_LABEL, bridge_arguments, logs / f"{BRIDGE_LABEL}.log"),
    )

    paths = [bridge_path]
    if args.ssh_host:
        tunnel_arguments = [
            "/usr/bin/ssh",
            "-N",
            "-o",
            "BatchMode=yes",
            "-o",
            "PreferredAuthentications=publickey",
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
        tunnel_path = agents / f"{TUNNEL_LABEL}.plist"
        write_plist(
            tunnel_path,
            launch_agent(
                TUNNEL_LABEL,
                tunnel_arguments,
                logs / f"{TUNNEL_LABEL}.log",
            ),
        )
        paths.insert(0, tunnel_path)

    user_domain = f"gui/{os.getuid()}"
    print("LaunchAgent files created:")
    for path in paths:
        print(f"  {path}")
    print("\nLoad them with:")
    for path in paths:
        print(f"  launchctl bootstrap {user_domain} {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
