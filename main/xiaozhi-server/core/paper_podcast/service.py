"""A small, connection-scoped podcast mode for one preprocessed paper."""

from functools import lru_cache
from pathlib import Path


CONTENT_PATH = Path(__file__).parent / "content" / "autosci_2605_31468.md"


@lru_cache(maxsize=1)
def load_paper_context() -> str:
    return CONTENT_PATH.read_text(encoding="utf-8").strip()


class PaperPodcastService:
    """Switch a live Xiaozhi connection into an interactive paper discussion."""

    title = "AutoSci：面向完整科研生命周期的记忆中心智能体系统"

    def build_prompt(self, base_prompt: str) -> str:
        return f"""{base_prompt.strip()}

<paper_podcast_mode>
你现在是中文论文播客的共同主持人，用户是另一位参与讨论的人。当前只讨论论文《{self.title}》。

交流规则：
1. 使用自然的中文口语。先回应用户刚才的观点，再补充、质疑或追问；不要像朗读摘要或上课。
2. 默认每轮回答二到四句、最多约一百八十个汉字，并且最多问一个问题。用户明确要求展开时可以详细回答。
3. 优先讨论论文的核心主张、系统设计、实验是否支持结论、局限性，以及它对实际科研智能体的启发。
4. 论文没有提供的信息必须明确说“论文没有说明”；你的分析或推断要明确标为推断，不能伪装成论文结论。
5. 不逐条讲参考文献，不长段引用英文原文。术语首次出现时使用“英文名加简短中文解释”。
6. 可以礼貌地不同意用户，并用论文中的事实说明理由。避免每轮都用“你说得很好”等机械客套话。
7. 用户说结束论文播客、退出共读或不聊了时，调用 paper_podcast 的 stop 动作。

以下是从论文正文提炼的知识包，参考文献和附录已排除：

<paper_context>
{load_paper_context()}
</paper_context>
</paper_podcast_mode>"""

    def start(self, conn) -> str:
        if getattr(conn, "paper_podcast_active", False):
            return "论文播客已经开始了。我们继续聊：你现在最想质疑它的系统设计，还是实验结论？"

        conn.paper_podcast_original_prompt = getattr(conn, "prompt", "") or ""
        conn.paper_podcast_active = True
        conn.change_system_prompt(self.build_prompt(conn.paper_podcast_original_prompt))
        conn.close_after_chat = False
        return (
            "论文播客开始。这篇论文提出AutoSci，想把文献、想法、实验、写作和审稿回复"
            "连成一个能长期记忆、还能改进自身流程的科研系统。你觉得科研智能体最难的是"
            "推理能力，还是长期管理研究过程？"
        )

    def stop(self, conn) -> str:
        if not getattr(conn, "paper_podcast_active", False):
            return "当前没有进行论文播客。"

        original_prompt = getattr(conn, "paper_podcast_original_prompt", "") or ""
        conn.change_system_prompt(original_prompt)
        conn.paper_podcast_active = False
        conn.paper_podcast_original_prompt = None
        return "本次论文播客结束了。刚才的讨论仍保留在当前对话里，需要时可以继续。"

    @staticmethod
    def status(conn) -> str:
        if getattr(conn, "paper_podcast_active", False):
            return "正在讨论AutoSci这篇论文。"
        return "当前没有进行论文播客。"
