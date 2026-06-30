# scripts/

Permanent helper scripts for the homebound blog backend. (Throwaway/one-off
scripts live in `scripts/oneoff/`.)

| Script | Purpose |
|---|---|
| `probe_retrieval.py` | Read-only probe of the bot retrieval/ranking pipeline for one query — prints the final top-K and, for an optional expected post id, its rank in the fused pool (pre-rerank), the reranked pool, and the final top-K. Use it to verify a retrieval/ranking change against a live or snapshot DB **without deploying**. Run from the web image: `docker compose run --rm --no-deps -T -e PYTHONUTF8=1 web python scripts/probe_retrieval.py "<query>" [post_id]`. |
| `recall_golden.py` | Golden recall gate: runs curated `query → expected slug` scenarios through live `bot_retrieval.retrieve()` against a Postgres+pgvector DB with real Voyage embeddings, asserting each oracle lands in the top-K. The bazel suite is SQLite-only (no semantic recall), so this is the END-STATE proof for any recall/fanout/rerank change. Exit 0 iff all pass; prints per-stage ranks on a miss. Canonical case: «ты болел недавно?» → the Ramsay-Hunt post. Run against the `:5434` testing DB: `DB_HOST=localhost DB_PORT=5434 DB_USER=postgres DB_PASSWORD=sftbuild DB_NAME=homebound VOYAGE_API_KEY="$(cat ~/tokens/homebound_voyage_key)" DJANGO_SETTINGS_MODULE=django_config.settings PYTHONPATH="bazel-bin:." .venv/bin/python scripts/recall_golden.py`. |
