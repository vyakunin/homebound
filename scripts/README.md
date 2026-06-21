# scripts/

Permanent helper scripts for the homebound blog backend. (Throwaway/one-off
scripts live in `scripts/oneoff/`.)

| Script | Purpose |
|---|---|
| `probe_retrieval.py` | Read-only probe of the bot retrieval/ranking pipeline for one query — prints the final top-K and, for an optional expected post id, its rank in the fused pool (pre-rerank), the reranked pool, and the final top-K. Use it to verify a retrieval/ranking change against a live or snapshot DB **without deploying**. Run from the web image: `docker compose run --rm --no-deps -T -e PYTHONUTF8=1 web python scripts/probe_retrieval.py "<query>" [post_id]`. |
