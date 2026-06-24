"""PROTOTYPE / one-off: test more-permissive topic-drift routing before the big regen.

Goal (per Vladimir, 2026-06-24): flip the abstain-under-close-neighbors examples
(transfer_abstain + raft_no_oracle) toward POSITIVE topic drift — synthesize an
answer in his voice from related neighbors when the answer post itself wasn't
retrieved — while KEEPING the target = his verbatim post (never LLM-written) and
keeping genuine fact-lookups as abstain.

This harness does NOT mutate the dataset. It:
  • Test 1 (judge): re-routes a sample of EXISTING v6 transfer_abstain + raft
    records through a NEW permissive 3-way judge (SYNTHESIZE / ABSTAIN_FACT /
    ABSTAIN_NOSUPPORT) and compares against the OLD strict entailment judge.
  • Test 2 (qgen): on a few oracle posts, compares OLD qgen vs a stance-biased
    qgen prompt (question construction permissiveness).

Prototype prompts live HERE; promote the approved wording into blog/sft_qgen.py
only after sign-off. Read-only against the training DB (:5434).

Run:
  cd ~/cursor_projects/homebound && DJANGO_SETTINGS_MODULE=django_config.settings \
    PYTHONPATH="bazel-bin:." DB_HOST=localhost DB_PORT=5434 DB_USER=postgres \
    DB_PASSWORD=sftbuild DB_NAME=homebound .venv/bin/python \
    scripts/oneoff/test_drift_route.py --n 40 --qgen-n 8
"""
from __future__ import annotations

import argparse
import json
import random
import re
import sys
from pathlib import Path

import django

V6 = Path("~/cursor_projects/homebound/output/sft_v6_full.jsonl").expanduser()

# ───────────────────────── PROTOTYPE PROMPTS (tune here) ─────────────────────

PROTOTYPE_DRIFT_JUDGE_SYSTEM = """\
You route how a "talk to the author" chatbot should handle ONE visitor question.

The author is a private person; his bot answers strangers using his past posts.
For this question the retriever surfaced some CONTEXT posts but MISSED his own best
post on it — that post (the ANSWER KEY) is shown to you but is NOT in the bot's
context. Decide what the bot should be trained to do here.

Choose ONE route:

SYNTHESIZE — answer in the author's voice from his related posts. Pick this when:
  • the QUESTION asks for his opinion, take, taste, attitude, values, a recurring
    theme, his general approach, OR his TYPICAL BEHAVIOR / HABITS / what he DOES in
    a kind of situation — i.e. a "what do you think / would you / how do you feel /
    what's your take / what do you do when / how do you handle / how do you usually"
    question, NOT a single lookup-able fact; AND
  • the CONTEXT has enough of the author's OWN posts (lines tagged
    "you wrote this yourself" — IGNORE reshared / third-party posts) on the same
    theme that answering in his voice is faithful to him.
  The answer need NOT appear in the CONTEXT. A stance or typical-behavior CONSISTENT
  with his related posts is exactly what we want the bot to synthesize. Be GENEROUS:
  most opinion / taste / stance / habit / "what do you do" questions are SYNTHESIZE
  when his own posts show his attitude or conduct in that domain — even if the
  ANSWER KEY's exact wording or the precise step-by-step isn't reachable from the
  context. A general behavior question is NOT a fact-lookup just because the exact
  actions aren't spelled out in the context.

ABSTAIN_FACT — pick this ONLY when answering correctly REQUIRES a specific
  QUANTITATIVE or IDENTITY fact, or a SINGLE ONE-OFF EVENT'S specific outcome, that
  lives only in the ANSWER KEY and cannot be inferred from his general stance/
  behavior: a number, price, count, date, duration, address, proper name, a specific
  URL/title, or what specifically happened in one particular past episode.
  Producing the answer would be GUESSING that specific. (e.g. "how much does X cost",
  "how many people came", "when exactly did you travel", "what's the referral
  reward", "what does THIS specific in-joke/postcard mean".)
  Do NOT use ABSTAIN_FACT for a general "what do you do / how do you handle / how do
  you usually" behavior question — that is SYNTHESIZE when his posts show his conduct.

ABSTAIN_NOSUPPORT — pick this when the CONTEXT is off-topic, or has no first-person
  posts on the theme (only reshares / unrelated material), so there's no real basis
  to answer in his voice.

Output ONLY JSON, nothing else:
{"route":"SYNTHESIZE"|"ABSTAIN_FACT"|"ABSTAIN_NOSUPPORT","reason":"<=12 words"}
"""

# Stance-biased qgen — same contract as sft_qgen._QGEN_SYSTEM but steers toward
# opinion/theme questions and away from narrow single-fact lookups.
PROTOTYPE_QGEN_STANCE_SYSTEM = """\
You generate realistic VISITOR QUESTIONS for a "talk to the author" chatbot.

The author is a private person — terse, ironic, bilingual Russian/English. His
chatbot answers strangers using his past social-media posts. Given ONE of his past
posts, output 1-3 questions a real visitor might type, plus the exact verbatim
excerpt of the post that answers each.

PREFER questions about his OPINION, TAKE, TASTE, VALUES, ATTITUDE, or a RECURRING
THEME — the kind a stranger asks to get HIS VIEW ("что думаешь про…", "как
относишься к…", "что бы ты сделал…", "what's your take on…", "do you like…").
These generalize: his stance shows up across many posts, so the bot can answer them
even when this exact post isn't retrieved.

AVOID narrow single-fact lookups whose answer is one number/price/date/count/
address/proper-name/one-off-event that lives ONLY in this post (e.g. "how much did X
cost", "how many people came", "when exactly"). Those don't generalize and force the
bot to guess. If the post only supports such a fact-lookup question, return [].

QUESTIONS must:
- sound like these REAL examples (casual, short, lowercase ok, slang/profanity ok),
  NOT polished QA-benchmark prose:
{fewshot}
- be asked by a stranger: do NOT quote the post or say "your post"/"you wrote". Ask
  about the topic/opinion as if simply curious.
- address the author DIRECTLY (second person / impersonal), NEVER third person
  (no «автор», «он/она», "the author", "this guy").
- be in the SAME language as the post. Never mix languages in one question.
- be varied; no near-duplicates.

ANSWER_SPAN must:
- be a VERBATIM substring copied from the post (his own words) that best expresses
  his view on the question — the minimal relevant part (whole post if it's all
  relevant). NEVER paraphrased/translated/reworded.

Output ONLY a JSON array, nothing else:
[{{"question":"...","answer_span":"...","lang":"ru"|"en"}}]
If no good stance question exists, output [].
"""

# ───────────────────────── parsing helpers ──────────────────────────────────

_POST_HDR = re.compile(r"^## Post \d+ — (/post/[^/]+/?) \(([^)]*)\)\s*$", re.M)


def parse_user_turn(user: str):
    """Return (question, [ {slug, source_is_own, text} ]) parsed from a built
    RAG user turn."""
    qm = re.search(r"# Visitor question\s*\n+(.*)", user, re.S)
    question = qm.group(1).strip() if qm else ""
    posts = []
    hdrs = list(_POST_HDR.finditer(user))
    for i, h in enumerate(hdrs):
        slug = h.group(1).strip("/").split("/")[-1]
        body_start = h.end()
        body_end = hdrs[i + 1].start() if i + 1 < len(hdrs) else (
            user.find("\n# Visitor question", body_start)
        )
        if body_end == -1:
            body_end = len(user)
        chunk = user[body_start:body_end]
        own = "SOURCE: you wrote this yourself." in chunk
        # text = everything after the SOURCE line / "---" separator
        m = re.search(r"---\n(.*)", chunk, re.S)
        text = (m.group(1) if m else chunk).strip()
        posts.append({"slug": slug, "source_is_own": own, "text": text})
    return question, posts


def fmt_context(posts) -> str:
    out = []
    for i, p in enumerate(posts, 1):
        tag = "you wrote this yourself" if p["source_is_own"] else "RESHARED / third-party (ignore for 'his own material')"
        out.append(f"[CONTEXT {i}] ({tag})\n{p['text'].strip()}")
    return "\n\n".join(out) or "(none)"


# ───────────────────────── judge callers ────────────────────────────────────


def new_drift_judge(client, model, question, posts, answer_key):
    from blog.sft_qgen import _extract_json_array  # reuse tolerant parser
    user = (
        f"QUESTION:\n{question}\n\n"
        f"CONTEXT posts (retriever surfaced these; the answer post is NOT here):\n"
        f"{fmt_context(posts)}\n\n"
        f"ANSWER KEY (the author's real post on this — held out, NOT in context):\n"
        f"{answer_key.strip()}\n\nRoute now."
    )
    resp = client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": PROTOTYPE_DRIFT_JUDGE_SYSTEM},
            {"role": "user", "content": user},
        ],
        temperature=0.0, max_tokens=80,
    )
    raw = (resp.choices[0].message.content or "").strip()
    t = raw
    s, e = t.find("{"), t.rfind("}")
    try:
        obj = json.loads(t[s:e + 1]) if s != -1 and e > s else {}
    except Exception:
        obj = {}
    return obj.get("route", "?"), obj.get("reason", ""), raw


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=40, help="judge-test sample size")
    ap.add_argument("--qgen-n", type=int, default=8, help="qgen-test sample size")
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--out", default="/tmp/drift_route_test.txt")
    args = ap.parse_args()

    django.setup()
    from blog import sft_qgen
    from blog.models import Post

    client = sft_qgen.make_together_client()
    model = sft_qgen.DEFAULT_QGEN_MODEL

    # ── load existing abstain records ──
    ta, raft = [], []
    with open(V6) as f:
        for line in f:
            d = json.loads(line); m = d.get("meta", {})
            if m.get("bucket") == "transfer_abstain":
                ta.append(d)
            elif m.get("objective") == "abstention" and m.get("subtype") == "raft_no_oracle":
                raft.append(d)
    rng = random.Random(args.seed)
    rng.shuffle(ta); rng.shuffle(raft)
    # split sample across both buckets
    half = args.n // 2
    sample = [("transfer_abstain", d) for d in ta[:half]] + \
             [("raft_no_oracle", d) for d in raft[:args.n - half]]

    def post_text(slug):
        p = Post.objects.filter(slug=slug).values_list("content_text", flat=True).first()
        return (p or "").strip()

    lines = []
    counts = {"SYNTHESIZE": 0, "ABSTAIN_FACT": 0, "ABSTAIN_NOSUPPORT": 0, "?": 0}
    print(f"=== TEST 1: re-route {len(sample)} existing abstain records (NEW permissive judge) ===\n")
    for idx, (bkt, d) in enumerate(sample, 1):
        m = d["meta"]
        u = next(x["content"] for x in d["messages"] if x["role"] == "user")
        old_target = next(x["content"] for x in d["messages"] if x["role"] == "assistant")
        oslug = m.get("oracle_slug") or m.get("excluded_oracle_slug") or ""
        q, posts = parse_user_turn(u)
        ans = post_text(oslug)
        if not ans:
            continue
        own_n = sum(1 for p in posts if p["source_is_own"])
        try:
            route, reason, raw = new_drift_judge(client, model, q, posts, ans)
        except Exception as ex:
            route, reason, raw = "?", f"ERR {ex}", ""
        counts[route if route in counts else "?"] += 1
        block = (
            f"\n{'='*92}\n#{idx} [{bkt}] oracle={oslug} | own-posts={own_n}/{len(posts)} "
            f"| OLD route=abstain('{old_target}')\n"
            f"Q: {q}\n"
            f"NEW route: {route}   ({reason})\n"
            f"ANSWER KEY (would become verbatim target if SYNTHESIZE):\n  {ans[:300]}\n"
            f"CONTEXT (his own posts only):\n"
        )
        for p in posts:
            if p["source_is_own"]:
                block += f"  • {p['text'][:160]}\n"
        lines.append(block)
        print(block)

    print(f"\n=== NEW-judge route distribution over {sum(counts.values())} re-judged abstains ===")
    for r, c in counts.items():
        tot = sum(counts.values()) or 1
        print(f"  {c:4d} ({100*c/tot:4.0f}%)  {r}")
    flip = counts["SYNTHESIZE"]
    print(f"\nFLIP rate (abstain → SYNTHESIZE, target=verbatim P): {flip}/{sum(counts.values())} "
          f"({100*flip/(sum(counts.values()) or 1):.0f}%)")

    # ── TEST 2: qgen permissiveness ──
    qgen_block = ["\n\n" + "#"*92 + f"\n=== TEST 2: OLD vs STANCE-biased qgen on {args.qgen_n} oracle posts ===\n"]
    print(qgen_block[0])
    pool = list(
        Post.objects.exclude(content_text="").filter(visibility=1)
        .values_list("slug", "content_text", "created_at")[:5000]
    )
    rng.shuffle(pool)
    picked = 0
    for slug, text, _ in pool:
        if picked >= args.qgen_n:
            break
        text = (text or "").strip()
        if len(text) < 40 or len(text) > 600:
            continue
        old_items = sft_qgen.generate_qa(text, client=client, model=model)
        # stance-biased: temporarily swap system prompt via monkeypatch of build_messages
        new_items = _stance_qgen(sft_qgen, text, client, model)
        picked += 1
        b = (f"\n{'-'*92}\nORACLE {slug}:\n  {text[:240]}\n"
             f"OLD qgen Qs: {[i.question for i in old_items]}\n"
             f"STANCE qgen Qs: {[i.question for i in new_items]}\n")
        qgen_block.append(b); print(b)

    Path(args.out).write_text("".join(lines) + "".join(qgen_block), encoding="utf-8")
    print(f"\n[full dump → {args.out}]")


def _stance_qgen(sft_qgen, text, client, model):
    """Run qgen with the stance-biased system prompt (reuses parse_items)."""
    fewshot = sft_qgen.DEFAULT_FEWSHOT
    bullets = "\n".join(f"    • {q}" for q in fewshot)
    system = PROTOTYPE_QGEN_STANCE_SYSTEM.format(fewshot=bullets)
    user = f'Past post by the author:\n"""\n{text.strip()}\n"""\n\nGenerate the questions now.'
    resp = client.chat.completions.create(
        model=model,
        messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
        temperature=0.7, max_tokens=800,
    )
    raw = resp.choices[0].message.content or ""
    return sft_qgen.parse_items(raw, text)


if __name__ == "__main__":
    sys.exit(main())
