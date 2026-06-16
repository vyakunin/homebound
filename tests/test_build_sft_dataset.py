"""Tests for the build_sft_dataset management command.

Covers the pure transform helpers (persona/reply example shape, lang detect,
source resolution) and an integration run via call_command writing JSONL.
"""
import json
from datetime import datetime, timezone

import tests.django_setup  # noqa: F401 — must run before any Django imports

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError

from blog.models import Post, PostComment, PostSource, PostVisibility
from blog.management.commands.build_sft_dataset import (
    _detect_lang,
    _is_degenerate,
    _is_dirty,
    _is_self_author,
    _persona_example,
    _reply_example,
    _reply_example_from_reply_to,
    _resolve_sources,
)


def _make_post(**kw) -> Post:
    defaults = dict(
        content_text="Hello world, a test post.",
        created_at=datetime(2018, 3, 4, 9, 0, tzinfo=timezone.utc),
        source=PostSource.TWITTER,
        source_id="t-1",
        visibility=PostVisibility.PUBLIC,
    )
    defaults.update(kw)
    return Post.objects.create(**defaults)


def _add_comments(post: Post, thread: list[tuple[str, str]]) -> None:
    """Append a flat comment thread as ``[(author_name, text), ...]`` in order.

    created_at is monotonic from the post time so the builder's time-ordering is
    deterministic regardless of insert order.
    """
    base = post.created_at
    for i, (author, text) in enumerate(thread):
        PostComment.objects.create(
            post=post,
            author_name=author,
            text=text,
            created_at=base.replace(minute=(base.minute + i + 1) % 60),
            source_id=f"{post.source_id}-c{i}",
        )


def test_detect_lang_cyrillic_vs_latin():
    assert _detect_lang("Привет, как дела сегодня") == "ru"
    assert _detect_lang("Hello there, how are you") == "en"
    assert _detect_lang("12345 !!! ###") == "und"


def test_resolve_sources_all_and_subset():
    assert PostSource.TWITTER in _resolve_sources("all")
    assert _resolve_sources("twitter,facebook") == [PostSource.TWITTER, PostSource.FACEBOOK]


def test_resolve_sources_rejects_unknown():
    with pytest.raises(CommandError):
        _resolve_sources("myspace")


@pytest.mark.django_db
def test_persona_example_shape():
    post = _make_post(content_text="Каждый день — это маленькая жизнь.")
    ex = _persona_example(post)
    roles = [m["role"] for m in ex.messages]
    assert roles == ["system", "user", "assistant"]
    assert ex.messages[-1]["content"] == "Каждый день — это маленькая жизнь."
    assert ex.meta["objective"] == "persona"
    assert ex.meta["source"] == "twitter"
    assert ex.meta["lang"] == "ru"


@pytest.mark.django_db
def test_reply_example_pairs_parent_to_response():
    post = _make_post(
        content_text="Totally agree with this.",
        reshared_from_author="@someone",
        reshared_from_url="https://x.com/someone/status/9",
        reshared_content_text="The original take being responded to.",
    )
    ex = _reply_example(post)
    assert ex.messages[0]["role"] == "system"
    # user turn carries the PARENT (what is being replied to), prefixed by author
    assert "The original take being responded to." in ex.messages[1]["content"]
    assert ex.messages[1]["content"].startswith("@someone:")
    # assistant turn is HIS response
    assert ex.messages[-1]["content"] == "Totally agree with this."
    assert ex.meta["objective"] == "reply"
    assert ex.meta["parent_url"] == "https://x.com/someone/status/9"


@pytest.mark.django_db
def test_command_emits_both_objectives_jsonl(tmp_path):
    # one plain post (persona only) + one quote/reshare (persona + reply)
    _make_post(source_id="plain-1", content_text="Just a standalone thought.")
    _make_post(
        source_id="quote-1",
        content_text="My commentary here.",
        reshared_content_text="Parent post body.",
        reshared_from_author="@orig",
    )
    out = tmp_path / "ds.jsonl"
    call_command("build_sft_dataset", "--out", str(out), "--sources", "twitter")

    lines = [json.loads(ln) for ln in out.read_text(encoding="utf-8").splitlines()]
    objectives = sorted(rec["meta"]["objective"] for rec in lines)
    # 2 persona (both posts) + 1 reply (the quote) = 3
    assert objectives == ["persona", "persona", "reply"]
    for rec in lines:
        assert [m["role"] for m in rec["messages"]] == ["system", "user", "assistant"]
        assert rec["messages"][-1]["content"]  # non-empty assistant turn


@pytest.mark.django_db
def test_command_min_len_filters_short_posts(tmp_path):
    _make_post(source_id="short", content_text="hi")
    _make_post(source_id="long", content_text="This is a sufficiently long post body.")
    out = tmp_path / "ds.jsonl"
    call_command(
        "build_sft_dataset", "--objective", "persona",
        "--sources", "twitter", "--min-len", "10", "--out", str(out),
    )
    lines = out.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    assert json.loads(lines[0])["meta"]["source_id"] == "long"


@pytest.mark.django_db
def test_command_dedup_is_per_objective_not_cross(tmp_path):
    # A quote post yields BOTH a persona example and a reply example whose
    # assistant turn is the same content_text. Cross-objective dedup would wrongly
    # drop the reply (regression: persona pass poisons the reply pass).
    _make_post(
        source_id="quote-1",
        content_text="Same commentary text.",
        reshared_content_text="Parent body.",
        reshared_from_author="@orig",
    )
    out = tmp_path / "ds.jsonl"
    call_command("build_sft_dataset", "--out", str(out), "--sources", "twitter")
    objectives = sorted(
        json.loads(ln)["meta"]["objective"]
        for ln in out.read_text(encoding="utf-8").splitlines()
    )
    assert objectives == ["persona", "reply"]


@pytest.mark.django_db
def test_reply_example_from_reply_to_pairs_parent_to_response():
    post = _make_post(
        content_text="Не согласен, вот почему.",
        reply_to_author="@interlocutor",
        reply_to_url="https://x.com/interlocutor/status/42",
        reply_to_text="The take he is replying to.",
        reply_to_source_id="42",
    )
    ex = _reply_example_from_reply_to(post)
    assert ex.messages[0]["role"] == "system"
    # user turn carries the PARENT (what he is replying to), prefixed by author
    assert ex.messages[1]["content"].startswith("@interlocutor:")
    assert "The take he is replying to." in ex.messages[1]["content"]
    # assistant turn is HIS reply
    assert ex.messages[-1]["content"] == "Не согласен, вот почему."
    assert ex.meta["objective"] == "reply"
    assert ex.meta["parent_url"] == "https://x.com/interlocutor/status/42"
    assert ex.meta["lang"] == "ru"


@pytest.mark.django_db
def test_command_yields_reply_pair_from_reply_to_text(tmp_path):
    # A true conversational reply (reply_to_text populated, no reshare) must
    # produce a reply example — the schema-foundation payoff.
    _make_post(
        source_id="reply-1",
        content_text="My conversational reply.",
        reply_to_text="Parent post being replied to.",
        reply_to_author="@parent",
    )
    out = tmp_path / "ds.jsonl"
    call_command("build_sft_dataset", "--objective", "reply",
                 "--sources", "twitter", "--out", str(out))
    lines = [json.loads(ln) for ln in out.read_text(encoding="utf-8").splitlines()]
    assert len(lines) == 1
    rec = lines[0]
    assert rec["meta"]["objective"] == "reply"
    assert rec["messages"][1]["content"].startswith("@parent:")
    assert "Parent post being replied to." in rec["messages"][1]["content"]
    assert rec["messages"][-1]["content"] == "My conversational reply."


@pytest.mark.django_db
def test_command_reply_skips_placeholder_reply_to_text(tmp_path):
    # The "(original post not available)" placeholder is no signal — drop it,
    # same as for reshared_content_text.
    _make_post(
        source_id="reply-ph-1",
        content_text="My reply.",
        reply_to_text="(original post not available)",
        reply_to_author="@parent",
    )
    out = tmp_path / "ds.jsonl"
    call_command("build_sft_dataset", "--objective", "reply",
                 "--sources", "twitter", "--out", str(out))
    assert out.read_text(encoding="utf-8").strip() == ""


@pytest.mark.django_db
def test_command_reply_yields_both_reshare_and_reply_to(tmp_path):
    # A post carrying BOTH reshared and reply_to parent context yields two reply
    # examples (one per parent-context source). They share the same assistant
    # turn (one post = one content_text), so --no-dedup is needed to observe both.
    _make_post(
        source_id="both-1",
        content_text="Distinct take A.",
        reshared_content_text="Reshared parent body.",
        reshared_from_author="@orig",
        reply_to_text="Reply parent body.",
        reply_to_author="@parent",
    )
    out = tmp_path / "ds.jsonl"
    call_command("build_sft_dataset", "--objective", "reply", "--no-dedup",
                 "--sources", "twitter", "--out", str(out))
    lines = [json.loads(ln) for ln in out.read_text(encoding="utf-8").splitlines()]
    parents = sorted(rec["messages"][1]["content"] for rec in lines)
    # Two reply examples from the one post, one per parent-context source.
    assert len(lines) == 2
    assert any(p.startswith("@orig:") for p in parents)
    assert any(p.startswith("@parent:") for p in parents)


@pytest.mark.django_db
def test_reply_skips_placeholder_parent(tmp_path):
    # FB stores "(original post not available)" when it can't fetch the reshared
    # body — useless as a reply parent, must not become a reply example.
    _make_post(
        source_id="ph-1",
        content_text="My take.",
        reshared_content_text="(original post not available)",
    )
    out = tmp_path / "ds.jsonl"
    call_command("build_sft_dataset", "--objective", "reply",
                 "--sources", "twitter", "--out", str(out))
    assert out.read_text(encoding="utf-8").strip() == ""


@pytest.mark.django_db
def test_command_dedup_drops_repeated_assistant_text(tmp_path):
    _make_post(source_id="a", content_text="#recurring hashtag noise")
    _make_post(source_id="b", content_text="#recurring hashtag noise")
    out = tmp_path / "ds.jsonl"
    call_command(
        "build_sft_dataset", "--objective", "persona",
        "--sources", "twitter", "--out", str(out),
    )
    assert len(out.read_text(encoding="utf-8").splitlines()) == 1


# --- content-cleanliness gate (regression: 2026-06-04 pre-train-run audit) ---


def test_is_dirty_flags_welded_fb_activity_log_chrome():
    # The three real contamination shapes the audit surfaced in sft_v1.jsonl.
    assert _is_dirty("поехалиPublic7:47 PMView shared a link.Современные номера")
    assert _is_dirty("Жаль, не СаратовPublicHidden from profile3:57 AM")
    assert _is_dirty("Your posts, photos and videosAllArchiveTrashChange AudienceDec 31")


def test_is_dirty_keeps_legit_prose_with_incidental_tokens():
    # False positives the narrow signatures must NOT drop: real political posts
    # that merely contain "shared a link." / a standalone timestamp / quoted
    # dialog with clock times.
    assert not _is_dirty("Собянин еще более трусливый, чем Медведев. shared a link.")
    assert not _is_dirty("перепост: Mar. 13th, 2013 at 9:44 PM Оригинал взят у ivand")
    assert not _is_dirty("me: hlikponser 11:13 PM puffypearls: im soooo bored")


def test_is_degenerate_flags_no_signal_turns():
    assert _is_degenerate("И")
    assert _is_degenerate(":)")
    assert _is_degenerate("\\")
    assert _is_degenerate("  ")
    assert not _is_degenerate("да нет наверное")
    assert not _is_degenerate("Каждый день — это маленькая жизнь.")


@pytest.mark.django_db
def test_command_drops_chrome_contaminated_persona(tmp_path):
    _make_post(source_id="clean", content_text="Совершенно нормальный пост про жизнь.")
    _make_post(
        source_id="chrome",
        content_text="поехалиPublic7:47 PMView shared a link.Отель Antelope Inn",
    )
    _make_post(source_id="degen", content_text="И")
    out = tmp_path / "ds.jsonl"
    call_command(
        "build_sft_dataset", "--objective", "persona",
        "--sources", "twitter", "--out", str(out),
    )
    lines = [json.loads(ln) for ln in out.read_text(encoding="utf-8").splitlines()]
    ids = [rec["meta"]["source_id"] for rec in lines]
    assert ids == ["clean"]  # chrome + degenerate both dropped


@pytest.mark.django_db
def test_command_drops_chrome_contaminated_reply_response(tmp_path):
    # A reply whose own (assistant) turn is chrome-polluted is unusable even with
    # a good parent.
    _make_post(
        source_id="dirty-reply",
        content_text="ОтветPublic2:15 PMView shared a post.",
        reply_to_text="A perfectly fine parent post.",
        reply_to_author="@parent",
    )
    out = tmp_path / "ds.jsonl"
    call_command(
        "build_sft_dataset", "--objective", "reply",
        "--sources", "twitter", "--out", str(out),
    )
    assert out.read_text(encoding="utf-8").strip() == ""


# --- Google+ third reply source: comment-thread mining ---


def test_is_self_author_matches_vladimir_only():
    assert _is_self_author("Vladimir Yakunin")
    assert _is_self_author("  vladimir yakunin ")  # case/space-insensitive
    assert _is_self_author("Владимир Якунин")
    assert not _is_self_author("Роман Якунин")     # a different Якунин
    assert not _is_self_author("Sergey Samoylenko")
    assert not _is_self_author("")


@pytest.mark.django_db
def test_comment_thread_pairs_his_reply_with_preceding_nonself(tmp_path):
    # On his own G+ post: someone comments, he replies in-thread → one pair
    # (their comment → his reply). The post body itself is a persona example.
    post = _make_post(
        source=PostSource.GOOGLE_PLUS, source_id="gp-1",
        content_text="A reflection on the day.",
    )
    _add_comments(post, [
        ("Sergey Samoylenko", "Interesting point, but what about X?"),
        ("Vladimir Yakunin", "Good question — X is handled by Y, here's why."),
    ])
    out = tmp_path / "ds.jsonl"
    call_command("build_sft_dataset", "--objective", "reply",
                 "--sources", "google_plus", "--out", str(out))
    lines = [json.loads(ln) for ln in out.read_text(encoding="utf-8").splitlines()]
    assert len(lines) == 1
    rec = lines[0]
    assert rec["meta"]["objective"] == "reply"
    assert rec["meta"]["parent_kind"] == "comment_thread"
    assert rec["messages"][1]["content"].startswith("Sergey Samoylenko:")
    assert "what about X?" in rec["messages"][1]["content"]
    assert rec["messages"][-1]["content"].startswith("Good question")


@pytest.mark.django_db
def test_comment_thread_skips_consecutive_self_continuations(tmp_path):
    # A->self->self: only the FIRST self-comment pairs (against A); the second is
    # a continuation of his own thought, not a reply → no second pair.
    post = _make_post(
        source=PostSource.GOOGLE_PLUS, source_id="gp-2", content_text="Post body here.",
    )
    _add_comments(post, [
        ("Egor Pasko", "Have you considered the counterargument here?"),
        ("Vladimir Yakunin", "Yes, and the first half of my answer is this."),
        ("Vladimir Yakunin", "And here is the second half continuing the thought."),
    ])
    out = tmp_path / "ds.jsonl"
    call_command("build_sft_dataset", "--objective", "reply",
                 "--sources", "google_plus", "--out", str(out))
    lines = [json.loads(ln) for ln in out.read_text(encoding="utf-8").splitlines()]
    assert len(lines) == 1
    assert lines[0]["messages"][-1]["content"].startswith("Yes, and the first half")


@pytest.mark.django_db
def test_comment_thread_skips_leading_self_comment(tmp_path):
    # Thread opening with his own comment has no external parent → no pair.
    post = _make_post(
        source=PostSource.GOOGLE_PLUS, source_id="gp-3", content_text="Another post.",
    )
    _add_comments(post, [
        ("Vladimir Yakunin", "Adding a note to my own post before anyone replies."),
        ("Olga Ольга", "Nice addition, thanks for sharing this one."),
    ])
    out = tmp_path / "ds.jsonl"
    call_command("build_sft_dataset", "--objective", "reply",
                 "--sources", "google_plus", "--out", str(out))
    assert out.read_text(encoding="utf-8").strip() == ""


@pytest.mark.django_db
def test_comment_thread_multiple_exchanges_yield_each_pair(tmp_path):
    # A->self->B->self yields two pairs, each parented on the preceding non-self.
    post = _make_post(
        source=PostSource.GOOGLE_PLUS, source_id="gp-4", content_text="Discussion post.",
    )
    _add_comments(post, [
        ("Ivan Korotkov", "First interlocutor raises the opening question."),
        ("Vladimir Yakunin", "My answer to the first interlocutor goes here."),
        ("Sergey Alyaev", "Second interlocutor pushes back on a different angle."),
        ("Vladimir Yakunin", "My distinct answer to the second interlocutor here."),
    ])
    out = tmp_path / "ds.jsonl"
    call_command("build_sft_dataset", "--objective", "reply",
                 "--sources", "google_plus", "--out", str(out))
    lines = [json.loads(ln) for ln in out.read_text(encoding="utf-8").splitlines()]
    assert len(lines) == 2
    parents = sorted(rec["messages"][1]["content"] for rec in lines)
    assert parents[0].startswith("Ivan Korotkov:")
    assert parents[1].startswith("Sergey Alyaev:")


@pytest.mark.django_db
def test_comment_thread_drops_short_reply_under_min_len(tmp_path):
    # His in-thread reply below min-len is dropped (parent is fine).
    post = _make_post(
        source=PostSource.GOOGLE_PLUS, source_id="gp-5", content_text="Body text here.",
    )
    _add_comments(post, [
        ("Max Ushakov", "A perfectly reasonable parent comment to reply to."),
        ("Vladimir Yakunin", "ok"),
    ])
    out = tmp_path / "ds.jsonl"
    call_command("build_sft_dataset", "--objective", "reply", "--min-len", "10",
                 "--sources", "google_plus", "--out", str(out))
    assert out.read_text(encoding="utf-8").strip() == ""
