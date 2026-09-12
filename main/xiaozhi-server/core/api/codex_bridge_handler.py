"""Authenticated HTTP endpoints consumed by the Mac Codex bridge."""

from __future__ import annotations

import hmac
import re
from typing import Any

from aiohttp import web

from core.codex import CodexControlService

BRIDGE_ID_PATTERN = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")


class CodexBridgeHandler:
    def __init__(self, service: CodexControlService):
        self.service = service
        self.settings = service.settings

    def _authenticate(self, request: web.Request) -> None:
        expected = self.service.bridge_token
        supplied = request.headers.get("X-Codex-Bridge-Token", "")
        if not expected or not hmac.compare_digest(expected, supplied):
            raise web.HTTPUnauthorized(
                text="Codex bridge authentication failed",
                headers={"WWW-Authenticate": "CodexBridge"},
            )

    @staticmethod
    async def _json(request: web.Request) -> dict[str, Any]:
        try:
            body = await request.json()
        except Exception as exc:
            raise web.HTTPBadRequest(text="Request body must be JSON") from exc
        if not isinstance(body, dict):
            raise web.HTTPBadRequest(text="Request body must be an object")
        return body

    @staticmethod
    def _bridge_id(body: dict[str, Any]) -> str:
        bridge_id = str(body.get("bridge_id", "")).strip()
        if not BRIDGE_ID_PATTERN.fullmatch(bridge_id):
            raise web.HTTPBadRequest(text="Invalid bridge_id")
        return bridge_id

    async def heartbeat(self, request: web.Request) -> web.Response:
        self._authenticate(request)
        body = await self._json(request)
        bridge_id = self._bridge_id(body)
        hostname = str(body.get("hostname") or bridge_id).strip()[:255]
        version = str(body.get("version") or "")[:64] or None
        capabilities = body.get("capabilities")
        if capabilities is not None and not isinstance(capabilities, dict):
            raise web.HTTPBadRequest(text="capabilities must be an object")
        self.service.store.heartbeat(bridge_id, hostname, version, capabilities or {})
        return web.json_response({"ok": True})

    async def lease_job(self, request: web.Request) -> web.Response:
        self._authenticate(request)
        body = await self._json(request)
        bridge_id = self._bridge_id(body)
        lease_seconds = max(10, int(self.settings.get("job_lease_seconds", 60)))
        job = self.service.store.lease_job(bridge_id, lease_seconds)
        if job is None:
            return web.json_response({"job": None})
        return web.json_response(
            {
                "job": {
                    "id": job["job_id"],
                    "kind": job["kind"],
                    "payload": job["payload"],
                    "attempt": job["attempts"],
                }
            }
        )

    async def complete_job(self, request: web.Request) -> web.Response:
        self._authenticate(request)
        body = await self._json(request)
        bridge_id = self._bridge_id(body)
        job_id = str(body.get("job_id", "")).strip()
        if not re.fullmatch(r"[0-9a-f]{32}", job_id):
            raise web.HTTPBadRequest(text="Invalid job_id")
        response = body.get("response")
        if response is not None and not isinstance(response, dict):
            raise web.HTTPBadRequest(text="response must be an object")
        error = body.get("error")
        error = str(error)[:1000] if error else None
        job = self.service.store.complete_job(bridge_id, job_id, response, error)
        if job is None:
            raise web.HTTPConflict(text="Job is no longer leased by this bridge")
        self.service.handle_job_result(job)
        return web.json_response({"ok": True})

    async def post_event(self, request: web.Request) -> web.Response:
        self._authenticate(request)
        body = await self._json(request)
        self._bridge_id(body)
        event = body.get("event")
        if not isinstance(event, dict):
            raise web.HTTPBadRequest(text="event must be an object")
        if event.get("type") not in {
            "thread_status",
            "turn_completed",
            "user_input",
            "approval",
        }:
            raise web.HTTPBadRequest(text="Unsupported event type")
        self.service.handle_bridge_event(event)
        return web.json_response({"ok": True})

    async def health(self, request: web.Request) -> web.Response:
        self._authenticate(request)
        bridge = self.service._latest_bridge()
        return web.json_response(
            {
                "enabled": self.service.enabled,
                "bridge_online": bridge is not None,
                "bridge": (
                    {
                        "id": bridge["bridge_id"],
                        "hostname": bridge["hostname"],
                        "last_seen_at": bridge["last_seen_at"],
                    }
                    if bridge
                    else None
                ),
            }
        )
