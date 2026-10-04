from core.paper_podcast.service import PaperPodcastService, load_paper_context


class StubConnection:
    def __init__(self):
        self.prompt = "你是小智。"
        self.close_after_chat = True

    def change_system_prompt(self, prompt):
        self.prompt = prompt


def test_paper_context_contains_claims_and_excludes_references():
    context = load_paper_context()
    assert "SciMem" in context
    assert "1.52 倍" in context
    assert "论文自己承认的局限" in context
    assert "参考文献" not in context


def test_start_and_stop_restore_original_prompt():
    conn = StubConnection()
    service = PaperPodcastService()

    opening = service.start(conn)

    assert conn.paper_podcast_active is True
    assert conn.close_after_chat is False
    assert "中文论文播客" in conn.prompt
    assert "SciEvolve" in conn.prompt
    assert "论文播客开始" in opening

    closing = service.stop(conn)

    assert conn.paper_podcast_active is False
    assert conn.prompt == "你是小智。"
    assert "论文播客结束" in closing


def test_start_is_idempotent():
    conn = StubConnection()
    service = PaperPodcastService()

    service.start(conn)
    prompt = conn.prompt
    response = service.start(conn)

    assert conn.prompt == prompt
    assert "已经开始" in response
