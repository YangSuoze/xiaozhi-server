"""Voice entry point for the AutoSci interactive paper podcast."""

from core.paper_podcast import PaperPodcastService
from plugins_func.register import Action, ActionResponse, ToolType, register_function


PAPER_PODCAST_DESC = {
    "type": "function",
    "function": {
        "name": "paper_podcast",
        "description": (
            "控制AutoSci论文的中文互动播客模式。用户说开始论文播客、讨论AutoSci、"
            "聊这篇论文时使用start；明确说结束论文播客、退出共读或不聊了时使用stop；"
            "询问是否正在论文模式时使用status。进入模式后的普通论文讨论不需要重复调用。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["start", "stop", "status"],
                    "description": "要执行的论文播客动作。",
                }
            },
            "required": ["action"],
        },
        "examples": [
            {"user_query": "开始论文播客", "answer": {"action": "start"}},
            {"user_query": "我们来讨论AutoSci这篇论文", "answer": {"action": "start"}},
            {"user_query": "结束论文播客", "answer": {"action": "stop"}},
        ],
    },
}


@register_function("paper_podcast", PAPER_PODCAST_DESC, ToolType.SYSTEM_CTL)
def paper_podcast(conn, action: str):
    service = PaperPodcastService()
    if action == "start":
        response = service.start(conn)
    elif action == "stop":
        response = service.stop(conn)
    elif action == "status":
        response = service.status(conn)
    else:
        return ActionResponse(Action.ERROR, response="不支持的论文播客动作。")
    return ActionResponse(Action.RESPONSE, response=response)
