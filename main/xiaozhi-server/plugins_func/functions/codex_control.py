"""Voice actions for controlling existing Codex desktop tasks."""

from core.codex import get_codex_control_service
from plugins_func.register import Action, ActionResponse, ToolType, register_function

CODEX_CONTROL_DESC = {
    "type": "function",
    "function": {
        "name": "codex_control",
        "description": (
            "控制电脑上的Codex桌面任务。用户明确提到Codex，或者当前对话已经"
            "进入Codex模式、正在等待选择或等待回答时调用。"
            "支持进入Codex模式、选择最近任务、发送或补充指令、回答Codex问题、"
            "查询状态和具体进展、回顾任务历史、设置播报、停止监控和退出。"
            "用户问之前做过什么、还剩什么、遇到过什么问题时使用ask_task；只问当前"
            "状态或当前步骤时使用status。进入模式后用户说‘第一个’等"
            "序号是在选择任务；选中任务后的普通工作要求使用send_instruction。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": [
                        "enter",
                        "switch_task",
                        "repeat_tasks",
                        "select_task",
                        "send_instruction",
                        "respond",
                        "status",
                        "ask_task",
                        "set_announcements",
                        "stop_monitor",
                        "exit",
                    ],
                    "description": "要执行的Codex控制动作。",
                },
                "text": {
                    "type": "string",
                    "description": (
                        "选择任务时传用户说的序号或名称；发送指令、回答Codex问题、"
                        "询问任务历史时传完整原话。其他动作不需要。"
                    ),
                },
                "enabled": {
                    "type": "boolean",
                    "description": "set_announcements时表示开启或关闭定时播报。",
                },
                "interval_minutes": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 120,
                    "description": "播报间隔分钟数，用户未指定时不传。",
                },
                "delivery_mode": {
                    "type": "string",
                    "enum": ["after_current", "steer"],
                    "description": (
                        "指令发送方式。默认after_current；用户明确说立即补充到"
                        "当前工作时使用steer。"
                    ),
                },
            },
            "required": ["action"],
        },
        "examples": [
            {"user_query": "请切换Codex模式", "answer": {"action": "enter"}},
            {
                "user_query": "选择第一个任务",
                "answer": {"action": "select_task", "text": "第一个"},
            },
            {
                "user_query": "让Codex检查服务器日志",
                "answer": {
                    "action": "send_instruction",
                    "text": "检查服务器日志",
                },
            },
            {"user_query": "Codex现在做到哪一步了", "answer": {"action": "status"}},
            {
                "user_query": "这个任务之前做了什么，还有什么没解决？",
                "answer": {
                    "action": "ask_task",
                    "text": "之前做了什么，还有什么没解决？",
                },
            },
            {
                "user_query": "需要，每五分钟告诉我一次",
                "answer": {
                    "action": "set_announcements",
                    "enabled": True,
                    "interval_minutes": 5,
                },
            },
            {
                "user_query": "不用定时播报",
                "answer": {"action": "set_announcements", "enabled": False},
            },
            {"user_query": "退出Codex模式", "answer": {"action": "exit"}},
        ],
    },
}


@register_function("codex_control", CODEX_CONTROL_DESC, ToolType.SYSTEM_CTL)
def codex_control(
    conn,
    action: str,
    text: str | None = None,
    enabled: bool | None = None,
    interval_minutes: int | None = None,
    delivery_mode: str = "after_current",
):
    device_id = getattr(conn, "device_id", None)
    if not device_id:
        return ActionResponse(Action.RESPONSE, response="无法识别当前音箱设备。")
    service = get_codex_control_service(conn.config)
    response = service.handle_action(
        device_id=device_id,
        action=action,
        text=text,
        enabled=enabled,
        interval_minutes=interval_minutes,
        delivery_mode=delivery_mode,
        llm=getattr(conn, "llm", None),
    )
    session = service.store.get_session(device_id)
    conn.codex_mode_expires_at = (
        session.get("expires_at") if session.get("active") else None
    )
    if session.get("active"):
        conn.close_after_chat = False
    return ActionResponse(Action.RESPONSE, response=response)
