"""A connection-scoped Chinese startup discussion podcast."""

import json
import re
from functools import lru_cache
from pathlib import Path

from core.paper_podcast.bgm import set_enabled as set_bgm_enabled


CONTENT_PATH = Path(__file__).parent / "content" / "founder_startup.md"


@lru_cache(maxsize=1)
def load_podcast_context() -> str:
    return CONTENT_PATH.read_text(encoding="utf-8").strip()


class PaperPodcastService:
    """Switch a live Xiaozhi connection into an interactive startup discussion."""

    title = "创业：马斯克、黄仁勋与乔布斯的不同选择"
    openings = {
        "jobs": (
            "创业播客开始。乔布斯回到苹果后，iMac 用鲜明的设计呈现了完整的电脑体验。"
            "这段经历有意思的地方，不只是产品好看，而是团队如何把资源集中到少数选择上。"
            "我们先聊这个取舍：哪些东西值得坚持，哪些该暂时放下？"
        ),
        "huang": (
            "创业播客开始。英伟达起步时做的是图形芯片，后来的 CUDA 和人工智能机会，"
            "并不是当时就能确定的结局。我们从黄仁勋的经历聊起："
            "一家公司的长期技术投入，怎样与眼前的生存压力相处？"
        ),
        "musk": (
            "创业播客开始。按马斯克的公开回顾，猎鹰一号前三次发射失败，第四次才成功。"
            "事后看像坚持的故事，但当时没人能保证下一次会成功。"
            "我们先聊这道难题：冒险什么时候值得继续，什么时候该设止损点？"
        ),
        "compare": (
            "创业播客开始。乔布斯、黄仁勋和马斯克都做过很难的长期选择，"
            "但他们面对的公司、市场和资源并不相同。咱们先从一个具体决定讲起，"
            "再看另外两人会怎样处理类似的矛盾。你想先听哪一位？"
        ),
    }

    def build_prompt(self, base_prompt: str, user_preferences: str = "") -> str:
        # 只沿用功能和事实上下文。普通聊天的身份、情绪、句式和长度模板
        # 会强制撒娇、短答或追问，不能与播客风格一起注入。
        preserved = []
        for tag in ("tool_calling", "context", "memory", "user_preferences"):
            preserved.extend(
                re.findall(rf"<{tag}>.*?</{tag}>", base_prompt, flags=re.DOTALL)
            )
        if user_preferences.strip():
            preserved.append(
                f"<user_preferences>\n{user_preferences.strip()}\n</user_preferences>"
            )
        shared_context = "\n\n".join(preserved)
        return f"""{shared_context}

<startup_context>
{load_podcast_context()}
</startup_context>

<paper_podcast_mode>
你是一位中文创业播客的讨论伙伴，与用户共同讨论本期主题《{self.title}》。讨论主要借鉴埃隆·马斯克、黄仁勋和史蒂夫·乔布斯的公开传记及可靠公开资料，分析创业者在产品、技术、组织、融资、市场和个人代价之间所做的真实选择。
你的任务不是歌颂三位人物，也不是模仿他们说话，而是借助可核实的经历，帮助用户理解创业决策的条件、代价、风险与边界。你应当有自己的判断，可以赞同、补充或反对用户，但要说明理由。

【对话原则】

主持顺序是：先满足用户这轮明确提出的要求，再处理没听清或要求复述，再延续当前分歧，最后才由你主动转场。用户要听三人的例子就比较三人；不要擅自改成让用户做创业选择题。用户只说“明白了”“嗯，继续”时，沿刚才的故事增加一层事实、代价或反例，不只回答“嗯”或重新开场。用户问“刚才哪两个点”等内容时，只准确、简短地复述原来的要点，不引入新论证。

通常每轮抓住一个最关键的创业矛盾，例如速度与质量、控制权与融资、技术理想与市场需求、创始人意志与组织健康。用户明确要求比较或列举时，完整回应这个范围，不用单一矛盾规则回避请求。

当某段真实经历有助于解释问题时，从三人中选择最相关的一位作为案例，不要求每轮同时提到三个人。案例之后重点分析：

这项选择在什么条件下可能有效；
它付出了什么代价；
换一个团队、阶段或市场后是否仍然成立；
是否存在反例、幸存者偏差或时代因素。

不要把成功者的做法总结成普遍创业定律，也不要为了出现名人而生硬插入案例。如果用户的问题用直接的商业分析就能回答，可以先分析，再在必要时补充传记视角。

【互动方式】

把用户视为同一场播客中的另一位讨论者，而不是听课的学生。语气平等、自然、直接，有判断但不居高临下。

一轮可以讲故事、分析、比较、复述或提出问题；每轮都应带来一点新内容。先讲清自己的判断或一个具体场景，再决定是否需要提问。只有用户的答案会改变接下来讨论的方向，或当前分歧需要知道他的理由时，才主动问一个紧贴话题的问题；不要连续几轮只追问，也不要例行问“你怎么看”。一段讨论已有结论时，用一句话收束，再自然转到另一位人物或相反的案例。用户主动换题时立即跟随。

如果用户说“我不同意”，先识别具体分歧，说明你接受或反对哪一部分及其理由。不要立即附和，也不要重新开场。

【表达风格】

使用自然的中文口语段落，像成熟的播客讨论，而不是论文、课程讲义或励志演讲。

回答长度服从内容需要：复述或澄清通常在十几秒内说清，普通讨论约半分钟，用户明确要听故事或比较时可以讲得更完整。不要机械按字数裁剪，也不要每轮套用“案例—分析—提问”。口播时用完整的短句承载要点，避免“我分三点说：”“关键是：”这类悬空开头和难记的长清单；如果确需列举，先说最重要的一点。

【工具与会话状态】

创业案例、人物比较、观点讨论、质疑以及“继续”等普通对话，直接根据知识包和当前对话回答，不重复调用 paper_podcast.start。

只有在尚未进入播客模式、且外部系统明确要求初始化时，才调用 paper_podcast.start。不得把准备调用工具说成已经完成。

当用户明确表示“结束创业播客”“退出播客”“不聊了”或其他含义清楚的结束指令时，调用 paper_podcast.stop。工具成功后，退出本模式并恢复普通聊天风格。

提醒、设备控制或其他真实操作继续遵循相应工具规则，不得假装已经执行。

上面的 startup_context 是公开资料整理；其中的讨论角度是分析，不是传记原话或已经证明的因果结论。
</paper_podcast_mode>"""

    def start(self, conn) -> str:
        if getattr(conn, "paper_podcast_active", False):
            return "创业播客已经开始了，我们接着刚才的话题聊。"

        original_prompt = getattr(conn, "prompt", "") or ""
        settings = getattr(conn, "config", {}).get("paper_podcast") or {}
        preferences = settings.get("user_preferences") or ""
        prompt = self.build_prompt(original_prompt, preferences)
        conn.paper_podcast_original_prompt = original_prompt
        conn.paper_podcast_original_close_after_chat = getattr(
            conn, "close_after_chat", False
        )
        conn.change_system_prompt(prompt)
        conn.paper_podcast_active = True
        conn.close_after_chat = False
        if settings.get("bgm_enabled", True):
            set_bgm_enabled(conn, True)
        return self._opening_for(conn)

    def _opening_for(self, conn) -> str:
        last_user_text = ""
        dialogue = getattr(getattr(conn, "dialogue", None), "dialogue", [])
        for message in reversed(dialogue):
            if getattr(message, "role", None) == "user":
                last_user_text = getattr(message, "content", "") or ""
                break

        if any(word in last_user_text for word in ("三位", "比较", "对比")):
            return self.openings["compare"]
        for name, key in (("乔布斯", "jobs"), ("黄仁勋", "huang"), ("马斯克", "musk")):
            if name in last_user_text:
                return self.openings[key]

        # 未指定人物时在不同连接间变换切入点，避免反复播放同一道选择题。
        session_id = str(getattr(conn, "session_id", "") or "")
        keys = ("jobs", "huang", "musk")
        return self.openings[keys[sum(session_id.encode("utf-8")) % len(keys)]]

    def stop(self, conn) -> str:
        if not getattr(conn, "paper_podcast_active", False):
            return "当前没有进行创业播客。"

        original_prompt = getattr(conn, "paper_podcast_original_prompt", "") or ""
        conn.change_system_prompt(original_prompt)
        conn.paper_podcast_active = False
        set_bgm_enabled(conn, False)
        conn.paper_podcast_original_prompt = None
        conn.close_after_chat = conn.paper_podcast_original_close_after_chat
        conn.paper_podcast_original_close_after_chat = None
        return "本次创业播客结束了。刚才的讨论仍保留在当前对话里，需要时可以继续。"

    @staticmethod
    def response_options(conn) -> dict:
        # 每次请求覆盖上限，不修改共享 LLM 实例，退出后自动恢复普通聊天设置。
        if getattr(conn, "paper_podcast_active", False):
            return {"max_tokens": 1200, "temperature": 0.5}
        return {}

    @staticmethod
    def prepare_messages(conn, messages: list) -> list:
        """标明用户原话，避免长篇助手分析盖过用户的纠正；不改写会话历史。"""
        if not getattr(conn, "paper_podcast_active", False):
            return messages
        user_words = []
        remaining = 600
        # 最后一条是本次问题，原样留在 user 消息中；这里只补充之前的用户原话。
        for message in reversed(messages[:-1]):
            content = message.get("content")
            if message.get("role") != "user" or not isinstance(content, str):
                continue
            if len(user_words) == 3 or len(content) > remaining:
                break
            user_words.append(content)
            remaining -= len(content)
        if not user_words:
            return messages
        reminder = (
            "\n\n以下按时间先后列出最近的用户原话，仅用来核对观点归属。"
            "回顾用户立场时以这些原话为准，并保留后续纠正和限定；"
            "只概括用户亲口表达的意思，不补足论据、不替用户延伸立场。"
            "用户只说了一个理由，不能把助手后来扩展的其他理由、方案或建议也归给用户，"
            "即使这些延伸听起来合理。用户没有明确认可，就仍然是助手的分析。"
            "这些历史话语不替代本轮问题。\n"
            + json.dumps(list(reversed(user_words)), ensure_ascii=False)
        )
        return [
            {**message, "content": message["content"] + reminder}
            if message.get("role") == "system" else message
            for message in messages
        ]

    @staticmethod
    def status(conn) -> str:
        if getattr(conn, "paper_podcast_active", False):
            return "正在聊创业，结合马斯克、黄仁勋和乔布斯的经历讨论。"
        return "当前没有进行创业播客。"
