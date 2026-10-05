"""Voice entry point for the interactive podcast topics."""

from core.paper_podcast import PaperPodcastService
from plugins_func.register import Action, ActionResponse, ToolType, register_function


PAPER_PODCAST_DESC = {
    "type": "function",
    "function": {
        "name": "paper_podcast",
        "description": (
            "控制中文互动播客。话题 startup 是马斯克、黄仁勋、乔布斯的创业选择；"
            "话题 llm 是大模型发展对工作、教育、科技和社会的影响。"
            "用户明确说开始创业播客时用 start、topic=startup；说开始大模型播客、"
            "进入大模型话题或切换到大模型播客时用 start、topic=llm。"
            "在播客里继续讨论、要例子或质疑观点都直接聊天，不重复调用 start。"
            "明确退出播客时用 stop；询问当前播客状态时用 status。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["start", "stop", "status"],
                    "description": "要执行的播客动作。",
                },
                "topic": {
                    "type": "string",
                    "enum": ["startup", "llm"],
                    "description": "start 时选择话题；startup=创业，llm=大模型发展与影响。省略时默认为创业。",
                },
            },
            "required": ["action"],
        },
        "examples": [
            {"user_query": "开始创业播客", "answer": {"action": "start", "topic": "startup"}},
            {"user_query": "聊聊马斯克、黄仁勋和乔布斯的创业故事", "answer": {"action": "start", "topic": "startup"}},
            {"user_query": "开始大模型播客", "answer": {"action": "start", "topic": "llm"}},
            {"user_query": "切换到大模型发展话题", "answer": {"action": "start", "topic": "llm"}},
            {"user_query": "结束创业播客", "answer": {"action": "stop"}},
        ],
    },
}


@register_function("paper_podcast", PAPER_PODCAST_DESC, ToolType.SYSTEM_CTL)
def paper_podcast(conn, action: str, topic: str = "startup"):
    service = PaperPodcastService()
    if action == "start":
        if topic not in ("startup", "llm"):
            return ActionResponse(Action.ERROR, response="不支持这个播客话题。")
        response = service.start(conn, topic)
    elif action == "stop":
        response = service.stop(conn)
    elif action == "status":
        response = service.status(conn)
    else:
        return ActionResponse(Action.ERROR, response="不支持的播客动作。")
    return ActionResponse(Action.RESPONSE, response=response)
