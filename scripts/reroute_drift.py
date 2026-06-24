"""Re-route the close-neighbor abstain buckets to POSITIVE topic drift.

Takes a built SFT jsonl (v6) and, for every ``transfer_abstain`` and
``raft_no_oracle`` example, re-decides via the permissive 3-way drift router
(``blog.sft_qgen.judge_drift_route``) whether the bot should SYNTHESIZE an answer
in the author's voice or keep abstaining:

  • SYNTHESIZE → rewrite the assistant target to the held-out oracle's VERBATIM
    post (his real words — never LLM-written), relabel objective/bucket = transfer.
  • ABSTAIN_FACT / ABSTAIN_NOSUPPORT / judge-failed / answer would leak into the
    block → keep the original abstain example unchanged.

Every OTHER bucket (persona / reply / grounded_qa / source / contrastive /
off_corpus abstention) is copied through untouched — this is the "splice just the
two drift buckets" rebuild. Targets stay 100% verbatim; the leak guard keeps the
verify HARD gate (target ⊄ user turn) green.

Read-only against the training DB (:5434) for the held-out post text.

Run:
  cd ~/cursor_projects/homebound && DJANGO_SETTINGS_MODULE=django_config.settings \
    PYTHONPATH="bazel-bin:." DB_HOST=localhost DB_PORT=5434 DB_USER=postgres \
    DB_PASSWORD=sftbuild DB_NAME=homebound .venv/bin/python scripts/reroute_drift.py \
    --in output/sft_v6_full.jsonl --out output/sft_v7_full.jsonl
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from concurrent.futures import ThreadPoolExecutor

import django

_POST_HDR = re.compile(r"^## Post \d+ — (/post/[^/]+/?) \(([^)]*)\)\s*$", re.M)


def parse_user_turn(user: str):
    """(question, [(post_text, is_own)]) parsed from a built RAG user turn."""
    qm = re.search(r"# Visitor question\s*\n+(.*)", user, re.S)
    question = qm.group(1).strip() if qm else ""
    ctx = []
    hdrs = list(_POST_HDR.finditer(user))
    for i, h in enumerate(hdrs):
        body_start = h.end()
        if i + 1 < len(hdrs):
            body_end = hdrs[i + 1].start()
        else:
            body_end = user.find("\n# Visitor question", body_start)
            if body_end == -1:
                body_end = len(user)
        chunk = user[body_start:body_end]
        own = "SOURCE: you wrote this yourself." in chunk
        m = re.search(r"---\n(.*)", chunk, re.S)
        text = (m.group(1) if m else chunk).strip()
        ctx.append((text, own))
    return question, ctx


def is_drift_abstain(meta: dict) -> bool:
    return meta.get("bucket") == "transfer_abstain" or (
        meta.get("objective") == "abstention" and meta.get("subtype") == "raft_no_oracle"
    )


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--in", dest="inp", default="output/sft_v6_full.jsonl")
    ap.add_argument("--out", dest="out", default="output/sft_v7_full.jsonl")
    ap.add_argument("--model", default=None, help="override qgen/judge model")
    ap.add_argument("--limit", type=int, default=0, help="cap re-judged records (debug)")
    ap.add_argument("--workers", type=int, default=12, help="concurrent judge calls")
    args = ap.parse_args()

    django.setup()
    from blog import sft_qgen
    from blog.models import Post
    from blog.sft_common import _is_degenerate, _is_dirty, _target_copied_into
    from blog.sft_grounded import _strip_wrapping_quotes

    client = sft_qgen.make_together_client()
    model = args.model or sft_qgen.DEFAULT_QGEN_MODEL
    sft_qgen.assert_funded(client, model)  # HARD funded gate (paid judge calls)

    # cache held-out post text by slug
    _txt_cache: dict[str, str] = {}

    def post_text(slug: str) -> str:
        if slug not in _txt_cache:
            t = Post.objects.filter(slug=slug).values_list("content_text", flat=True).first()
            _txt_cache[slug] = (t or "").strip()
        return _txt_cache[slug]

    rows = [json.loads(line) for line in open(args.inp)]
    stats = Counter()
    out_rows = list(rows)  # placeholder; drift records are replaced in-place below

    # ── collect the drift records to re-judge (preserve their output index) ──
    tasks = []  # (idx, q, ctx, ans, oslug, orig_bucket, user)
    for idx, d in enumerate(rows):
        m = d.get("meta", {})
        if not is_drift_abstain(m):
            stats["passthrough_other"] += 1
            continue
        if args.limit and len(tasks) >= args.limit:
            stats["skipped_over_limit"] += 1
            continue
        user = next(x["content"] for x in d["messages"] if x["role"] == "user")
        q, ctx = parse_user_turn(user)
        oslug = m.get("oracle_slug") or m.get("excluded_oracle_slug") or ""
        ans = _strip_wrapping_quotes(post_text(oslug))
        orig_bucket = m.get("bucket") or m.get("subtype") or "?"
        tasks.append((idx, q, ctx, ans, oslug, orig_bucket, user))

    print(f"re-judging {len(tasks)} drift records with {args.workers} workers…", flush=True)
    done = [0]

    def judge_one(task):
        idx, q, ctx, ans, oslug, orig_bucket, user = task
        route, reason, synth = "KEEP_NO_ORACLE", "oracle text missing", False
        if ans and not _is_dirty(ans) and not _is_degenerate(ans):
            v = sft_qgen.judge_drift_route(q, ctx, ans, client=client, model=model)
            route, reason = v.route, v.reason
            if v.synthesize:
                # Leak guard: verbatim target must not already sit in the user turn
                # (the exact predicate verify_sft_dataset.py HARD-gates).
                if _target_copied_into(ans, user):
                    route, reason, synth = "ABSTAIN_LEAK", "answer in block → abstain", False
                else:
                    synth = True
        done[0] += 1
        if done[0] % 100 == 0:
            print(f"  …{done[0]}/{len(tasks)} judged", flush=True)
        return idx, route, reason, synth

    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        results = list(ex.map(judge_one, tasks))

    # ── apply results in original order ──
    by_idx = {idx: (route, reason, synth) for idx, route, reason, synth in results}
    for idx, q, ctx, ans, oslug, orig_bucket, user in tasks:
        route, reason, synth = by_idx[idx]
        d = rows[idx]
        m = d["meta"]
        stats[f"route_{route}"] += 1
        if synth:
            m2 = dict(m)
            m2.update(
                objective="transfer", bucket="transfer",
                drift_route=route, drift_reason=reason,
                rerouted_from=orig_bucket, oracle_slug=oslug, qgen=True,
            )
            m2.pop("subtype", None)
            out_rows[idx] = {
                "messages": [
                    d["messages"][0],                       # system (persona RAG)
                    d["messages"][1],                       # user (question + block)
                    {"role": "assistant", "content": ans},  # verbatim held-out post
                ],
                "meta": m2,
            }
            stats["flipped_synthesize"] += 1
        else:
            m2 = dict(m)
            m2["drift_route"] = route
            m2["drift_reason"] = reason
            out_rows[idx] = {"messages": d["messages"], "meta": m2}
            stats["kept_abstain"] += 1

    with open(args.out, "w") as f:
        for r in out_rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")

    print("\n=== reroute summary ===")
    for k, v in sorted(stats.items()):
        print(f"  {v:6d}  {k}")
    flips = stats["flipped_synthesize"]
    tot = flips + stats["kept_abstain"]
    print(f"\n  drift records re-judged: {tot}")
    print(f"  flipped → SYNTHESIZE (verbatim target): {flips} ({100*flips/(tot or 1):.0f}%)")
    print(f"  kept abstain: {stats['kept_abstain']}")
    print(f"  total rows written: {len(out_rows)} → {args.out}")


if __name__ == "__main__":
    sys.exit(main())
