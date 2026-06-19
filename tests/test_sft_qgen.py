"""Tests for the grounded-QA question generator (blog.sft_qgen).

No network: a fake OpenAI-compatible client returns canned content, so we test
the prompt assembly, JSON extraction, and the verbatim-span faithfulness gate.
"""
import tests.django_setup  # noqa: F401 — must run before any Django imports
from blog.sft_qgen import (
    QGenItem,
    _extract_json_array,
    _span_is_faithful,
    build_messages,
    generate_qa,
    parse_items,
)


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


def test_generate_qa_client_error_returns_empty():
    class _BoomClient:
        class chat:  # noqa: N801
            class completions:  # noqa: N801
                @staticmethod
                def create(**kw):
                    raise RuntimeError("network down")

    assert generate_qa("post", client=_BoomClient(), model="fake") == []


def test_build_messages_includes_fewshot_and_post():
    msgs = build_messages("мой пост", source="twitter", date="2020-01-01")
    assert msgs[0]["role"] == "system"
    assert "как тебе анакондаз?" in msgs[0]["content"]  # a few-shot anchor
    assert "мой пост" in msgs[1]["content"]
    assert "twitter" in msgs[1]["content"]
