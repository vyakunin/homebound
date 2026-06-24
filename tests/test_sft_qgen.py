"""Tests for the grounded-QA question generator (blog.sft_qgen).

No network: a fake OpenAI-compatible client returns canned content, so we test
the prompt assembly, JSON extraction, and the verbatim-span faithfulness gate.
"""
import pytest

import tests.django_setup  # noqa: F401 — must run before any Django imports
from blog.sft_qgen import (
    GroundingVerdict,
    QGenCallError,
    QGenItem,
    _extract_json_array,
    _parse_verdict,
    _retry_call,
    _span_is_faithful,
    build_messages,
    generate_qa,
    judge_grounding,
    parse_items,
)


@pytest.fixture
def no_sleep(monkeypatch):
    """Stub the backoff sleep so requeue tests run instantly."""
    monkeypatch.setattr("blog.sft_qgen._sleep", lambda _s: None)


class _FakeMessage:
    def __init__(self, content):
        self.content = content


class _FakeChoice:
    def __init__(self, content):
        self.message = _FakeMessage(content)


class _FakeResp:
    def __init__(self, content):
        self.choices = [_FakeChoice(content)]


class _FakeClient:
    """Minimal OpenAI-compatible stub: returns a fixed response, records the call."""

    def __init__(self, content):
        self._content = content
        self.calls = []

        class _Completions:
            def __init__(self, outer):
                self._outer = outer

            def create(self, **kw):
                self._outer.calls.append(kw)
                return _FakeResp(self._outer._content)

        class _Chat:
            def __init__(self, outer):
                self.completions = _Completions(outer)

        self.chat = _Chat(self)


class _FlakyClient:
    """OpenAI-compatible stub that raises ``fail_times`` then returns ``content``.

    Models a transient Together 429/503 wave that clears after a few attempts, so
    a requeue pass recovers the example.
    """

    def __init__(self, content, fail_times):
        self._content = content
        self._fail = fail_times
        self.calls = 0
        outer = self

        class _Completions:
            def create(self, **kw):
                outer.calls += 1
                if outer.calls <= outer._fail:
                    raise RuntimeError("503 service unavailable")
                return _FakeResp(outer._content)

        class _Chat:
            def __init__(self):
                self.completions = _Completions()

        self.chat = _Chat()


# ── JSON extraction ────────────────────────────────────────────────────


def test_extract_json_array_plain():
    assert _extract_json_array('[{"question": "q"}]') == [{"question": "q"}]


def test_extract_json_array_with_code_fence_and_prose():
    raw = 'Here you go:\n```json\n[{"question": "q", "lang": "ru"}]\n```\nDone.'
    assert _extract_json_array(raw) == [{"question": "q", "lang": "ru"}]


def test_extract_json_array_garbage_returns_empty():
    assert _extract_json_array("no json here") == []
    assert _extract_json_array("[broken") == []


# ── verbatim-span faithfulness gate ────────────────────────────────────


def test_span_faithful_exact_substring():
    post = "вчера ходил в баню, отличный был пар"
    assert _span_is_faithful("отличный был пар", post) is True


def test_span_faithful_tolerates_whitespace_quote_drift():
    post = "это  не дрочит,\nэто мучает"
    assert _span_is_faithful('"это не дрочит, это мучает"', post) is True


def test_span_unfaithful_paraphrase_rejected():
    post = "берлин дорогой, но свободный город"
    # A paraphrase that shares few exact tokens must be rejected.
    assert _span_is_faithful("я считаю столицу германии комфортной", post) is False


def test_span_empty_rejected():
    assert _span_is_faithful("", "anything") is False


# ── parse_items ────────────────────────────────────────────────────────


def test_parse_short_post_uses_whole_post_as_span():
    post = "борщ топ"  # short -> span overridden to the whole post
    items = parse_items(
        '[{"question": "как тебе борщ?", "answer_span": "ignored", "lang": "ru"}]',
        post,
    )
    assert items == [QGenItem(question="как тебе борщ?", answer_span="борщ топ", lang="ru")]


def test_parse_long_post_keeps_faithful_span_drops_paraphrase():
    post = "x" * 400 + " реальная фраза из поста про эмиграцию и берлин"
    raw = (
        '[{"question": "что думаешь про эмиграцию?", '
        '"answer_span": "реальная фраза из поста про эмиграцию и берлин", "lang": "ru"},'
        '{"question": "а берлин?", "answer_span": "выдуманный пересказ автора", "lang": "ru"}]'
    )
    items = parse_items(raw, post)
    assert len(items) == 1
    assert items[0].question == "что думаешь про эмиграцию?"


def test_parse_drops_overlong_question_and_dedups():
    post = "короткий пост"
    raw = (
        '[{"question": "норм вопрос?", "answer_span": "x", "lang": "ru"},'
        '{"question": "норм вопрос?", "answer_span": "x", "lang": "ru"},'
        '{"question": "' + "очень длинный вопрос " * 12 + '", "answer_span": "x", "lang": "ru"}]'
    )
    items = parse_items(raw, post)
    assert len(items) == 1  # dedup + overlong dropped


def test_parse_infers_lang_when_missing():
    raw = '[{"question": "what about berlin?", "answer_span": "s"}]'
    items = parse_items(raw, "berlin is fine")
    assert items[0].lang == "en"


def test_parse_drops_third_person_author_questions():
    post = "борщ топ, варю с уксусом"
    raw = (
        '[{"question": "что думает автор про борщ?", "answer_span": "x", "lang": "ru"},'
        '{"question": "what does the author cook?", "answer_span": "x", "lang": "en"},'
        '{"question": "как тебе борщ?", "answer_span": "x", "lang": "ru"}]'
    )
    items = parse_items(raw, post)
    assert [i.question for i in items] == ["как тебе борщ?"]


def test_parse_keeps_third_person_about_other_people():
    # «он» about a third party (Putin) must NOT be filtered — only author-noun forms are.
    post = "путин военный преступник, однозначно"
    raw = (
        '[{"question": "путин же диктатор, он развязал войну?", '
        '"answer_span": "x", "lang": "ru"}]'
    )
    items = parse_items(raw, post)
    assert len(items) == 1


# ── generate_qa with a fake client ─────────────────────────────────────


def test_generate_qa_happy_path():
    client = _FakeClient(
        '[{"question": "как жизнь в берлине?", "answer_span": "берлин ок", "lang": "ru"}]'
    )
    items = generate_qa("берлин ок", client=client, model="fake")
    assert items[0].question == "как жизнь в берлине?"
    # The model + messages were actually passed through.
    assert client.calls[0]["model"] == "fake"
    assert client.calls[0]["messages"][0]["role"] == "system"


def test_generate_qa_bad_response_returns_empty():
    assert generate_qa("post", client=_FakeClient("sorry, I can't."), model="fake") == []


def test_generate_qa_client_error_returns_empty(no_sleep):
    # A call that fails every requeue pass falls back to [] (drop), not a crash.
    boom = _FlakyClient("ignored", fail_times=99)
    assert generate_qa("post", client=boom, model="fake", retry_passes=3) == []
    assert boom.calls == 3  # all passes exhausted before giving up


def test_generate_qa_recovers_after_transient_failures(no_sleep):
    # 503 on the first two attempts, then a valid response → the requeue pass
    # recovers the example instead of silently dropping it (the silent-loss bug).
    reply = '[{"question": "как берлин?", "answer_span": "берлин ок", "lang": "ru"}]'
    client = _FlakyClient(reply, fail_times=2)
    items = generate_qa("берлин ок", client=client, model="fake", retry_passes=4)
    assert client.calls == 3  # 2 failures + 1 success
    assert len(items) == 1 and items[0].question == "как берлин?"


def test_generate_qa_no_requeue_when_passes_is_one(no_sleep):
    # retry_passes=1 == old behaviour: one attempt, drop on failure.
    boom = _FlakyClient("ignored", fail_times=99)
    assert generate_qa("post", client=boom, model="fake", retry_passes=1) == []
    assert boom.calls == 1


def test_build_messages_includes_fewshot_and_post():
    msgs = build_messages("мой пост", source="twitter", date="2020-01-01")
    assert msgs[0]["role"] == "system"
    assert "как тебе анакондаз?" in msgs[0]["content"]  # a few-shot anchor
    assert "мой пост" in msgs[1]["content"]
    assert "twitter" in msgs[1]["content"]


# ── Relevance-QC judge (F2) ────────────────────────────────────────────


def test_parse_verdict_plain_and_fenced():
    assert _parse_verdict('{"oracle_answers": true, "better_distractor": false}') == \
        GroundingVerdict(oracle_answers=True, better_distractor=False)
    fenced = '```json\n{"oracle_answers": false, "better_distractor": true}\n```'
    v = _parse_verdict(fenced)
    assert v.oracle_answers is False and v.better_distractor is True


def test_parse_verdict_garbage_returns_none():
    assert _parse_verdict("no json here") is None
    assert _parse_verdict('{"foo": 1}') is None  # missing oracle_answers


def test_judge_grounding_parses_client_response():
    client = _FakeClient('{"oracle_answers": true, "better_distractor": false}')
    v = judge_grounding("как берлин?", "берлин ок", ["другой пост"], client=client)
    assert v.judged is True and v.oracle_answers is True
    # Judge call carries the question, oracle and distractors.
    sent = client.calls[0]["messages"][-1]["content"]
    assert "как берлин?" in sent and "берлин ок" in sent and "другой пост" in sent


def test_judge_grounding_fails_open_on_unparseable():
    client = _FakeClient("the model rambled and returned no JSON")
    v = judge_grounding("q?", "oracle", [], client=client)
    assert v.judged is False and v.oracle_answers is True  # fail-open keeps the example


def test_judge_grounding_fails_open_on_exception(no_sleep):
    boom = _FlakyClient("ignored", fail_times=99)
    v = judge_grounding("q?", "oracle", [], client=boom, retry_passes=3)
    assert v.judged is False and v.oracle_answers is True  # fail-open after retries
    assert boom.calls == 3


def test_judge_grounding_recovers_after_transient_failure(no_sleep):
    client = _FlakyClient('{"oracle_answers": true, "better_distractor": false}',
                          fail_times=2)
    v = judge_grounding("q?", "oracle", [], client=client, retry_passes=4)
    assert client.calls == 3
    assert v.judged is True and v.oracle_answers is True


# ── Transfer entailment judge (F5) ─────────────────────────────────────


def test_parse_transfer_plain_and_fenced():
    from blog.sft_qgen import TransferVerdict, _parse_transfer
    assert _parse_transfer('{"supported": true}') == TransferVerdict(supported=True)
    assert _parse_transfer('```json\n{"supported": false}\n```').supported is False


def test_parse_transfer_garbage_none():
    from blog.sft_qgen import _parse_transfer
    assert _parse_transfer("nope") is None
    assert _parse_transfer('{"x": 1}') is None


def test_judge_transfer_support_parses_and_passes_payload():
    from blog.sft_qgen import judge_transfer_support
    client = _FakeClient('{"supported": true}')
    v = judge_transfer_support("q?", ["ctx a", "ctx b"], "answer key", client=client)
    assert v.judged is True and v.supported is True
    sent = client.calls[0]["messages"][-1]["content"]
    assert "answer key" in sent and "ctx a" in sent and "q?" in sent


def test_judge_transfer_fails_closed(no_sleep):
    from blog.sft_qgen import judge_transfer_support
    # Unparseable AND exhausted-retries both -> supported=False (fail-closed:
    # never emit an unconfirmed transfer as training data).
    v1 = judge_transfer_support("q", ["c"], "k", client=_FakeClient("rambling, no json"))
    assert v1.judged is False and v1.supported is False

    boom = _FlakyClient("ignored", fail_times=99)
    v2 = judge_transfer_support("q", ["c"], "k", client=boom, retry_passes=3)
    assert v2.judged is False and v2.supported is False
    assert boom.calls == 3


def test_judge_transfer_recovers_after_transient_failure(no_sleep):
    from blog.sft_qgen import judge_transfer_support
    # Fail-closed judge: a swallowed throttle was silent data loss. Requeue must
    # recover the transfer rather than drop it.
    client = _FlakyClient('{"supported": true}', fail_times=2)
    v = judge_transfer_support("q", ["c"], "k", client=client, retry_passes=4)
    assert client.calls == 3
    assert v.judged is True and v.supported is True


# ── drift router (positive-topic-drift 3-way gate) ──────────────────────


def test_judge_drift_route_synthesize_and_payload():
    from blog.sft_qgen import judge_drift_route
    client = _FakeClient('{"route":"SYNTHESIZE","reason":"stance clear"}')
    v = judge_drift_route(
        "what do you think about X?",
        [("my own take on X", True), ("someone else reshared", False)],
        "the held-out answer post",
        client=client,
    )
    assert v.judged is True and v.synthesize is True and v.route == "SYNTHESIZE"
    sent = client.calls[0]["messages"][-1]["content"]
    assert "you wrote this yourself" in sent      # own-post tag
    assert "RESHARED" in sent                      # reshare tag (judge ignores these)
    assert "held-out answer post" in sent
    assert "what do you think about X?" in sent


def test_judge_drift_route_abstain_fact():
    from blog.sft_qgen import judge_drift_route
    v = judge_drift_route(
        "how much did it cost?", [("ctx", True)], "key",
        client=_FakeClient('{"route":"ABSTAIN_FACT","reason":"needs price"}'),
    )
    assert v.route == "ABSTAIN_FACT" and v.synthesize is False and v.judged is True


def test_judge_drift_route_fails_closed(no_sleep):
    from blog.sft_qgen import judge_drift_route
    # Unparseable / unknown-route / exhausted retries all -> ABSTAIN_NOSUPPORT,
    # judged=False (fail-closed: never emit an unconfirmed synthesis).
    v1 = judge_drift_route("q", [("c", True)], "k", client=_FakeClient("no json here"))
    assert v1.judged is False and v1.route == "ABSTAIN_NOSUPPORT" and v1.synthesize is False

    v2 = judge_drift_route("q", [("c", True)], "k",
                           client=_FakeClient('{"route":"MAYBE"}'))
    assert v2.judged is False and v2.synthesize is False  # unknown route rejected

    boom = _FlakyClient("ignored", fail_times=99)
    v3 = judge_drift_route("q", [("c", True)], "k", client=boom, retry_passes=3)
    assert v3.judged is False and v3.route == "ABSTAIN_NOSUPPORT" and boom.calls == 3


def test_qgen_stance_prompt_selected():
    from blog.sft_qgen import build_messages
    std = build_messages("some post", stance=False)[0]["content"]
    stance = build_messages("some post", stance=True)[0]["content"]
    assert std != stance
    assert "TYPICAL BEHAVIOR" in stance and "AVOID narrow single-fact" in stance
    assert "TYPICAL BEHAVIOR" not in std


# ── _retry_call helper ─────────────────────────────────────────────────


def test_retry_call_returns_on_first_success(no_sleep):
    calls = {"n": 0}

    def fn():
        calls["n"] += 1
        return "ok"

    assert _retry_call(fn, passes=4) == "ok"
    assert calls["n"] == 1  # no retries when the first attempt works


def test_retry_call_raises_qgencallerror_after_exhausting(no_sleep):
    calls = {"n": 0}

    def fn():
        calls["n"] += 1
        raise RuntimeError("429 dynamic_rate_limit")

    with pytest.raises(QGenCallError):
        _retry_call(fn, passes=3)
    assert calls["n"] == 3


def test_retry_call_recovers_on_later_pass(no_sleep):
    calls = {"n": 0}

    def fn():
        calls["n"] += 1
        if calls["n"] < 3:
            raise RuntimeError("503")
        return "recovered"

    assert _retry_call(fn, passes=5) == "recovered"
    assert calls["n"] == 3


# ── funded-balance gate (assert_funded) ─────────────────────────────────


class _RaisingClient:
    """OpenAI-compatible stub whose create() raises a given exception."""

    def __init__(self, exc):
        class _Completions:
            def create(self, **kw):
                raise exc

        class _Chat:
            def __init__(self):
                self.completions = _Completions()

        self.chat = _Chat()


def _status_error(status, message):
    e = RuntimeError(message)
    e.status_code = status
    return e


def test_assert_funded_passes_on_real_content():
    from blog.sft_qgen import assert_funded

    # A normal 200 with content → no raise.
    assert_funded(_FakeClient("чо как, как сам?"), model="fake") is None


def test_assert_funded_raises_on_402_unfunded():
    from blog.sft_qgen import TogetherBalanceError, assert_funded

    client = _RaisingClient(_status_error(402, "spend limit reached for this billing cycle"))
    try:
        assert_funded(client, model="fake")
        assert False, "expected TogetherBalanceError on 402"
    except TogetherBalanceError as e:
        assert "UNFUNDED" in str(e) and "billing" in str(e)


def test_assert_funded_raises_on_402_by_message_without_status():
    # No status_code attr, but the message names a spend limit → still caught.
    from blog.sft_qgen import TogetherBalanceError, assert_funded

    client = _RaisingClient(RuntimeError("Error: insufficient balance"))
    try:
        assert_funded(client, model="fake")
        assert False, "expected TogetherBalanceError"
    except TogetherBalanceError:
        pass


def test_assert_funded_raises_on_401_bad_key():
    from blog.sft_qgen import TogetherBalanceError, assert_funded

    client = _RaisingClient(_status_error(401, "invalid api key"))
    try:
        assert_funded(client, model="fake")
        assert False, "expected TogetherBalanceError on 401"
    except TogetherBalanceError as e:
        assert "REJECTED" in str(e)


def test_assert_funded_reraises_transient_5xx():
    # A transient server error must NOT be swallowed as a balance error — the
    # caller's retry path handles it; we only HARD-stop on 402/401.
    from blog.sft_qgen import TogetherBalanceError, assert_funded

    client = _RaisingClient(_status_error(503, "service temporarily unavailable"))
    try:
        assert_funded(client, model="fake")
        assert False, "expected the original error to propagate"
    except TogetherBalanceError:
        assert False, "transient 5xx must not be classified as a balance error"
    except RuntimeError as e:
        assert "unavailable" in str(e)


def test_assert_funded_raises_on_empty_content():
    from blog.sft_qgen import TogetherBalanceError, assert_funded

    try:
        assert_funded(_FakeClient(""), model="fake")
        assert False, "expected TogetherBalanceError on empty content"
    except TogetherBalanceError as e:
        assert "empty content" in str(e)
