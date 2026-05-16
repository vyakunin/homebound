# Homebound

> Self-hosted personal-archive blog: import your social-media history, search it, talk to it, hand it to your LLM.

Homebound takes the years of content you've scattered across Google+ (RIP), Facebook, and Twitter/X, parks them in a Django blog you control, and gives both humans and language models good ways to find what's in there.

**Status:** alpha. The maintainer's deployment runs at [vyakunin.org](https://vyakunin.org); use that as a reference of what the surface looks like. APIs and DB schema may change without notice until Stage 2 ships.

## What's in the box

- **Ingest pipeline** — adapters for Google+ Takeout HTML/CSV, Facebook activity-log export (via the bundled browser extension), and Twitter/X export + Wayback Machine reconstruction
- **Django blog** — post listing + detail, tag cloud, full-text search, RSS, JSON-LD `BlogPosting` structured data, year-sharded sitemap
- **Markdown editor** — EasyMDE-backed authoring with OG-metadata fetch
- **Semantic search (Stage 1.3)** — Voyage embeddings + pgvector for natural-language queries across your whole archive
- **MCP server (Stage 1/2.7)** — stdio mode for Claude Desktop / Cursor; tools include `search_posts`, `get_post`, `on_this_day`, `search_media`, `get_timeline`, `ghostwrite`
- **Public bot widget (Stage 2.5, optional)** — a chat surface on your site that answers visitor questions from your public posts, with citations

See [`docs/HIGH_LEVEL_DESIGN.md`](docs/HIGH_LEVEL_DESIGN.md) for the architecture and [`docs/STAGE_1_FOUNDATIONS.md`](docs/STAGE_1_FOUNDATIONS.md) / [`docs/STAGE_2_LLM_FEATURES.md`](docs/STAGE_2_LLM_FEATURES.md) for the roadmap.

## Quickstart (Docker)

```bash
git clone https://github.com/vyakunin/homebound.git
cd homebound
cp .env.example .env   # fill in DB creds + optional API keys
docker compose up -d
# Wait for migrations, then visit http://localhost:8080
```

## Quickstart (local dev with Bazel)

```bash
bazel build //...
bazel test //tests:...
bazel run //:runserver           # localhost:8080
```

Management commands run via the venv (Bazel sandbox lacks `allauth`'s transitive deps):

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements_lock.txt
DJANGO_SETTINGS_MODULE=django_config.settings PYTHONPATH="bazel-bin:." \
  python manage.py import_posts --source google_plus --input-dir ./takeout/
```

## MCP setup (stdio, Claude Desktop or Cursor)

After Phase 3 lands:

```json
{
  "mcpServers": {
    "homebound": {
      "command": "docker",
      "args": ["exec", "-i", "homebound-web", "homebound", "mcp"]
    }
  }
}
```

Tools available: `search_posts`, `get_post`, `on_this_day`, `search_media`, `get_timeline`, `ghostwrite`.

## API keys (optional, enables LLM features)

| Key | What it unlocks | Get from |
|---|---|---|
| `VOYAGE_API_KEY` | Embeddings → semantic search, MCP `search_posts`, bot RAG | [dash.voyageai.com](https://dash.voyageai.com/) |
| `ANTHROPIC_API_KEY_GHOSTWRITER` | MCP `ghostwrite` tool (private authoring) | [console.anthropic.com](https://console.anthropic.com/) |
| `ANTHROPIC_API_KEY_PUBLICBOT` | Public bot widget (separate key so abuse can be revoked without breaking authoring) | same |

Without keys, semantic search + MCP `ghostwrite` + public bot are disabled; the blog and full-text search work without any external service.

## Deployment

The Docker compose in this repo is the *generic* self-host setup. For your own production deployment (TLS, reverse proxy, backups, monitoring), keep a private overlay repo. The maintainer's overlay is `homebound-platform`; treat it as a worked example, not a dependency.

## License

[AGPL-3.0](LICENSE). If you host a modified version of Homebound and serve it over the network, you must make your modifications available to your users.

## Contributing

Issues + PRs welcome. Run `bazel test //tests:...` before submitting; CI runs the same on every PR.
