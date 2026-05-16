# Stage 1: Foundations — Repo Split, Multi-User, LLM Index, Self-Checkout

**Goal**: Split the codebase into public (self-hosted) and private (platform) repos. Transform the private repo into a multi-user product with vector-indexed content. Deliver a turnkey self-hosted product in the public repo. Onboard yourself and wife as the first two "product users."

**Depends on**: All current phases complete (vyakunin.org live with G+, Facebook, Twitter)

---

## 1.1 Repository Split

### The Split

The current `personal_blog` repo becomes `homebound-platform` (private). The shared core is extracted into `homebound` (public, AGPL-3.0). **The public repo is the source of truth for all shared code** — the standard open-core model (GitLab, Sentry, PostHog). Community PRs for extractor fixes, format-shift patches, new themes, etc. land directly in the public repo. The private repo includes it as a git submodule and adds only platform-specific code.

### What moves to public repo

| Directory/File | Notes |
| --- | --- |
| `blog/` | Django blog app — models, views, templates, management commands, templatetags. Single-user (no `owner` FK). |
| `extractors/` | All extractors: Google+, Facebook, base, schema. Twitter when ready. |
| `mcp_server/` | **New.** MCP server in stdio mode. Free addon. |
| `django_config/` | Settings, URLs, WSGI — configured for single-user standalone mode. |
| `static/`, `templates/` | All CSS, JS, images, HTML templates. |
| `tests/` | Tests for all public components. |
| `deployment/` | `docker-compose.yml`, `Dockerfile`, `nginx/` config. |
| `manage.py` | Django manage.py. |
| `homebound` | **New.** CLI entrypoint script wrapping management commands. |
| `requirements.txt` | Dependencies for the public subset only. |
| `README.md`, `LICENSE` | AGPL-3.0. Polished README with screenshots, quickstart, MCP setup. |

### What stays private

| Directory | Purpose |
| --- | --- |
| `homebound/` | Git submodule → public repo |
| `platform/` | Multi-tenant layer: UserProfile, Site, Subscription, tenant middleware, onboarding wizard, dashboard, Stripe |
| `landing/` | Landing page at homebound.app |
| `firehose/` | Global aggregator (Stage 4) |
| `federation/` | ActivityPub (Stage 4) |
| `docs/` | Product strategy, stage plans, design docs |
| `deployment/` | Platform-specific infra (Caddy, multi-domain, S3) |
| `tests/` | Platform-specific tests |

### CLI entrypoint (`homebound` command)

The public repo ships a `homebound` CLI that wraps Django management commands into a clean interface:

```bash
homebound setup          # Run migrations, create superuser, configure site
homebound import FILE    # Auto-detect source, extract, import posts
homebound generate-embeddings  # Generate vector embeddings (needs OPENAI_API_KEY)
homebound serve          # Start gunicorn
homebound mcp            # Start MCP server (stdio mode)
homebound search "query" # Quick CLI search
```

Implementation: thin Python script that dispatches to `manage.py` commands. Installed as a console_script entry point.

### Docker Compose (public repo)

```yaml
services:
  db:
    image: pgvector/pgvector:pg15
    volumes: [homebound-data:/var/lib/postgresql/data]
    environment:
      POSTGRES_DB: homebound
      POSTGRES_USER: homebound
      POSTGRES_PASSWORD: ${DB_PASSWORD:-homebound}

  web:
    image: ghcr.io/vyakunin/homebound:latest
    depends_on: [db]
    volumes: [homebound-media:/app/media]
    environment:
      DATABASE_URL: postgres://homebound:${DB_PASSWORD:-homebound}@db:5432/homebound
      OPENAI_API_KEY: ${OPENAI_API_KEY:-}  # optional, for LLM features
    ports: ["8080:8080"]

  nginx:
    image: nginx:alpine
    depends_on: [web]
    ports: ["80:80"]
    volumes: [./nginx.conf:/etc/nginx/conf.d/default.conf]

volumes:
  homebound-data:
  homebound-media:
```

First run: `web` container auto-runs migrations and prints "Visit http://localhost:8080 to upload archives."

### Development workflow

**Shared code** (extractors, blog app, MCP, themes, search): develop and merge in the **public repo**. Community PRs land here. We work here too.

**Platform code** (multi-tenant, billing, onboarding, firehose): develop in the **private repo**. Never touches the public repo.

**Day-to-day**: Work in the private repo (has everything via submodule). When changing shared code, push to the public repo and update the submodule pointer.

```
Community PR → public repo → review & merge → public CI builds Docker image → GHCR
                                            → private repo: git submodule update
```

### Migration path from current repo

1. Create `homebound` public repo on GitHub
2. Move shared code (blog/, extractors/, static/, templates/, tests/, django_config/, deployment/) into the public repo with full git history (`git filter-branch` or `git subtree split`)
3. Restructure current repo as `homebound-platform`: add `homebound/` submodule, create `platform/` Django app
4. Set up public repo CI: tests + Docker image build → GHCR
5. Verify public repo works standalone: `docker compose up` → working blog
6. Verify private repo works with submodule: platform tests pass, vyakunin.org deploys

---

## 1.2 Multi-User Architecture (Private Repo Only)

### Decision: Multi-tenant Django with row-level isolation

The public repo stays **single-user** (no `owner` FK, no tenant middleware). The private repo adds multi-tenancy on top via the `platform/` Django app.

### Platform models

1. **UserProfile**: Extends Django User with `bio`, `avatar`, `display_name`, `site_slug`, `custom_domain`, `tier` (IntegerChoices: FREE=0, BASIC=1, PREMIUM=2, WHITE_GLOVE=3), `stripe_customer_id`, `storage_used_bytes`.

2. **Site**: Per-user site config — FK to User, `name`, `description`, `theme`, `is_public`, `enable_public_bot`, `activitypub_enabled`.

3. **Platform adds `owner` FK to Post model** via migration in the private repo. All platform views filter by owner. URL routing includes site_slug or domain.

### Tenant resolution

`platform.middleware.TenantMiddleware`:
- Resolves current site from hostname (`{slug}.homebound.app` or custom domain)
- Sets `request.site` and `request.site_owner` for all views
- The blog app's views use `request.site_owner` to filter querysets (the platform injects this via a mixin or monkey-patch, without modifying the public repo's code)

### URL routing

- `vyakunin.homebound.app` or `vyakunin.org` → user "vyakunin"
- `{slug}.homebound.app` → user with that slug
- Custom domains via Caddy dynamic routing

### Migration strategy

Existing vyakunin.org data stays intact:
1. Creates UserProfile + Site tables
2. Creates UserProfile for "vyakunin" linked to existing OAuth user
3. Adds `owner_id` FK to Post (nullable initially)
4. Backfills `owner_id` for all existing posts
5. Makes `owner_id` non-nullable
6. Updates platform views to filter by `request.site.owner`

---

## 1.3 LLM-Friendly Index (pgvector) — Public Repo

This goes into the public repo. Self-hosted users get semantic search if they provide an `OPENAI_API_KEY`.

### Deliverables

1. **pgvector** in Docker Compose (use `pgvector/pgvector:pg15` image — zero config).

2. **Vector column on Post**: `embedding = VectorField(dimensions=1536, null=True)`

3. **`homebound generate-embeddings` command**:
   - Iterates over posts with `embedding IS NULL`
   - Batches text (content_text + tags + location) into OpenAI API calls
   - Writes embeddings back. Idempotent.
   - Graceful no-op if `OPENAI_API_KEY` not set (prints "Set OPENAI_API_KEY to enable")
   - Cost: ~$0.10 for a 10-year archive

4. **Semantic search endpoint**: `/api/search/semantic/?q=...`
   - Generates query embedding, pgvector cosine similarity, top-N results
   - Falls back to keyword search if no embeddings exist

5. **Hybrid search UI**: Toggle between keyword and semantic modes. Show which mode is active.

---

## 1.4 Self-Checkout Interface (Private Repo)

### For wife and future hosted users

1. **Registration page** `/signup/`: Email + password, Google OAuth. Choose site slug.

2. **Archive upload** `/dashboard/import/`: Drag-and-drop ZIP, auto-detect source, preview ("Found 2,978 posts"), background import.

3. **Background worker**: Django-Q (database backend, no Redis needed). Receives ZIP → extracts → imports → generates embeddings → notifies.

4. **Dashboard** `/dashboard/`: Stats, recent imports with status, site settings.

---

## 1.5 Onboarding: Self + Wife

### Onboard vyakunin (convert to product user)

1. Run multi-tenant migration on production (private repo)
2. Verify vyakunin.org still works with `owner` FK filtering
3. Generate embeddings for all existing posts
4. Verify semantic search works
5. Verify existing RSS, search, word cloud still work

### Onboard wife (first external user)

1. Signs up via `/signup/` (self-checkout)
2. Uploads GDPR archives
3. Background worker processes archives
4. Site live at `{slug}.homebound.app`
5. Collect feedback on friction

### Success criteria
- Complete flow without CLI/SSH/technical help
- Import < 30 minutes for typical archive
- Site looks good immediately after import
- Can explain what it is to a friend

---

## 1.6 Infrastructure Changes

### DNS
- Register `homebound.app` (or chosen domain)
- Wildcard DNS: `*.homebound.app` → platform server
- vyakunin.org continues as custom domain

### Server
- vyakunin.org now runs on the home-lab box (`homeserver.local`, 8 GB RAM); the original 0.5 GB Lightsail instance is gone (migrated 2026-05-15).
- Platform tier (multi-tenant + workers + pgvector) likely wants a separate 2 GB cloud instance (~$10/mo) — keeps the home-lab single-tenant and stops a single noisy customer from impacting it.
- Alternative: scale the home box (it has headroom for pgvector + workers today). Decide at Stage 1.6 once load profile is clearer.

### File storage
- Local filesystem for Stage 1 (simplest)
- Each user's media in `/app/media/{user_id}/`
- Migrate to S3/R2 in Stage 3

---

## Exit Criteria

- [ ] Public repo (`homebound`) exists and works standalone
- [ ] `docker compose up` on public repo → working blog in < 2 minutes
- [ ] `homebound import` CLI works end-to-end on a GDPR archive
- [ ] `homebound mcp` starts a working MCP server
- [ ] Sync script publishes public subset from private repo
- [ ] Multi-tenant model deployed in private repo: Post has `owner` FK, views filter by site
- [ ] vyakunin.org works identically (no regression)
- [ ] pgvector installed, embeddings generated for vyakunin's posts
- [ ] Semantic search returns relevant results
- [ ] Self-checkout works end-to-end: signup → upload → import → live site
- [ ] Wife successfully onboarded without technical assistance
- [ ] Background import worker processes archives reliably
- [ ] Dashboard shows import status and site stats
- [ ] `bazel test //tests:...` passes
- [ ] `*.homebound.app` wildcard routing works

---

## Estimated Effort

| Component | Estimate |
| --- | --- |
| Repo split + git history migration + public Docker setup | 1-2 weeks |
| `homebound` CLI entrypoint | 2-3 days |
| Multi-tenant migration + platform views | 1-2 weeks |
| pgvector + embedding pipeline | 1 week |
| Self-checkout UI (signup, upload, dashboard) | 2 weeks |
| Background task infrastructure (Django-Q) | 3-4 days |
| Infrastructure (DNS, server, wildcard routing) | 2-3 days |
| MCP server (stdio, basic tools) | 3-5 days |
| Onboarding + testing + polish | 1 week |
| **Total** | **7-10 weeks** |

---

## Cleanup (Post-Implementation)

- [ ] Extract multi-tenant patterns to rule file if reusable
- [ ] Update DEPLOYMENT_DESIGN.md with platform server details
- [ ] Update HIGH_LEVEL_DESIGN.md with actual architecture decisions
- [ ] Write public repo README with screenshots and quickstart
- [ ] Delete this planning doc or convert to current-state doc
