from core.paper_podcast.service import PaperPodcastService, load_podcast_context
from core.utils.dialogue import Dialogue, Message


class StubConnection:
    def __init__(self):
        self.prompt = "你是小智。"
        self.close_after_chat = True
        self.config = {}
        self.dialogue = Dialogue()
        self.dialogue.update_system_message(self.prompt)

    def change_system_prompt(self, prompt):
        self.prompt = prompt
        self.dialogue.update_system_message(prompt)


def test_startup_context_has_three_founders_and_marks_source_limits():
    context = load_podcast_context()
    for name in ["马斯克", "黄仁勋", "乔布斯"]:
        assert name in context
    assert "没有公开材料支撑" in context
    assert "并未导入三本书的全文" in context


def test_start_and_stop_restore_original_prompt():
    conn = StubConnection()
    service = PaperPodcastService()

    opening = service.start(conn)

    assert conn.paper_podcast_active is True
    assert conn.close_after_chat is False
    assert "中文创业播客" in conn.prompt
    assert "黄仁勋" in conn.prompt
    assert "创业播客开始" in opening

    closing = service.stop(conn)

    assert conn.paper_podcast_active is False
    assert conn.prompt == "你是小智。"
    assert conn.close_after_chat is True
    assert "创业播客结束" in closing


def test_start_is_idempotent():
    conn = StubConnection()
    service = PaperPodcastService()

    service.start(conn)
    prompt = conn.prompt
    response = service.start(conn)

    assert conn.prompt == prompt
    assert "已经开始" in response
    assert "？" not in response


def test_podcast_drops_legacy_persona_and_preserves_functional_context():
    conn = StubConnection()
    original = """<identity>你是一个小女孩，每句话都要撒娇。</identity>
<communication_length_constraint>最多300字，结尾问要不要继续。</communication_length_constraint>
<emotion>每段开头加emoji。</emotion>
<tool_calling>操作设备必须调用工具，不能假装已执行。</tool_calling>
<context>用户所在城市：北京</context>
<memory>用户在做科研软件。</memory>
<user_preferences>称呼用户为老杨。</user_preferences>"""
    conn.change_system_prompt(original)
    conn.config = {"paper_podcast": {"user_preferences": "先给具体例子，再解释术语。"}}
    service = PaperPodcastService()
    service.start(conn)

    assert "你是一个小女孩" not in conn.prompt
    assert "最多300字" not in conn.prompt
    assert "<emotion>" not in conn.prompt
    for retained in ["操作设备必须调用工具", "用户所在城市：北京", "用户在做科研软件", "称呼用户为老杨", "先给具体例子"]:
        assert retained in conn.prompt
    service.stop(conn)
    assert conn.prompt == original
    assert conn.dialogue.dialogue[0].content == original


def test_mode_switch_keeps_user_views_and_discussion_history():
    conn = StubConnection()
    user_view = Message(role="user", content="我认为创业初期先验证付费意愿。")
    conn.dialogue.put(user_view)
    service = PaperPodcastService()
    service.start(conn)
    answer = Message(role="assistant", content="先找愿意付费的人，能更快验证需求。")
    conn.dialogue.put(answer)
    service.stop(conn)
    assert conn.dialogue.dialogue[1:] == [user_view, answer]


def test_response_budget_is_scoped_to_connection_and_restored_on_exit():
    podcast = StubConnection()
    ordinary = StubConnection()
    service = PaperPodcastService()
    assert service.response_options(podcast) == {}
    service.start(podcast)
    assert service.response_options(podcast) == {"max_tokens": 1200, "temperature": 0.5}
    assert service.response_options(ordinary) == {}
    service.stop(podcast)
    assert service.response_options(podcast) == {}


def test_repeated_start_does_not_replace_restore_snapshot():
    conn = StubConnection()
    service = PaperPodcastService()
    service.start(conn)
    service.start(conn)
    service.stop(conn)
    assert conn.prompt == "你是小智。"
    assert conn.close_after_chat is True
    assert service.stop(conn) == "当前没有进行创业播客。"


def test_opening_follows_requested_founder_and_context_fits_budget():
    conn = StubConnection()
    conn.dialogue.put(Message(role="user", content="开始创业播客，先讲黄仁勋"))
    opening = PaperPodcastService().start(conn)
    assert "英伟达" in opening
    assert "六个月" not in opening
    assert len(conn.prompt) < 30000


def test_comparison_request_does_not_turn_into_a_personal_quiz():
    conn = StubConnection()
    conn.dialogue.put(Message(role="user", content="开始创业播客，比较三位创始人"))
    opening = PaperPodcastService().start(conn)
    assert all(name in opening for name in ("乔布斯", "黄仁勋", "马斯克"))
    assert "愿意付费的用户" not in opening


def test_context_keeps_key_facts_and_avoids_biography_fabrication():
    context = load_podcast_context()
    for evidence in ["2004 年", "1993 年", "2007 年", "幸存者偏差", "不应编造为传记原文"]:
        assert evidence in context


def test_user_view_reminder_quotes_users_only_and_does_not_mutate_history():
    conn = StubConnection()
    service = PaperPodcastService()
    service.start(conn)
    messages = [
        {"role": "system", "content": "播客提示词"},
        {"role": "user", "content": "我反对用完全相同的预算比较，记忆维护有额外成本。"},
        {"role": "assistant", "content": "我建议做成本收益分析。"},
        {"role": "user", "content": "我的看法是什么？"},
    ]
    prepared = service.prepare_messages(conn, messages)
    assert "记忆维护有额外成本" in prepared[0]["content"]
    assert "我建议做成本收益分析" not in prepared[0]["content"]
    assert "我的看法是什么" not in prepared[0]["content"]
    assert messages[0]["content"] == "播客提示词"
    assert prepared[1:] == messages[1:]
    service.stop(conn)
    assert service.prepare_messages(conn, messages) is messages


def test_user_view_reminder_is_bounded_without_truncating_quoted_statements():
    conn = StubConnection()
    service = PaperPodcastService()
    service.start(conn)
    messages = [{"role": "system", "content": "系统"}]
    messages += [{"role": "user", "content": str(i) + "观点" * 150} for i in range(20)]
    messages += [{"role": "user", "content": "请回顾"}]
    prepared = service.prepare_messages(conn, messages)
    assert len(prepared[0]["content"]) < 2300
    assert messages[-2]["content"] in prepared[0]["content"]
    assert messages[1]["content"] not in prepared[0]["content"]
