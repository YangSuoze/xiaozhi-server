#!/usr/bin/env python3
"""Mac bridge between the Xiaozhi server and Codex App Server.

The bridge opens only outbound connections.  It polls the cloud queue, translates
jobs into Codex App Server requests, and forwards task events to the speaker.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import queue
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from typing import Any

VERSION = "1.0.0"
LOGGER = logging.getLogger("codex_voice_bridge")
APPROVAL_METHODS = {
    "item/commandExecution/requestApproval",
    "item/fileChange/requestApproval",
    "item/permissions/requestApproval",
}


def default_codex_binary() -> str:
    configured = os.environ.get("CODEX_BIN")
    if configured:
        return configured
    desktop_binary = "/Applications/ChatGPT.app/Contents/Resources/codex"
    if os.path.isfile(desktop_binary) and os.access(desktop_binary, os.X_OK):
        return desktop_binary
    return shutil.which("codex") or "codex"


class CloudClient:
    def __init__(
        self,
        base_url: str,
        token: str,
        bridge_id: str,
        allow_insecure_http: bool = False,
        timeout: float = 15,
    ):
        self.base_url = base_url.rstrip("/")
        self.token = token.strip()
        self.bridge_id = bridge_id
        self.timeout = timeout
        parsed = urllib.parse.urlparse(self.base_url)
        if parsed.scheme not in {"http", "https"}:
            raise ValueError("server URL must use http or https")
        loopback = parsed.hostname in {"127.0.0.1", "localhost", "::1"}
        if parsed.scheme == "http" and not loopback and not allow_insecure_http:
            raise ValueError(
                "plain HTTP is allowed only for localhost; use HTTPS or an SSH tunnel"
            )
        if not self.token:
            raise ValueError("bridge token is required")
        self._opener = (
            urllib.request.build_opener(urllib.request.ProxyHandler({}))
            if loopback
            else urllib.request.build_opener()
        )

    def post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(
            f"{self.base_url}{path}",
            data=body,
            method="POST",
            headers={
                "Content-Type": "application/json",
                "X-Codex-Bridge-Token": self.token,
                "User-Agent": f"xiaozhi-codex-bridge/{VERSION}",
            },
        )
        try:
            with self._opener.open(request, timeout=self.timeout) as response:
                data = response.read()
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:500]
            raise RuntimeError(f"cloud returned HTTP {exc.code}: {detail}") from exc
        if not data:
            return {}
        result = json.loads(data.decode("utf-8"))
        if not isinstance(result, dict):
            raise TypeError("cloud returned a non-object response")
        return result

    def heartbeat(self) -> None:
        self.post(
            "/codex/bridge/v1/heartbeat",
            {
                "bridge_id": self.bridge_id,
                "hostname": socket.gethostname(),
                "version": VERSION,
                "capabilities": {
                    "thread_list": True,
                    "thread_monitor": True,
                    "turn_start": True,
                    "turn_steer": True,
                    "request_user_input": True,
                },
            },
        )

    def lease_job(self) -> dict[str, Any] | None:
        response = self.post(
            "/codex/bridge/v1/jobs/lease", {"bridge_id": self.bridge_id}
        )
        job = response.get("job")
        return job if isinstance(job, dict) else None

    def complete_job(
        self,
        job_id: str,
        response: dict[str, Any] | None = None,
        error: str | None = None,
    ) -> None:
        self.post(
            "/codex/bridge/v1/jobs/complete",
            {
                "bridge_id": self.bridge_id,
                "job_id": job_id,
                "response": response,
                "error": error,
            },
        )

    def post_event(self, event: dict[str, Any]) -> None:
        self.post(
            "/codex/bridge/v1/events",
            {"bridge_id": self.bridge_id, "event": event},
        )


class AppServerClient:
    """Minimal JSON-lines client for `codex app-server --stdio`."""

    def __init__(
        self,
        codex_bin: str,
        event_callback: Callable[[dict[str, Any]], None],
        request_timeout: float = 30,
        managed_app_server: bool = False,
    ):
        self.codex_bin = codex_bin
        self.event_callback = event_callback
        self.request_timeout = request_timeout
        self.managed_app_server = managed_app_server
        self.process: subprocess.Popen[str] | None = None
        self._request_id = 0
        self._write_lock = threading.Lock()
        self._state_lock = threading.RLock()
        self._pending: dict[int, queue.Queue] = {}
        self._server_requests: dict[str, dict[str, Any]] = {}
        self._thread_status: dict[str, Any] = {}
        self._active_turns: dict[str, str] = {}
        self._agent_text: dict[tuple[str, str], str] = {}
        self._monitored_threads: set[str] = set()

    @staticmethod
    def _key(request_id: Any) -> str:
        return json.dumps(request_id, ensure_ascii=False, sort_keys=True)

    def start(self) -> None:
        command = (
            [self.codex_bin, "app-server", "proxy"]
            if self.managed_app_server
            else [self.codex_bin, "app-server", "--stdio"]
        )
        self.process = subprocess.Popen(
            command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            bufsize=1,
        )
        threading.Thread(target=self._read_stdout, daemon=True).start()
        threading.Thread(target=self._read_stderr, daemon=True).start()
        self.request(
            "initialize",
            {
                "clientInfo": {
                    "name": "xiaozhi-codex-bridge",
                    "title": "Xiaozhi Codex Voice Bridge",
                    "version": VERSION,
                },
                "capabilities": {"experimentalApi": True},
            },
        )
        self.notify("initialized", {})

    def close(self) -> None:
        process = self.process
        self.process = None
        if process and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()

    def _send(self, message: dict[str, Any]) -> None:
        process = self.process
        if process is None or process.poll() is not None or process.stdin is None:
            raise RuntimeError("Codex App Server is not running")
        line = json.dumps(message, ensure_ascii=False, separators=(",", ":"))
        with self._write_lock:
            process.stdin.write(line + "\n")
            process.stdin.flush()

    def request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        with self._state_lock:
            self._request_id += 1
            request_id = self._request_id
            response_queue: queue.Queue = queue.Queue(maxsize=1)
            self._pending[request_id] = response_queue
        try:
            self._send({"id": request_id, "method": method, "params": params})
            try:
                message = response_queue.get(timeout=self.request_timeout)
            except queue.Empty as exc:
                raise TimeoutError(f"Codex request timed out: {method}") from exc
            if "error" in message:
                error = message["error"]
                if isinstance(error, dict):
                    error = error.get("message") or json.dumps(
                        error, ensure_ascii=False
                    )
                raise RuntimeError(f"Codex request failed: {error}")
            result = message.get("result") or {}
            return result if isinstance(result, dict) else {"value": result}
        finally:
            with self._state_lock:
                self._pending.pop(request_id, None)

    def notify(self, method: str, params: dict[str, Any]) -> None:
        self._send({"method": method, "params": params})

    def _read_stdout(self) -> None:
        process = self.process
        if process is None or process.stdout is None:
            return
        for line in process.stdout:
            try:
                message = json.loads(line)
                self._handle_message(message)
            except json.JSONDecodeError:
                LOGGER.warning("ignored malformed App Server output")
            except Exception:
                LOGGER.exception("failed to process App Server message")

    def _read_stderr(self) -> None:
        process = self.process
        if process is None or process.stderr is None:
            return
        for line in process.stderr:
            text = line.strip()
            if text:
                LOGGER.debug("codex: %s", text[:1000])

    def _handle_message(self, message: dict[str, Any]) -> None:
        if "id" in message and ("result" in message or "error" in message):
            request_id = message["id"]
            if isinstance(request_id, int):
                with self._state_lock:
                    target = self._pending.get(request_id)
                if target:
                    target.put(message)
            return

        method = message.get("method")
        params = message.get("params") or {}
        if "id" in message and method:
            with self._state_lock:
                self._server_requests[self._key(message["id"])] = message
            self._handle_server_request(message["id"], method, params)
            return
        if method:
            self._handle_notification(method, params)

    def _handle_notification(self, method: str, params: dict[str, Any]) -> None:
        thread_id = params.get("threadId")
        if method == "thread/status/changed" and thread_id:
            status = params.get("status") or {"type": "unknown"}
            with self._state_lock:
                self._thread_status[thread_id] = status
            if self._is_monitored(thread_id):
                self.event_callback(
                    {
                        "type": "thread_status",
                        "thread_id": thread_id,
                        "status": status,
                        "turn_id": self._active_turn(thread_id),
                    }
                )
            return
        if method == "turn/started" and thread_id:
            turn = params.get("turn") or {}
            turn_id = turn.get("id") or params.get("turnId")
            if turn_id:
                with self._state_lock:
                    self._active_turns[thread_id] = turn_id
            return
        if method == "item/agentMessage/delta" and thread_id:
            turn_id = params.get("turnId") or ""
            key = (thread_id, turn_id)
            with self._state_lock:
                self._agent_text[key] = self._agent_text.get(key, "") + str(
                    params.get("delta") or ""
                )
            return
        if method == "turn/completed" and thread_id:
            turn = params.get("turn") or {}
            turn_id = turn.get("id") or params.get("turnId") or ""
            with self._state_lock:
                summary = self._agent_text.pop((thread_id, turn_id), "")
                self._active_turns.pop(thread_id, None)
                self._thread_status[thread_id] = {"type": "idle"}
            if not summary:
                summary = self._extract_agent_message(turn)
            if self._is_monitored(thread_id):
                self.event_callback(
                    {
                        "type": "turn_completed",
                        "thread_id": thread_id,
                        "turn_id": turn_id or None,
                        "status": turn.get("status") or "completed",
                        "summary": summary[:2000],
                    }
                )

    @staticmethod
    def _extract_agent_message(turn: dict[str, Any]) -> str:
        for item in reversed(turn.get("items") or []):
            if item.get("type") in {"agentMessage", "assistantMessage"}:
                return str(item.get("text") or item.get("content") or "")
        return ""

    @staticmethod
    def _approval_description(method: str, params: dict[str, Any]) -> str:
        reason = params.get("reason")
        if reason:
            return str(reason)
        command = params.get("command") or params.get("commandActions")
        if command:
            if isinstance(command, list):
                command = " ".join(str(item) for item in command)
            return f"请求执行：{command}。"
        if method == "item/fileChange/requestApproval":
            return "请求修改文件。"
        if method == "item/permissions/requestApproval":
            return "请求额外的文件或网络权限。"
        return "请求执行需要确认的操作。"

    def _handle_server_request(
        self, request_id: Any, method: str, params: dict[str, Any]
    ) -> None:
        thread_id = params.get("threadId")
        if method == "item/tool/requestUserInput" and thread_id:
            questions = params.get("questions") or []
            spoken = []
            question_ids = []
            for question in questions:
                question_ids.append(question.get("id"))
                text = str(question.get("question") or "")
                options = question.get("options") or []
                if options:
                    labels = "、".join(
                        str(option.get("label") or "") for option in options
                    )
                    text += f"可选：{labels}。"
                spoken.append(text)
            self.event_callback(
                {
                    "type": "user_input",
                    "thread_id": thread_id,
                    "turn_id": params.get("turnId"),
                    "request_id": request_id,
                    "question": " ".join(spoken),
                    "question_ids": [item for item in question_ids if item],
                }
            )
            return
        if method in APPROVAL_METHODS and thread_id:
            self.event_callback(
                {
                    "type": "approval",
                    "thread_id": thread_id,
                    "turn_id": params.get("turnId"),
                    "request_id": request_id,
                    "method": method,
                    "description": self._approval_description(method, params),
                }
            )
            return
        LOGGER.warning("unsupported App Server request: %s", method)

    def _is_monitored(self, thread_id: str) -> bool:
        with self._state_lock:
            return thread_id in self._monitored_threads

    def _active_turn(self, thread_id: str) -> str | None:
        with self._state_lock:
            return self._active_turns.get(thread_id)

    def list_threads(self, limit: int) -> dict[str, Any]:
        result = self.request(
            "thread/list",
            {
                "limit": min(5, max(1, int(limit))),
                "archived": False,
                "sortKey": "recency_at",
                "sortDirection": "desc",
            },
        )
        threads = []
        for item in result.get("data") or []:
            title = item.get("name") or item.get("preview") or "未命名任务"
            threads.append(
                {
                    "id": item.get("id") or item.get("sessionId"),
                    "title": str(title).splitlines()[0],
                    "status": item.get("status") or {"type": "notLoaded"},
                    "updated_at": item.get("recencyAt") or item.get("updatedAt"),
                }
            )
        return {"threads": threads}

    def monitor_thread(self, thread_id: str) -> dict[str, Any]:
        with self._state_lock:
            self._monitored_threads.add(thread_id)
        result = self.request("thread/resume", {"threadId": thread_id})
        thread = result.get("thread") or {}
        status = (
            thread.get("status")
            or self._thread_status.get(thread_id)
            or {"type": "idle"}
        )
        with self._state_lock:
            self._thread_status[thread_id] = status
        return {
            "status": status,
            "turn_id": self._active_turn(thread_id),
        }

    def get_status(self, thread_id: str) -> dict[str, Any]:
        if not self._is_monitored(thread_id):
            return self.monitor_thread(thread_id)
        with self._state_lock:
            status = self._thread_status.get(thread_id) or {"type": "idle"}
        return {"status": status, "turn_id": self._active_turn(thread_id)}

    def send_message(self, payload: dict[str, Any]) -> dict[str, Any]:
        thread_id = str(payload["thread_id"])
        if not self._is_monitored(thread_id):
            self.monitor_thread(thread_id)
        text = str(payload.get("text") or "").strip()
        if not text:
            raise ValueError("instruction is empty")

        mode = payload.get("mode", "start")
        active_turn = self._active_turn(thread_id)
        if mode == "steer" and active_turn:
            result = self.request(
                "turn/steer",
                {
                    "threadId": thread_id,
                    "expectedTurnId": payload.get("expected_turn_id") or active_turn,
                    "input": [{"type": "text", "text": text}],
                },
            )
            return {"turn_id": result.get("turnId") or active_turn, "steered": True}
        if active_turn:
            raise RuntimeError(
                "Codex task is still active; retry after it becomes idle"
            )

        result = self.request(
            "turn/start",
            {
                "threadId": thread_id,
                "input": [{"type": "text", "text": text}],
                "clientUserMessageId": payload.get("client_user_message_id"),
            },
        )
        turn = result.get("turn") or {}
        turn_id = turn.get("id")
        if turn_id:
            with self._state_lock:
                self._active_turns[thread_id] = turn_id
                self._thread_status[thread_id] = {"type": "active", "activeFlags": []}
        return {"turn_id": turn_id, "steered": False}

    def respond_request(self, payload: dict[str, Any]) -> dict[str, Any]:
        request_id = payload.get("request_id")
        key = self._key(request_id)
        with self._state_lock:
            request = self._server_requests.get(key)
        if request is None:
            raise RuntimeError("Codex is no longer waiting for this response")
        method = request["method"]
        params = request.get("params") or {}
        if method == "item/tool/requestUserInput":
            question_ids = [
                item.get("id")
                for item in params.get("questions") or []
                if item.get("id")
            ]
            answer = str(payload.get("answer") or "")
            result = {
                "answers": {
                    question_id: {"answers": [answer]} for question_id in question_ids
                }
            }
        elif method in {
            "item/commandExecution/requestApproval",
            "item/fileChange/requestApproval",
        }:
            result = {"decision": "accept" if payload.get("approved") else "decline"}
        elif method == "item/permissions/requestApproval":
            result = {
                "permissions": params.get("permissions")
                if payload.get("approved")
                else {},
                "scope": "turn",
            }
        else:
            raise RuntimeError(f"unsupported pending request: {method}")
        self._send({"id": request_id, "result": result})
        with self._state_lock:
            self._server_requests.pop(key, None)
        return {"accepted": True}

    def unmonitor_thread(self, thread_id: str) -> dict[str, Any]:
        with self._state_lock:
            self._monitored_threads.discard(thread_id)
        return {"unmonitored": True}


class VoiceBridge:
    def __init__(
        self,
        cloud: CloudClient,
        codex: AppServerClient,
        poll_interval: float = 1,
        heartbeat_interval: float = 15,
    ):
        self.cloud = cloud
        self.codex = codex
        self.poll_interval = max(0.2, poll_interval)
        self.heartbeat_interval = max(5, heartbeat_interval)
        self.events: queue.Queue[dict[str, Any]] = queue.Queue()
        self.stopping = threading.Event()

    def queue_event(self, event: dict[str, Any]) -> None:
        self.events.put(event)

    def stop(self, *_args: Any) -> None:
        self.stopping.set()

    def _drain_events(self) -> None:
        for _ in range(100):
            try:
                event = self.events.get_nowait()
            except queue.Empty:
                return
            try:
                self.cloud.post_event(event)
            except Exception:
                self.events.put(event)
                raise

    def _execute_job(self, job: dict[str, Any]) -> dict[str, Any]:
        kind = job.get("kind")
        payload = job.get("payload") or {}
        if kind == "list_threads":
            return self.codex.list_threads(payload.get("limit", 3))
        if kind == "monitor_thread":
            return self.codex.monitor_thread(str(payload["thread_id"]))
        if kind == "get_status":
            return self.codex.get_status(str(payload["thread_id"]))
        if kind == "send_message":
            return self.codex.send_message(payload)
        if kind == "respond_request":
            return self.codex.respond_request(payload)
        if kind == "unmonitor_thread":
            return self.codex.unmonitor_thread(str(payload["thread_id"]))
        raise ValueError(f"unsupported job kind: {kind}")

    def _handle_job(self, job: dict[str, Any]) -> None:
        job_id = str(job.get("id") or "")
        try:
            response = self._execute_job(job)
        except Exception as exc:
            LOGGER.exception("job %s failed", job_id)
            self.cloud.complete_job(job_id, error=str(exc)[:1000])
        else:
            self.cloud.complete_job(job_id, response=response)

    def run(self, once: bool = False) -> None:
        self.codex.start()
        next_heartbeat = 0.0
        backoff = 1.0
        try:
            while not self.stopping.is_set():
                try:
                    now = time.monotonic()
                    if now >= next_heartbeat:
                        self.cloud.heartbeat()
                        next_heartbeat = now + self.heartbeat_interval
                    self._drain_events()
                    job = self.cloud.lease_job()
                    if job:
                        self._handle_job(job)
                    backoff = 1.0
                    if once:
                        break
                    self.stopping.wait(self.poll_interval)
                except Exception as exc:
                    LOGGER.warning("cloud connection failed: %s", exc)
                    if once:
                        raise
                    self.stopping.wait(backoff)
                    backoff = min(30.0, backoff * 2)
        finally:
            self.codex.close()


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--server-url",
        default=os.environ.get("XIAOZHI_CODEX_SERVER_URL", "http://127.0.0.1:18003"),
        help="Xiaozhi HTTP server base URL; localhost SSH tunnel is recommended",
    )
    parser.add_argument(
        "--token", default=os.environ.get("XIAOZHI_CODEX_BRIDGE_TOKEN", "")
    )
    parser.add_argument(
        "--token-file",
        type=os.path.expanduser,
        help="read the bridge token from a user-only file",
    )
    parser.add_argument(
        "--bridge-id",
        default=os.environ.get(
            "XIAOZHI_CODEX_BRIDGE_ID", f"mac-{socket.gethostname()}"
        ),
    )
    parser.add_argument(
        "--codex-bin",
        default=default_codex_binary(),
    )
    parser.add_argument("--poll-interval", type=float, default=1.0)
    parser.add_argument("--heartbeat-interval", type=float, default=15.0)
    parser.add_argument(
        "--managed-app-server",
        action="store_true",
        help="connect through `codex app-server proxy` to a bootstrapped daemon",
    )
    parser.add_argument("--allow-insecure-http", action="store_true")
    parser.add_argument("--once", action="store_true", help="process at most one job")
    parser.add_argument("--verbose", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    try:
        token = args.token
        if args.token_file:
            with open(args.token_file, encoding="utf-8") as token_file:
                token = token_file.read().strip()
        cloud = CloudClient(
            args.server_url,
            token,
            args.bridge_id,
            allow_insecure_http=args.allow_insecure_http,
        )
        holder: dict[str, VoiceBridge] = {}
        codex = AppServerClient(
            args.codex_bin,
            lambda event: holder["bridge"].queue_event(event),
            managed_app_server=args.managed_app_server,
        )
        bridge = VoiceBridge(
            cloud,
            codex,
            poll_interval=args.poll_interval,
            heartbeat_interval=args.heartbeat_interval,
        )
        holder["bridge"] = bridge
        signal.signal(signal.SIGINT, bridge.stop)
        signal.signal(signal.SIGTERM, bridge.stop)
        bridge.run(once=args.once)
        return 0
    except Exception as exc:  # noqa: BLE001 - command boundary reports cleanly
        LOGGER.error("bridge stopped: %s", exc)
        return 1


if __name__ == "__main__":
    sys.exit(main())
