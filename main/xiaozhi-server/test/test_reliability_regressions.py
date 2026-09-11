"""关键可靠性问题的标准库回归测试。

测试使用轻量替身隔离云 API 和音频依赖，不会连接设备、修改真实闹钟文件
或产生付费请求。
"""

import asyncio
import importlib
import json
import sys
import tempfile
import time
import types
import unittest
from datetime import datetime
from pathlib import Path


class _Logger:
    def bind(self, **_kwargs):
        return self

    def __getattr__(self, _name):
        return lambda *_args, **_kwargs: None


logger_module = types.ModuleType("config.logger")
logger_module.setup_logging = lambda: _Logger()
sys.modules["config.logger"] = logger_module


class _Action:
    REQLLM = "reqllm"
    ERROR = "error"


class _ActionResponse:
    def __init__(self, action=None, result=None, response=None, **_kwargs):
        self.action = action
        self.result = result
        self.response = response


class _ToolType:
    SYSTEM_CTL = "system"


register_module = types.ModuleType("plugins_func.register")
register_module.Action = _Action
register_module.ActionResponse = _ActionResponse
register_module.ToolType = _ToolType
register_module.register_function = lambda *_args, **_kwargs: (
    lambda function: function
)
sys.modules["plugins_func.register"] = register_module


class _DeviceNotReadyError(RuntimeError):
    pass


notifier_module = types.ModuleType("core.device_notifier")
notifier_module.DeviceNotReadyError = _DeviceNotReadyError
notifier_module.queue_text_notification = None
sys.modules["core.device_notifier"] = notifier_module

alarm_clock = importlib.import_module("plugins_func.functions.alarm_clock")
connection_manager_module = importlib.import_module("core.connection_manager")
auth_module = importlib.import_module("core.auth")


class ConnectionManagerTests(unittest.TestCase):
    def test_old_connection_cannot_remove_replacement(self):
        manager = connection_manager_module.ConnectionManager()
        old_handler = object()
        new_handler = object()
        manager.register("device", old_handler)
        manager.register("device", new_handler)

        self.assertFalse(manager.unregister("device", old_handler))
        self.assertIs(manager.get_handler("device"), new_handler)
        self.assertTrue(manager.unregister("device", new_handler))


class AlarmManagerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.data_file = Path(self.temp_dir.name) / "alarms.json"
        self.manager = alarm_clock.AlarmManager(self.data_file)

    def tearDown(self):
        self.temp_dir.cleanup()

    @staticmethod
    def _alarm(target):
        return {
            "id": "alarm-test",
            "device_id": "device",
            "time": target.strftime("%Y-%m-%d %H:%M:%S"),
            "target_timestamp": target.timestamp(),
            "next_fire_at": target.timestamp(),
            "repeat": "once",
            "label": "测试",
            "enabled": True,
            "delivery_status": "pending",
        }

    async def test_alarm_is_not_missed_when_loop_skips_target_second(self):
        target = datetime(2026, 9, 11, 9, 0, 1)
        alarm = self._alarm(target)
        self.assertTrue(self.manager.add_alarm(alarm))
        self.manager.trigger_alarm = lambda _alarm: _async_value("delivery-1")

        queued = await self.manager.process_due_alarms(
            datetime(2026, 9, 11, 9, 0, 2)
        )

        self.assertEqual(queued, 1)
        self.assertFalse(alarm["enabled"])
        self.assertEqual(alarm["delivery_status"], "queued")

    async def test_offline_alarm_remains_pending_and_retries(self):
        target = datetime(2026, 9, 11, 9, 0, 1)
        alarm = self._alarm(target)
        self.assertTrue(self.manager.add_alarm(alarm))

        async def offline(_alarm):
            raise _DeviceNotReadyError("offline")

        self.manager.trigger_alarm = offline
        await self.manager.process_due_alarms(datetime(2026, 9, 11, 9, 0, 2))
        self.assertTrue(alarm["enabled"])
        self.assertEqual(alarm["delivery_status"], "pending")

        self.manager.trigger_alarm = lambda _alarm: _async_value("delivery-2")
        queued = await self.manager.process_due_alarms(
            datetime(2026, 9, 11, 9, 0, 18)
        )
        self.assertEqual(queued, 1)
        self.assertFalse(alarm["enabled"])

    async def test_atomic_file_contains_valid_json(self):
        alarm = self._alarm(datetime(2026, 9, 11, 9, 0, 1))
        self.assertTrue(self.manager.add_alarm(alarm))
        with self.data_file.open("r", encoding="utf-8") as file:
            self.assertEqual(json.load(file)[0]["id"], "alarm-test")
        self.assertEqual(list(self.data_file.parent.glob("*.tmp")), [])


async def _async_value(value):
    return value


class _Cache:
    def __init__(self):
        self.values = {}

    def get(self, cache_type, key):
        return self.values.get((cache_type, key))

    def set(self, cache_type, key, value):
        self.values[(cache_type, key)] = value


cache_module = types.ModuleType("core.utils.cache.manager")
cache_module.cache_manager = _Cache()
cache_module.CacheType = types.SimpleNamespace(INTENT="intent")
sys.modules["core.utils.cache.manager"] = cache_module
intent_module = importlib.import_module(
    "core.providers.intent.intent_llm.intent_llm"
)


class _FunctionHandler:
    def get_functions(self):
        return []


class _Message:
    def __init__(self, role, content):
        self.role = role
        self.content = content


class _Connection:
    device_id = "device"
    func_handler = _FunctionHandler()

    def __init__(self):
        self.dialogue = types.SimpleNamespace(dialogue=[])


class _BlockingLLM:
    model_name = "fake"

    def __init__(self, delay=0.2):
        self.delay = delay
        self.calls = []

    def response_no_stream(self, system_prompt, user_prompt):
        self.calls.append((system_prompt, user_prompt))
        time.sleep(self.delay)
        return '{"function_call": {"name": "continue_chat"}}'


class IntentProviderTests(unittest.IsolatedAsyncioTestCase):
    async def test_sync_llm_does_not_block_event_loop(self):
        provider = intent_module.IntentProvider(
            {"timeout_seconds": 1, "max_concurrent_requests": 1}
        )
        provider.llm = _BlockingLLM()

        started = time.perf_counter()
        intent_task = asyncio.create_task(
            provider.detect_intent(_Connection(), [], "你好")
        )
        await asyncio.sleep(0.02)
        heartbeat_elapsed = time.perf_counter() - started
        await intent_task

        self.assertLess(heartbeat_elapsed, 0.1)

    async def test_cache_key_changes_with_dialogue_context(self):
        provider = intent_module.IntentProvider(
            {"timeout_seconds": 1, "max_concurrent_requests": 1}
        )
        provider.llm = _BlockingLLM(delay=0)

        await provider.detect_intent(
            _Connection(), [_Message("user", "打开客厅")], "再来一次"
        )
        await provider.detect_intent(
            _Connection(), [_Message("user", "设置闹钟")], "再来一次"
        )

        self.assertEqual(len(provider.llm.calls), 2)
        self.assertIn("当前时间：", provider.llm.calls[0][0])


class AuthenticationTests(unittest.IsolatedAsyncioTestCase):
    def test_ota_provisioning_requires_matching_device_secret(self):
        allowed = {"b8:f8:62:e7:90:b4"}
        secrets = {"b8:f8:62:e7:90:b4": "secret-value"}

        self.assertTrue(
            auth_module.verify_device_secret(
                "B8:F8:62:E7:90:B4", "secret-value", allowed, secrets
            )
        )
        self.assertFalse(
            auth_module.verify_device_secret(
                "B8:F8:62:E7:90:B4", "wrong", allowed, secrets
            )
        )
        self.assertFalse(
            auth_module.verify_device_secret(
                "00:00:00:00:00:00", "secret-value", allowed, secrets
            )
        )

    @classmethod
    def setUpClass(cls):
        websockets_module = types.ModuleType("websockets")
        websockets_module.serve = None
        sys.modules["websockets"] = websockets_module

        connection_module = types.ModuleType("core.connection")
        connection_module.ConnectionHandler = object
        sys.modules["core.connection"] = connection_module

        config_loader = types.ModuleType("config.config_loader")
        config_loader.get_config_from_api_async = None
        sys.modules["config.config_loader"] = config_loader

        modules_initialize = types.ModuleType("core.utils.modules_initialize")
        modules_initialize.initialize_modules = None
        sys.modules["core.utils.modules_initialize"] = modules_initialize

        util_module = types.ModuleType("core.utils.util")
        util_module.check_vad_update = None
        util_module.check_asr_update = None
        sys.modules["core.utils.util"] = util_module

        cls.websocket_server = importlib.import_module("core.websocket_server")

    async def test_allowed_device_still_requires_valid_token(self):
        server = self.websocket_server.WebSocketServer.__new__(
            self.websocket_server.WebSocketServer
        )
        server.auth_enable = True
        server.allowed_devices = {"device"}
        server.auth = types.SimpleNamespace(
            verify_token=lambda token, client_id, username: token == "valid"
        )

        missing = _WebSocket(
            {"device-id": "device", "client-id": "client"}
        )
        with self.assertRaises(self.websocket_server.AuthenticationError):
            await server._handle_auth(missing)

        valid = _WebSocket(
            {
                "device-id": "device",
                "client-id": "client",
                "authorization": "Bearer valid",
            }
        )
        await server._handle_auth(valid)

        disallowed = _WebSocket(
            {
                "device-id": "other",
                "client-id": "client",
                "authorization": "Bearer valid",
            }
        )
        with self.assertRaises(self.websocket_server.AuthenticationError):
            await server._handle_auth(disallowed)


class _WebSocket:
    def __init__(self, headers):
        self.request = types.SimpleNamespace(headers=headers)


if __name__ == "__main__":
    unittest.main()
