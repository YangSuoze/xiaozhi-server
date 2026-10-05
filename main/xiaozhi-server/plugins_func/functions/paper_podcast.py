"""Voice entry point for the interactive startup podcast."""

from core.paper_podcast import PaperPodcastService
from plugins_func.register import Action, ActionResponse, ToolType, register_function


PAPER_PODCAST_DESC = {
    "type": "function",
    "function": {
        "name": "paper_podcast",
        "description": (
            "控制结合马斯克、黄仁勋、乔布斯传记视角的中文创业播客。"
            "用户说开始创业播客、进入创业模式、聊三位创始人的创业故事时使用start；"
            "明确说结束创业播客、退出播客或不聊了时使用stop；"
            "询问当前是否在创业播客时使用status。进入模式后的普通创业讨论不需要重复调用。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["start", "stop", "status"],
                    "description": "要执行的创业播客动作。",
                }
            },
            "required": ["action"],
        },
        "examples": [
            {"user_query": "开始创业播客", "answer": {"action": "start"}},
            {"user_query": "聊聊马斯克、黄仁勋和乔布斯的创业故事", "answer": {"action": "start"}},
            {"user_query": "结束创业播客", "answer": {"action": "stop"}},
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
        return ActionResponse(Action.ERROR, response="不支持的创业播客动作。")
    return ActionResponse(Action.RESPONSE, response=response)
