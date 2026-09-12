from llamacracy.search import extract_query


def test_strips_vocative_greeting():
    # The bug report: "Hey Gemma" leaked "Gemma" into the query and pulled in
    # an unrelated NVIDIA forum thread that happened to mention Gemma.
    q = extract_query("Hey Gemma, can you tell me what NVIDIA has been up to lately?")
    assert "gemma" not in q.lower()
    assert "nvidia" in q.lower()


def test_strips_wrapper_phrase_without_greeting():
    q = extract_query("Can you tell me about the latest SpaceX launch?")
    assert q.lower().startswith("the latest spacex launch") or "spacex" in q.lower()
    assert "can you tell me" not in q.lower()


def test_strips_both_greeting_and_wrapper():
    q = extract_query("Hi Qwen, could you tell me about the weather in Portland?")
    assert "qwen" not in q.lower()
    assert "could you tell me" not in q.lower()
    assert "portland" in q.lower()


def test_falls_back_when_nothing_left():
    # A greeting-only message shouldn't be stripped down to an empty query.
    q = extract_query("Hey Gemma")
    assert q  # non-empty
    assert "gemma" in q.lower()


def test_leaves_plain_query_alone():
    q = extract_query("2026 nba finals winner")
    assert q == "2026 nba finals winner"


def test_empty_and_whitespace():
    assert extract_query("") == ""
    assert extract_query("   ") == ""


def test_caps_length():
    long_msg = "search for " + ("x" * 500)
    q = extract_query(long_msg)
    assert len(q) <= 200
