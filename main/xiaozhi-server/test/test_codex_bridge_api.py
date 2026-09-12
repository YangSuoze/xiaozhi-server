import asyncio
import json

import pytest
from aiohttp import web

from core.api.codex_bridge_handler import CodexBridgeHandler
from core.codex.service import CodexControlService
from core.codex.store import CodexStore


class SilentLogger:
    def bind(self, **_kwargs):
        return self

    def __getattr__(self, _name):
        return lambda *_args, **_kwargs: None


class FakeRequest:
    def __init__(self, body, token="test-token"):
        self.headers = {"X-Codex-Bridge-Token": token}
        self._body = body

    async def json(self):
        return self._body


def make_handler(tmp_path):
    config = {"codex_bridge": {"enabled": True, "token": "test-token"}}
    service = CodexControlService(
        config,
        store=CodexStore(tmp_path / "codex.db"),
        logger=SilentLogger(),
    )
    return CodexBridgeHandler(service), service


def test_bridge_api_authenticates_and_leases_jobs(tmp_path):
    handler, service = make_handler(tmp_path)
    with pytest.raises(web.HTTPUnauthorized):
        asyncio.run(
            handler.heartbeat(
                FakeRequest({"bridge_id": "mac-home"}, token="wrong-token")
            )
        )

    response = asyncio.run(
        handler.heartbeat(
            FakeRequest(
                {
                    "bridge_id": "mac-home",
                    "hostname": "home-mac",
                    "version": "1.0",
                    "capabilities": {"thread_list": True},
                }
            )
        )
    )
    assert json.loads(response.text) == {"ok": True}

    health = json.loads(asyncio.run(handler.health(FakeRequest({}))).text)
    assert health["bridge"]["version"] == "1.0"
    assert health["bridge"]["capabilities"] == {"thread_list": True}

    service.store.create_job("speaker-1", "list_threads", {"limit": 3})
    response = asyncio.run(handler.lease_job(FakeRequest({"bridge_id": "mac-home"})))
    job = json.loads(response.text)["job"]
    assert job["kind"] == "list_threads"
    assert job["payload"] == {"limit": 3}
