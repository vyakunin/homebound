# Homebound — High-Level Technical Design

## Current State

A Django-based personal blog at **vyakunin.org** that:
1. Contains all historical posts from Google+, Facebook, and Twitter
2. Preserves original metadata (dates, media, comments, reactions, visibility, geo)
3. Supports writing new posts with Markdown editor
4. Authenticates via Google OAuth (single-user blog, owner only)
5. Runs on the home-lab server (`homeserver.local`, Dell Wyse 5070, 8 GB RAM) behind a Cloudflare Tunnel. Originally on AWS Lightsail (`backup_0_5Gb_vm`); migrated 2026-05-15 — see `NEXT.md` and `docs/DEPLOYMENT_DESIGN.md`.

This is the working prototype and flagship demo. The product strategy is in [PRODUCT_PROPOSAL_HOMEBOUND.md](PRODUCT_PROPOSAL_HOMEBOUND.md).

---

## Repository Architecture

**The public repo is the source of truth for all shared code.** This is the standard open-core model (GitLab, Sentry, PostHog). The private repo includes the public one as a git submodule and adds only platform-specific code on top.

### Why public-repo-as-source-of-truth

A one-way sync (private → public) would overwrite community contributions. Community PRs for extractor fixes, format-shift patches, new themes, etc. must land directly in the public repo. We develop shared code there too. The private repo only contains code that should never be open-sourced (billing, multi-tenant, managed infra).

### Repo Split

```
homebound (public, AGPL-3.0)            homebound-platform (private)
 THE source of truth for core code       thin SaaS layer on top
├── blog/              Django app       ├── homebound/       ← git submodule → public repo
├── extractors/        All parsers      ├── platform/        Multi-tenant overlay
├── mcp_server/        MCP (stdio)      │   ├── models.py    UserProfile, Site, Subscription
├── django_config/     Single-user      │   ├── middleware.py Tenant resolution, tier gates
├── static/                             │   ├── views.py     Dashboard, onboarding wizard
├── templates/                          │   ├── billing.py   Stripe integration
├── tests/                              │   ├── tasks.py     Managed background workers
├── deployment/                         │   └── templates/   Platform-specific UI
│   ├── docker-compose.yml              ├── landing/         Landing page app
│   ├── Dockerfile                      ├── firehose/        Aggregator app
│   └── nginx/                          ├── federation/      ActivityPub
├── manage.py                           ├── deployment/      Platform infra configs
├── requirements.txt                    ├── docs/            Product strategy, stage plans
├── homebound         CLI entrypoint    └── tests/           Platform-specific tests
├── README.md
└── LICENSE
```

### What goes where

| Public repo (free, self-hosted) | Private repo (adds SaaS layer) |
| --- | --- |
| Extractors (Google+, Facebook, Twitter) | Multi-tenant user/site models |
| Django blog app (single-user) | Tenant-resolution middleware |
| pgvector + embedding generation | Stripe billing + usage tracking |
| Semantic search | Self-checkout / onboarding wizard |
| MCP server (stdio mode) | Hosted MCP endpoint (SSE + API key auth) |
| Docker Compose (Postgres + Django + Nginx) | Managed background import workers |
| CLI (`homebound` command) | Landing page, firehose, ActivityPub |
| Themes, RSS, search, word cloud, SEO | White-glove tooling, admin dashboard |
| Tests for all the above | Platform-specific tests |

### Development workflow

**Shared code** (extractors, blog app, MCP, themes, search): develop and merge in the **public repo**. Community PRs land here. We work here too for anything that should be open.

**Platform code** (multi-tenant, billing, onboarding, firehose, federation): develop in the **private repo**. This code never touches the public repo.

**Day-to-day**: Work in the private repo most of the time (it has everything via submodule). When changing shared code, either:
- Push a branch to the public repo, PR, merge, then `git submodule update` in the private repo, or
- Develop in the submodule directory locally, push to public when ready

### Inclusion mechanism

The private repo includes the public one as a **git submodule** at `homebound/`. The platform's Django settings extend the public settings:

```python
# platform/settings.py
from homebound.django_config.settings import *  # noqa: F401,F403

INSTALLED_APPS += ['platform', 'landing', 'firehose', 'federation']
MIDDLEWARE += ['platform.middleware.TenantMiddleware']
# ... billing, S3 storage, background workers, etc.
```

Bazel: the private repo's MODULE.bazel references `//homebound/...` targets. The public repo has its own MODULE.bazel that works standalone.

### Community contribution flow

```
Community PR → public repo → review & merge → public CI builds Docker image → GHCR
                                            → private repo: git submodule update
```

We review and merge community PRs in the public repo. Then update the submodule pointer in the private repo to pick up the change. Standard open-source workflow — no special tooling needed.

---

## Turnkey Self-Hosted Setup

The public repo must deliver a fully working system in 1–2 commands. No SSH, no manual DB setup, no config editing for the basic case.

### Method 1: Docker (recommended, non-technical users)

```bash
curl -fsSL https://get.homebound.app | bash
```

The installer script:
1. Downloads `docker-compose.yml` + `.env` template
2. Runs `docker compose pull && docker compose up -d`
3. Prints "Visit http://localhost:8080 to upload your archives"

### Method 2: Docker one-liner (technical users)

```bash
docker compose -f <(curl -fsSL https://homebound.app/docker-compose.yml) up -d
```

### What `docker compose up` starts

| Container | Role |
| --- | --- |
| `homebound-db` | PostgreSQL 15 + pgvector, data volume persisted |
| `homebound-web` | Django app (gunicorn), serves web UI + API + import |
| `homebound-nginx` | Reverse proxy, static files, optional auto-TLS via Caddy sidecar |

The web UI at `:8080` handles everything: upload archives, browse posts, search, configure site. No CLI required for normal use.

### MCP server (LLM-first users)

```bash
# stdio mode — add to claude_desktop_config.json / Cursor MCP settings
docker exec homebound-web homebound mcp
```

```json
{
  "mcpServers": {
    "my-archive": {
      "command": "docker",
      "args": ["exec", "-i", "homebound-web", "homebound", "mcp"]
    }
  }
}
```

### Power-user CLI

```bash
docker exec homebound-web homebound import /data/facebook-archive.zip
docker exec homebound-web homebound generate-embeddings
docker exec homebound-web homebound search "that restaurant in Berlin 2016"
```

---

## Target Architecture

Two deployment models built from the same codebase:

### Self-Hosted (public repo, standalone)

Single-user Django app with optional LLM features (bring your own API keys). Everything runs in Docker on the user's machine or VPS. No cloud dependency.

```
┌─────────────────────────────────────────────┐
│  Self-Hosted (docker compose)               │
│                                             │
│  ┌────────┐  ┌───────────┐  ┌───────────┐  │
│  │ Nginx  │→ │  Django   │→ │ PostgreSQL│  │
│  │ :8080  │  │ (gunicorn)│  │ + pgvector│  │
│  └────────┘  └─────┬─────┘  └───────────┘  │
│                    │                        │
│        ┌───────────┴──────────┐             │
│        │                      │             │
│  ┌─────┴──────┐  ┌───────────┴──┐          │
│  │ MCP Server │  │ Media Store  │          │
│  │ (stdio)    │  │ (local disk) │          │
│  └────────────┘  └──────────────┘          │
└─────────────────────────────────────────────┘
```

### Hosted Platform (private repo adds this layer)

Multi-tenant Django with per-user isolation, managed LLM pipeline, Stripe billing.

```
┌─────────────────────────────────────────────────────────┐
│  Hosted Platform                                        │
│                                                         │
│  ┌───────┐  ┌─────────────┐  ┌──────────────┐         │
│  │ Caddy │→ │ Django App  │→ │ PostgreSQL   │         │
│  │ :443  │  │ multi-tenant│  │ + pgvector   │         │
│  │autoTLS│  │ (gunicorn)  │  │ per-user rows│         │
│  └───────┘  └──────┬──────┘  └──────────────┘         │
│                     │                                   │
│        ┌────────────┼────────────┐                      │
│        │            │            │                      │
│  ┌─────┴─────┐ ┌────┴────┐ ┌────┴──────┐              │
│  │LLM Pipeline│ │ Stripe │ │Media Store│              │
│  │RAG + Chat │ │ Billing │ │ (S3/R2)  │              │
│  │Embeddings │ │         │ │          │              │
│  └───────────┘ └─────────┘ └──────────┘              │
│                                                         │
│  ┌──────────────────────────────────────────┐          │
│  │  Background Workers (Django-Q / Celery)  │          │
│  │  - Managed archive import / extraction   │          │
│  │  - Embedding generation                  │          │
│  │  - Media preprocessing (CLIP, OCR)       │          │
│  └──────────────────────────────────────────┘          │
└─────────────────────────────────────────────────────────┘
```

---

## Data Sources

| Source | Format | Status |
| --- | --- | --- |
| Google+ | Takeout ZIP (HTML posts + CSV metadata + media) | Working extractor |
| Facebook | Activity log Chrome extension → ZIP (posts, comments, media, reactions) | Working extractor |
| Twitter/X | Chrome extension → ZIP + Wayback Machine recovery | Working extractor |
| LiveJournal | XML export | Future (plugin) |
| VKontakte | GDPR data download | Future (plugin) |
| Tumblr | Data download | Future (plugin) |
| LinkedIn | Data download | Future (plugin) |

---

## Component Breakdown

### 1. Data Extraction Pipeline (existing → public repo)

Parses each social network's export format and produces **proto binary** (`PostRecord` `.binpb` files). Keeps extraction logic separate from Django.

- **Google+ extractor**: Takeout HTML + CSV → proto binpb + media
- **Facebook extractor**: Activity log extension ZIP → proto binpb + media
- **Twitter extractor**: Extension ZIP + Wayback Machine snapshots → proto binpb
- **Base extractor**: Shared utilities for all extractors

See: [DATA_EXTRACTION_DESIGN.md](DATA_EXTRACTION_DESIGN.md)

### 2. Django Blog Application (existing → public repo, single-user)

Core Django app with models for posts, media, comments, tags, social metadata.

**Models (public repo)**:
- **Post** — text, HTML content, original date, source network, original URL, visibility, geo, embedding vector
- **PostMedia** — images, videos attached to posts (FK to Post)
- **PostComment** — imported comments from social networks
- **PostReaction** — imported +1s/likes/retweets
- **Tag** — user-defined tags / imported hashtags

**Models (private repo, platform layer)**:
- **UserProfile** — extends Django User with bio, avatar, site settings, tier, Stripe customer ID
- **Site** — per-user site configuration (domain, theme, name, description)
- **Subscription** — Stripe subscription state, tier, usage tracking

See: [DJANGO_BLOG_DESIGN.md](DJANGO_BLOG_DESIGN.md)

### 3. LLM Pipeline (Stage 2 → public repo for core, private for managed)

RAG-based system for all AI features. Architecture is model-agnostic.

**Public repo (self-hosted)**:
- Embedding generation command (`homebound generate-embeddings`)
- pgvector vector column on Post
- Semantic search endpoint
- MCP server (stdio) exposing search, ghostwrite, on-this-day, timeline tools

**Private repo (platform)**:
- Managed embedding generation on import
- Hosted MCP endpoint (SSE + API key auth)
- Web chat UI (librarian, ghostwriter, archivist personas)
- Public bot widget
- Media preprocessing (CLIP, OCR, alt-text)
- Per-tier model selection and rate limiting

**LLM provider strategy**: Default to Claude Haiku / GPT-4o-mini for cost efficiency. White Glove tier gets stronger models. Self-hosted users bring their own keys or use Ollama.

See: [STAGE_2_LLM_FEATURES.md](STAGE_2_LLM_FEATURES.md)

### 4. MCP Server (Stage 2 → public repo)

MCP server ships as part of the free, self-hosted product. It's the primary interface for LLM-first users.

**Tools**: `search_posts`, `get_post`, `on_this_day`, `ghostwrite`, `search_media`, `get_timeline`

**Resources**: `archive://profile`, `archive://recent`, `archive://stats`, `archive://sources`

**Deployment**:
- Public repo: stdio mode via `homebound mcp` (configured in Claude Desktop / Cursor)
- Private repo adds: authenticated SSE endpoint at `username.homebound.app/mcp/`

See: [STAGE_2_LLM_FEATURES.md](STAGE_2_LLM_FEATURES.md)

### 5. Auth & User Management

**Public repo (single-user)**: Google OAuth or local admin user. Single owner. Simple.

**Private repo (multi-user)**: Multi-user registration (email + password, Google OAuth, GitHub OAuth). Per-user site ownership. Self-checkout. Admin tools for white-glove.

See: [AUTH_AND_EDITOR_DESIGN.md](AUTH_AND_EDITOR_DESIGN.md)

### 6. Billing (Stage 3 → private repo only)

Stripe integration for recurring subscriptions.

- Three hosted tiers ($1, $10, $50/mo)
- One-time archive processing fee ($20-50)
- Promo codes for raffle winners (10 free slots)
- Usage tracking for LLM API calls and storage
- Webhook-based subscription lifecycle (create, update, cancel, invoice)

See: [STAGE_3_MONETIZATION.md](STAGE_3_MONETIZATION.md)

### 7. Distribution Layer (Stage 4 → private repo)

- **ActivityPub**: Each user's site is a Fediverse actor. Mastodon users can follow and interact.
- **RSS/Atom feeds**: Per-user, per-source, per-tag feeds (existing in public repo, extend).
- **Firehose**: Opt-in global aggregator feed on homebound.app.
- **SEO**: Auto sitemaps, Schema.org structured data, semantic HTML (existing in public repo).

See: [STAGE_4_DISTRIBUTION.md](STAGE_4_DISTRIBUTION.md)

### 8. Deployment & Hosting

**Public repo**: Docker Compose (Postgres + pgvector + Django + Nginx). One-command setup. Local disk for media.

**Private repo**: Platform infrastructure on Lightsail/DO. Caddy with on-demand TLS for custom domains. S3/R2 for media. Background workers.

See: [DEPLOYMENT_DESIGN.md](DEPLOYMENT_DESIGN.md)

---

## Tech Stack

| Layer | Public Repo (self-hosted) | Private Repo (adds) | Rationale |
| --- | --- | --- | --- |
| Backend | Django 5.x, Python 3.11+ | + Django-Q/Celery for managed tasks | Async import, embedding gen |
| Database | PostgreSQL 15 + pgvector | Same, multi-tenant rows | Vector search, no new infra |
| Auth | django-allauth, single-user | + multi-user, email/password, GitHub | Platform needs registration |
| LLM | OpenAI/Anthropic API (user keys) | Managed keys + rate limiting | Self-hosted = BYOK |
| MCP | stdio server (`homebound mcp`) | + SSE hosted endpoint + API key auth | Free addon in public repo |
| Media | Local filesystem + Nginx | S3/R2 | Scalable for hosted |
| Billing | — | Stripe | Platform-only |
| Federation | — | ActivityPub | Platform-only |
| Editor | Markdown + EasyMDE | Same | Already sufficient |
| Deployment | Docker Compose + Nginx | + Caddy auto-TLS per domain | Multi-domain for hosted |
| CI | GitHub Actions | Same + public repo sync | Build, test, deploy, publish |

---

## Unified Intermediate Format

All extractors produce JSONL with this schema per post (unchanged):

```json
{
  "source": "google_plus|facebook|twitter",
  "source_id": "original_platform_id_or_url",
  "created_at": "2017-05-25T13:28:00-07:00",
  "updated_at": "2017-05-25T13:28:00-07:00",
  "content_text": "plain text of the post",
  "content_html": "<p>HTML of the post</p>",
  "visibility": "public|friends|private",
  "location": {"lat": 37.39, "lng": -122.06, "name": "Mountain View, CA"},
  "reshared_from": {"author": "Name", "url": "..."},
  "media": [
    {"type": "image", "original_filename": "IMG_123.jpg", "local_path": "google_plus/2017/05/IMG_123.jpg"}
  ],
  "reactions": [{"type": "plus_one", "user": "Name", "user_url": "..."}],
  "comments": [{"author": "Name", "text": "...", "date": "..."}],
  "tags": ["hashtag1", "hashtag2"],
  "extra": {}
}
```

---

## Implementation Phasing

### Completed Phases

| Phase | Scope | Status |
| --- | --- | --- |
| **1** | Django app + Bazel build + G+ extraction + local import + tests | Done |
| **2** | Deploy + DNS for vyakunin.org (originally `backup_0_5Gb_vm` on Lightsail; migrated to homeserver + Cloudflare 2026-05-15) | Done |
| **3** | Auth + posting UI + editor | Done |
| **4** | Facebook extraction & import | Done |
| **5** | Polish: RSS, search, word cloud, SEO, theming | Done |

### Product Stages (New)

| Stage | Scope | Depends On | Details |
| --- | --- | --- | --- |
| **1** | Repo split, multi-user foundations, LLM index, self-checkout, onboard self + wife | Completed phases | [STAGE_1_FOUNDATIONS.md](STAGE_1_FOUNDATIONS.md) |
| **2** | LLM features: archivist, ghostwriter, librarian, MCP server, public bot | Stage 1 | [STAGE_2_LLM_FEATURES.md](STAGE_2_LLM_FEATURES.md) |
| **3** | Monetization: Stripe, pricing tiers, promo codes, landing page | Stage 2 | [STAGE_3_MONETIZATION.md](STAGE_3_MONETIZATION.md) |
| **4** | Distribution: ActivityPub, POSSE, firehose, marketing execution | Stage 3 | [STAGE_4_DISTRIBUTION.md](STAGE_4_DISTRIBUTION.md) |

---

## Open Questions

1. ~~**Multi-tenancy model**~~: **Decided**: Multi-tenant Django in private repo. Public repo stays single-user. Private repo adds `owner` FK + tenant middleware on top.
2. **LLM provider lock-in**: Embedding format is provider-specific. Switching embedding models requires re-indexing. Mitigate by abstracting the embedding interface.
3. **Media storage at scale**: S3/R2 for hosted tier. Average Facebook archive is 5-20 GB of media.
4. **Self-hosted LLM support**: Ollama/llama.cpp for fully local embeddings. Strong selling point. Target Stage 2.
5. **ActivityPub complexity**: Start outbox-only (publish to Fediverse) before inbox (receive replies).
6. **Custom domain TLS**: Caddy with on-demand TLS for the hosted platform. Public repo uses plain Nginx + optional Certbot.
7. ~~**Submodule vs monorepo-with-sync**~~: **Decided**: Public repo is source of truth for shared code (standard open-core model). Private repo includes it as git submodule. Community PRs land in public repo directly. No sync scripts needed.
