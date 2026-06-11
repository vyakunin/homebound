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

from blog.models import Post, PostSource, PostVisibility
from blog.management.commands.build_sft_dataset import (
    _detect_lang,
    _is_degenerate,
    _is_dirty,
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
