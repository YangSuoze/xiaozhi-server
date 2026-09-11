"""持久化、多设备闹钟与提醒。"""

import asyncio
import datetime
import json
import os
import re
import tempfile
import threading
import uuid
from pathlib import Path

from config.logger import setup_logging
from core.connection_manager import connection_manager
from core.device_notifier import DeviceNotReadyError, queue_text_notification
from plugins_func.register import Action, ActionResponse, ToolType, register_function

TAG = __name__
logger = setup_logging()

PROJECT_DIR = Path(__file__).resolve().parents[2]
ALARM_DATA_FILE = PROJECT_DIR / "data" / "alarm_clocks.json"
VALID_REPEATS = {"once", "daily", "weekdays", "weekends"}
ONCE_DELIVERY_GRACE_SECONDS = 24 * 60 * 60
REPEATING_DELIVERY_GRACE_SECONDS = 5 * 60
OFFLINE_RETRY_SECONDS = 15
_alarm_file_lock = threading.RLock()


set_alarm_function_desc = {
    "type": "function",
    "function": {
        "name": "set_alarm",
        "description": (
            "设置一次性或重复闹钟。相对时间使用 30s、10m、2h、3d；"
            "指定时间使用 YYYY-MM-DD HH:MM:SS 或 HH:MM:SS。"
            "请根据每次请求提供的当前时间解析今天和明天。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "time": {
                    "type": "string",
                    "description": (
                        "相对时间或明确时间，例如 30s、10m、"
                        "2026-09-12 08:00:00、08:00:00。"
                    ),
                },
                "repeat": {
                    "type": "string",
                    "enum": ["once", "daily", "weekdays", "weekends"],
                    "description": "重复周期，普通提醒默认 once。",
                },
                "label": {
                    "type": "string",
                    "description": "提醒内容，例如喝水、起床。",
                },
                "song_name": {
                    "type": "string",
                    "description": "可选歌曲名；当前提醒以语音播报为准。",
                },
            },
            "required": ["time"],
        },
        "examples": [
            {
                "user_query": "20秒后提醒我喝水",
                "answer": {"time": "20s", "repeat": "once", "label": "喝水"},
            },
            {
                "user_query": "工作日早上九点提醒我记录待办",
                "answer": {
                    "time": "09:00:00",
                    "repeat": "weekdays",
                    "label": "记录待办",
                },
            },
        ],
    },
}

list_alarms_function_desc = {
    "type": "function",
    "function": {
        "name": "list_alarms",
        "description": "查看当前设备设置的闹钟",
        "parameters": {"type": "object", "properties": {}, "required": []},
    },
}

delete_alarm_function_desc = {
    "type": "function",
    "function": {
        "name": "delete_alarm",
        "description": "删除当前设备的指定闹钟",
        "parameters": {
            "type": "object",
            "properties": {
                "alarm_id": {"type": "string", "description": "要删除的闹钟 ID"}
            },
            "required": ["alarm_id"],
        },
    },
}


def load_alarms(data_file=ALARM_DATA_FILE):
    """读取闹钟文件。格式损坏时保留原文件并返回空列表。"""
    path = Path(data_file)
    if not path.exists():
        return []
    try:
        with path.open("r", encoding="utf-8") as file:
            alarms = json.load(file)
        if not isinstance(alarms, list):
            raise ValueError("闹钟数据必须是列表")
        return alarms
    except Exception as exc:
        logger.bind(tag=TAG).error(f"加载闹钟数据失败: {exc}")
        return []


def save_alarms(alarms, data_file=ALARM_DATA_FILE):
    """在同一目录原子替换闹钟文件，避免写到一半造成数据损坏。"""
    path = Path(data_file)
    temp_name = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with _alarm_file_lock:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=path.parent,
                prefix=f".{path.name}.",
                suffix=".tmp",
                delete=False,
            ) as temp_file:
                temp_name = temp_file.name
                json.dump(alarms, temp_file, ensure_ascii=False, indent=2)
                temp_file.flush()
                os.fsync(temp_file.fileno())
            os.replace(temp_name, path)
            temp_name = None
        return True
    except Exception as exc:
        logger.bind(tag=TAG).error(f"保存闹钟数据失败: {exc}")
        return False
    finally:
        if temp_name:
            try:
                os.unlink(temp_name)
            except OSError:
                pass


def _parse_clock(value):
    for fmt in ("%H:%M:%S", "%H:%M"):
        try:
            return datetime.datetime.strptime(value, fmt).time()
        except ValueError:
            continue
    raise ValueError("时间格式无法识别")


def _repeat_allows_day(repeat, candidate):
    if repeat == "weekdays":
        return candidate.weekday() < 5
    if repeat == "weekends":
        return candidate.weekday() >= 5
    return True


def next_repeating_time(clock_text, repeat, after):
    """计算严格晚于 after 的下一次重复触发时间。"""
    clock = _parse_clock(clock_text)
    for day_offset in range(0, 8):
        day = after.date() + datetime.timedelta(days=day_offset)
        candidate = datetime.datetime.combine(day, clock)
        if candidate > after and _repeat_allows_day(repeat, candidate):
            return candidate
    raise ValueError(f"无法计算重复周期: {repeat}")


def _legacy_next_fire(alarm, now):
    """把旧版闹钟转换成下一次触发时间，不修改原始 time 字段。"""
    repeat = alarm.get("repeat", "once")
    target_timestamp = alarm.get("target_timestamp")
    if repeat == "once":
        if target_timestamp is not None:
            return datetime.datetime.fromtimestamp(float(target_timestamp))
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
            try:
                return datetime.datetime.strptime(alarm["time"], fmt)
            except ValueError:
                continue
        return next_repeating_time(alarm["time"], "daily", now)

    if target_timestamp is not None:
        candidate = datetime.datetime.fromtimestamp(float(target_timestamp))
        if candidate > now:
            return candidate

    clock = _parse_clock(alarm["time"])
    today_candidate = datetime.datetime.combine(now.date(), clock)
    lateness = (now - today_candidate).total_seconds()
    if (
        0 <= lateness <= REPEATING_DELIVERY_GRACE_SECONDS
        and _repeat_allows_day(repeat, today_candidate)
    ):
        return today_candidate
    return next_repeating_time(alarm["time"], repeat, now)


class AlarmManager:
    def __init__(self, data_file=ALARM_DATA_FILE):
        self.data_file = Path(data_file)
        self.alarms = load_alarms(self.data_file)
        self.running = False
        self.task = None
        self._lock = threading.RLock()

    def start(self, loop):
        if self.task and not self.task.done():
            return
        self.running = True
        self.task = loop.create_task(self.check_alarms())
        logger.bind(tag=TAG).info("闹钟检查任务已启动")

    async def stop(self):
        self.running = False
        if self.task and not self.task.done():
            self.task.cancel()
            try:
                await self.task
            except asyncio.CancelledError:
                pass
        self.task = None
        logger.bind(tag=TAG).info("闹钟检查任务已停止")

    def _save(self):
        return save_alarms(self.alarms, self.data_file)

    def add_alarm(self, alarm):
        with self._lock:
            self.alarms.append(alarm)
            if self._save():
                return True
            self.alarms.remove(alarm)
            return False

    def delete_alarm(self, alarm_id, device_id):
        with self._lock:
            alarm = next(
                (
                    item
                    for item in self.alarms
                    if item.get("id") == alarm_id
                    and item.get("device_id") == device_id
                ),
                None,
            )
            if alarm is None:
                return None, True
            self.alarms.remove(alarm)
            if self._save():
                return alarm, True
            self.alarms.append(alarm)
            return None, False

    def alarms_for_device(self, device_id):
        with self._lock:
            return [
                dict(alarm)
                for alarm in self.alarms
                if alarm.get("device_id") == device_id
            ]

    def _ensure_next_fire(self, alarm, now):
        value = alarm.get("next_fire_at")
        if value is not None:
            return datetime.datetime.fromtimestamp(float(value))
        next_fire = _legacy_next_fire(alarm, now)
        alarm["next_fire_at"] = next_fire.timestamp()
        return next_fire

    def _advance_repeating_alarm(self, alarm, now):
        next_fire = next_repeating_time(alarm["time"], alarm["repeat"], now)
        alarm["next_fire_at"] = next_fire.timestamp()
        alarm["target_timestamp"] = next_fire.timestamp()
        alarm["delivery_status"] = "pending"

    async def process_due_alarms(self, now=None):
        """处理所有已到期任务；返回本轮成功入队的数量。"""
        now = now or datetime.datetime.now()
        queued = 0
        changed = False

        with self._lock:
            alarms_snapshot = list(self.alarms)

        for alarm in alarms_snapshot:
            if not alarm.get("enabled", True):
                continue
            repeat = alarm.get("repeat", "once")
            if repeat not in VALID_REPEATS:
                alarm["enabled"] = False
                alarm["delivery_status"] = "invalid"
                changed = True
                continue

            try:
                next_fire = self._ensure_next_fire(alarm, now)
            except (KeyError, TypeError, ValueError) as exc:
                alarm["enabled"] = False
                alarm["delivery_status"] = "invalid"
                alarm["last_error"] = str(exc)
                changed = True
                continue

            if next_fire > now:
                continue

            lateness = (now - next_fire).total_seconds()
            grace = (
                ONCE_DELIVERY_GRACE_SECONDS
                if repeat == "once"
                else REPEATING_DELIVERY_GRACE_SECONDS
            )
            if lateness > grace:
                if repeat == "once":
                    alarm["enabled"] = False
                    alarm["delivery_status"] = "expired"
                else:
                    alarm["last_missed_at"] = now.isoformat(timespec="seconds")
                    self._advance_repeating_alarm(alarm, now)
                changed = True
                continue

            last_attempt = alarm.get("last_attempt_at")
            if last_attempt:
                try:
                    elapsed = now.timestamp() - float(last_attempt)
                    if elapsed < OFFLINE_RETRY_SECONDS:
                        continue
                except (TypeError, ValueError):
                    pass

            alarm["delivery_status"] = "delivering"
            alarm["last_attempt_at"] = now.timestamp()
            alarm["delivery_attempts"] = int(alarm.get("delivery_attempts", 0)) + 1
            if not self._save():
                alarm["delivery_status"] = "pending"
                continue

            try:
                delivery_id = await self.trigger_alarm(alarm)
            except DeviceNotReadyError as exc:
                alarm["delivery_status"] = "pending"
                alarm["last_error"] = str(exc)
                changed = True
                continue
            except Exception as exc:
                logger.bind(tag=TAG).error(
                    f"闹钟 {alarm.get('id')} 投递失败: {exc}"
                )
                alarm["delivery_status"] = "pending"
                alarm["last_error"] = str(exc)
                changed = True
                continue

            queued += 1
            alarm["delivery_status"] = "queued"
            alarm["last_delivery_id"] = delivery_id
            alarm["last_triggered_at"] = now.isoformat(timespec="seconds")
            alarm.pop("last_error", None)
            if repeat == "once":
                alarm["enabled"] = False
            else:
                self._advance_repeating_alarm(alarm, now)
            changed = True

        if changed:
            self._save()
        return queued

    async def check_alarms(self):
        while self.running:
            try:
                await self.process_due_alarms()
                await asyncio.sleep(1)
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.bind(tag=TAG).error(f"检查闹钟时出错: {exc}")
                await asyncio.sleep(1)

    async def trigger_alarm(self, alarm):
        device_id = alarm.get("device_id")
        if not device_id:
            raise DeviceNotReadyError("闹钟缺少设备 ID")
        current_conn = connection_manager.get_handler(device_id)
        if current_conn is None:
            raise DeviceNotReadyError("设备离线，等待重新连接后补发")

        label = alarm.get("label") or "闹钟"
        notify_text = f"时间到了！提醒您：{label}。"
        return await queue_text_notification(current_conn, notify_text)


alarm_manager = AlarmManager()


def _parse_alarm_time(value, repeat, now):
    relative = re.fullmatch(r"(?:\d+[smhd])+", value.strip(), re.IGNORECASE)
    if relative:
        seconds = 0
        for amount, unit in re.findall(r"(\d+)([smhd])", value.lower()):
            multiplier = {"s": 1, "m": 60, "h": 3600, "d": 86400}[unit]
            seconds += int(amount) * multiplier
        if seconds <= 0:
            raise ValueError("相对时间必须大于 0")
        return now + datetime.timedelta(seconds=seconds), "once", True

    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            target = datetime.datetime.strptime(value, fmt)
        except ValueError:
            continue
        if target <= now:
            raise ValueError("一次性闹钟时间已经过去")
        return target, "once", True

    clock = _parse_clock(value)
    if repeat == "once":
        target = datetime.datetime.combine(now.date(), clock)
        if target <= now:
            target += datetime.timedelta(days=1)
    else:
        target = next_repeating_time(value, repeat, now)
    return target, repeat, False


@register_function("set_alarm", set_alarm_function_desc, ToolType.SYSTEM_CTL)
def set_alarm(conn, time, repeat="once", label=None, song_name=""):
    try:
        repeat = repeat or "once"
        if repeat not in VALID_REPEATS:
            return ActionResponse(
                Action.REQLLM,
                "重复周期无效，请使用 once、daily、weekdays 或 weekends",
                None,
            )
        device_id = getattr(conn, "device_id", None)
        if not device_id:
            return ActionResponse(Action.REQLLM, "设备身份缺失，无法设置闹钟", None)

        now = datetime.datetime.now()
        target, repeat, has_date = _parse_alarm_time(str(time), repeat, now)
        storage_time = (
            target.strftime("%Y-%m-%d %H:%M:%S")
            if has_date
            else target.strftime("%H:%M:%S")
        )
        alarm = {
            "id": f"alarm_{uuid.uuid4().hex}",
            "device_id": device_id,
            "time": storage_time,
            "target_timestamp": target.timestamp(),
            "next_fire_at": target.timestamp(),
            "repeat": repeat,
            "label": label or f"{time}的闹钟",
            "song_name": song_name or "",
            "enabled": True,
            "delivery_status": "pending",
            "delivery_attempts": 0,
            "created_at": now.isoformat(timespec="seconds"),
        }
        if not alarm_manager.add_alarm(alarm):
            return ActionResponse(Action.REQLLM, "闹钟保存失败，请稍后重试", None)

        if not alarm_manager.running and getattr(conn, "loop", None):
            alarm_manager.start(conn.loop)

        repeat_text = {
            "once": "仅一次",
            "daily": "每天",
            "weekdays": "工作日",
            "weekends": "周末",
        }[repeat]
        response = (
            f"已设置闹钟：{alarm['label']}，"
            f"{target.strftime('%Y-%m-%d %H:%M:%S')}，{repeat_text}"
        )
        return ActionResponse(Action.REQLLM, response, None)
    except ValueError as exc:
        return ActionResponse(Action.REQLLM, f"时间格式无法识别：{exc}", None)
    except Exception as exc:
        logger.bind(tag=TAG).error(f"设置闹钟失败: {exc}")
        return ActionResponse(Action.REQLLM, "设置闹钟失败，请稍后重试", None)


@register_function("list_alarms", list_alarms_function_desc, ToolType.SYSTEM_CTL)
def list_alarms(conn):
    device_id = getattr(conn, "device_id", None)
    alarms = alarm_manager.alarms_for_device(device_id)
    if not alarms:
        return ActionResponse(Action.REQLLM, "当前设备没有设置闹钟", None)

    status_text = {
        "pending": "待触发",
        "delivering": "投递中",
        "queued": "已触发",
        "expired": "已过期",
        "invalid": "数据无效",
    }
    lines = ["当前设备设置的闹钟："]
    for index, alarm in enumerate(
        sorted(alarms, key=lambda item: item.get("time", "")), 1
    ):
        status = status_text.get(alarm.get("delivery_status"), "启用")
        if not alarm.get("enabled", True) and status == "启用":
            status = "已停用"
        lines.append(
            f"{index}. [{status}] {alarm.get('time')} - "
            f"{alarm.get('label', '闹钟')}，ID: {alarm.get('id')}"
        )
    return ActionResponse(Action.REQLLM, "\n".join(lines), None)


@register_function("delete_alarm", delete_alarm_function_desc, ToolType.SYSTEM_CTL)
def delete_alarm(conn, alarm_id):
    device_id = getattr(conn, "device_id", None)
    alarm, saved = alarm_manager.delete_alarm(alarm_id, device_id)
    if not saved:
        return ActionResponse(Action.REQLLM, "删除失败，闹钟数据未改变", None)
    if alarm is None:
        return ActionResponse(Action.REQLLM, f"未找到 ID 为 {alarm_id} 的闹钟", None)
    return ActionResponse(
        Action.REQLLM,
        f"已删除闹钟：{alarm.get('label', '闹钟')} ({alarm.get('time')})",
        None,
    )
