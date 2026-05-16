# Django Blog Application — Design

> Core deliverable of [Phase 1](HIGH_LEVEL_DESIGN.md#phase-1--django-app--g-extraction--local-import). Bazel wiring, models, views, and tests.

## Project Structure (Bazel-First)

Layout mirrors the visa_bulletin project's Bazel conventions: one BUILD target per Python file, explicit dependency graphs, tests as first-class Bazel targets.

```
personal_blog/
  MODULE.bazel                ← Bazel 8+ module (name = "personal_blog")
  requirements.txt            ← pip deps (Django, gunicorn, etc.)
  requirements.lock           ← generated lock file for Bazel pip
  BUILD                       ← root targets: runserver, migrate, makemigrations, import_posts
  manage.py
  blog/                       ← main Django app
    BUILD                     ← py_library per file: models, views, urls, admin, adapters
    models.py
    views.py
    urls.py
    admin.py
    adapters.py               ← single-user OAuth adapter
    apps.py
    migrations/
      BUILD                   ← filegroup(name = "migrations", srcs = glob(["*.py"]))
    templatetags/
      BUILD
      blog_tags.py
    management/
      commands/
        BUILD                 ← py_binary for import_posts
        import_posts.py
  django_config/
    BUILD                     ← py_library: settings, urls, wsgi (mirrors visa_bulletin pattern)
    settings.py
    urls.py
    wsgi.py
  templates/
    base.html
    blog/
      post_list.html
      post_detail.html
      post_form.html
  static/
    css/style.css
    js/editor.js
  extractors/                 ← standalone, no Django dep
    BUILD                     ← py_library + py_binary per extractor
    google_plus.py
    facebook.py
    twitter.py
  tests/
    BUILD                     ← py_test targets + django_setup helper
    django_setup.py           ← test DB setup (reads .env, creates test DB)
    conftest.py               ← pytest-django fixtures (py_library, not auto-discovered)
    django_test.bzl           ← macro: django_py_test() adds common Django deps
    test_models.py
    test_views.py
    test_google_plus_extractor.py
    test_import_posts.py
    fixtures/                 ← sample G+ HTML, expected JSONL, etc.
  media/                      ← uploaded/imported images (gitignored)
  docs/
  deployment/
```

## Local Development Setup

### Prerequisites

- **Python 3.13**: `/opt/homebrew/bin/python3.13` (installed via Homebrew)
- **PostgreSQL 15**: installed via Homebrew at `/opt/homebrew/opt/postgresql@15/`; default user is the OS username (e.g. `vyakunin`), no password
- **`postgres` role does NOT exist** in the Homebrew install — always use the OS username in `.env`

### One-Time Setup

```bash
cd ~/cursor_projects/personal_blog

# 1. Create and activate venv (must use Homebrew Python 3.13, not system Python 3.9)
/opt/homebrew/bin/python3.13 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# 2. Add postgresql@15 to PATH (Homebrew doesn't do this automatically)
export PATH="/opt/homebrew/opt/postgresql@15/bin:$PATH"

# 3. Create the database (Homebrew PG is already running; default socket is /tmp)
createdb personal_blog

# 4. Create .env (gitignored) with local credentials
cat > .env <<EOF
DEBUG=True
SECRET_KEY=dev-secret-key-do-not-use-in-production
ALLOWED_HOSTS=localhost,127.0.0.1
DB_NAME=personal_blog
DB_USER=vyakunin
DB_PASSWORD=
DB_HOST=localhost
DB_PORT=5432
EOF

# 5. Run migrations
python manage.py migrate
```

Settings auto-load `.env` on startup via a plain `os.environ.setdefault` loop at the top of `django_config/settings.py` — no external library needed.

### Daily Dev Workflow

```bash
cd ~/cursor_projects/personal_blog
source .venv/bin/activate
export PATH="/opt/homebrew/opt/postgresql@15/bin:$PATH"
python manage.py runserver 8100     # http://127.0.0.1:8100/  (port 8100 avoids conflicts with other dev servers)
```

### Key Facts

| Item | Value |
|---|---|
| Python version | 3.13 (Homebrew) |
| PostgreSQL version | 15 (Homebrew) |
| PG socket | `/tmp` (default for Homebrew) |
| PG port | 5432 |
| Local DB user | `vyakunin` (OS username, no password) |
| Local DB name | `personal_blog` |
| `.env` location | project root (gitignored) |

---

## Bazel Wiring

### MODULE.bazel

```python
module(name = "personal_blog", version = "1.0.0")

bazel_dep(name = "rules_python", version = "0.40.0")

python = use_extension("@rules_python//python/extensions:python.bzl", "python")
python.toolchain(python_version = "3.11")

pip = use_extension("@rules_python//python/extensions:pip.bzl", "pip")
pip.parse(
    hub_name = "personal_blog_pip",
    python_version = "3.11",
    requirements_lock = "//:requirements.lock",
)
use_repo(pip, "personal_blog_pip")
```

### Root BUILD (top-level targets)

```python
load("@rules_python//python:defs.bzl", "py_binary")
load("@personal_blog_pip//:requirements.bzl", "requirement")

# Dev server
py_binary(
    name = "runserver",
    srcs = ["manage.py"],
    main = "manage.py",
    args = ["runserver", "8000", "--noreload"],
    data = ["//blog:migrations", "//templates"],
    env = {"DJANGO_SETTINGS_MODULE": "django_config.settings"},
    deps = [
        "//django_config:settings",
        "//django_config:urls",
        "//blog:apps",
        "//blog:models",
        "//blog:views",
        "//blog:admin",
        requirement("Django"),
        requirement("psycopg2_binary"),
    ],
)

# Database migrations
py_binary(
    name = "migrate",
    srcs = ["manage.py"],
    main = "manage.py",
    args = ["migrate", "--noinput"],
    data = ["//blog:migrations"],
    env = {"DJANGO_SETTINGS_MODULE": "django_config.settings"},
    deps = [
        "//django_config:settings",
        "//blog:apps",
        "//blog:models",
        requirement("Django"),
        requirement("psycopg2_binary"),
    ],
)

py_binary(
    name = "makemigrations",
    srcs = ["manage.py"],
    main = "manage.py",
    args = ["makemigrations"],
    data = ["//blog:migrations"],
    env = {"DJANGO_SETTINGS_MODULE": "django_config.settings"},
    deps = [
        "//django_config:settings",
        "//blog:apps",
        "//blog:models",
        requirement("Django"),
        requirement("psycopg2_binary"),
    ],
)

# JSONL importer
py_binary(
    name = "import_posts",
    srcs = ["manage.py"],
    main = "manage.py",
    args = ["import_posts"],
    env = {"DJANGO_SETTINGS_MODULE": "django_config.settings"},
    deps = [
        "//django_config:settings",
        "//blog:apps",
        "//blog:models",
        "//blog/management/commands:import_posts",
        requirement("Django"),
        requirement("psycopg2_binary"),
    ],
)

filegroup(
    name = "templates",
    srcs = glob(["templates/**/*.html"]),
    visibility = ["//visibility:public"],
)
```

### django_config/BUILD

```python
load("@rules_python//python:defs.bzl", "py_library")
load("@personal_blog_pip//:requirements.bzl", "requirement")

py_library(
    name = "settings",
    srcs = ["settings.py"],
    visibility = ["//visibility:public"],
    deps = [requirement("Django")],
)

py_library(
    name = "urls",
    srcs = ["urls.py"],
    visibility = ["//visibility:public"],
    deps = [
        "//blog:urls",
        requirement("Django"),
    ],
)

py_library(
    name = "wsgi",
    srcs = ["wsgi.py"],
    visibility = ["//visibility:public"],
    deps = [
        ":settings",
        requirement("Django"),
    ],
)
```

### blog/BUILD

```python
load("@rules_python//python:defs.bzl", "py_library")
load("@personal_blog_pip//:requirements.bzl", "requirement")

py_library(
    name = "models",
    srcs = ["models.py"],
    visibility = ["//visibility:public"],
    deps = [
        requirement("Django"),
        requirement("psycopg2_binary"),
    ],
)

py_library(
    name = "views",
    srcs = ["views.py"],
    visibility = ["//visibility:public"],
    deps = [
        ":models",
        requirement("Django"),
    ],
)

py_library(
    name = "urls",
    srcs = ["urls.py"],
    visibility = ["//visibility:public"],
    deps = [
        ":views",
        requirement("Django"),
    ],
)

py_library(
    name = "admin",
    srcs = ["admin.py"],
    visibility = ["//visibility:public"],
    deps = [
        ":models",
        requirement("Django"),
    ],
)

py_library(
    name = "apps",
    srcs = ["apps.py"],
    visibility = ["//visibility:public"],
    deps = [requirement("Django")],
)

py_library(
    name = "adapters",
    srcs = ["adapters.py"],
    visibility = ["//visibility:public"],
    deps = [
        requirement("Django"),
        requirement("django-allauth"),
    ],
)

filegroup(
    name = "migrations",
    srcs = glob(["migrations/*.py"]),
    visibility = ["//visibility:public"],
)
```

### tests/BUILD + django_test.bzl macro

```python
# tests/django_test.bzl
load("@rules_python//python:defs.bzl", "py_test")
load("@personal_blog_pip//:requirements.bzl", "requirement")

def django_py_test(name, srcs, deps, data = None, size = "small", env = None, **kwargs):
    """Macro that adds common Django test deps."""
    all_deps = list(deps) + [
        ":django_setup",
        "//django_config:settings",
        "//blog:apps",
        requirement("Django"),
        requirement("psycopg2_binary"),
        requirement("pytest"),
        requirement("pytest-django"),
    ]
    all_data = list(data or []) + ["//blog:migrations"]
    all_env = dict(env or {})
    all_env.setdefault("DJANGO_SETTINGS_MODULE", "django_config.settings")
    all_env.setdefault("RUNNING_TESTS", "1")
    py_test(
        name = name,
        srcs = srcs,
        main = srcs[0],
        deps = all_deps,
        data = all_data,
        env = all_env,
        size = size,
        **kwargs,
    )
```

```python
# tests/BUILD
load("@rules_python//python:defs.bzl", "py_library")
load("@personal_blog_pip//:requirements.bzl", "requirement")
load(":django_test.bzl", "django_py_test")

py_library(
    name = "django_setup",
    srcs = ["django_setup.py"],
    deps = [
        requirement("Django"),
        "//django_config:settings",
    ],
)

py_library(
    name = "conftest_lib",
    srcs = ["conftest.py"],
    deps = [
        ":django_setup",
        "//blog:models",
        requirement("pytest"),
        requirement("pytest-django"),
    ],
)

django_py_test(
    name = "test_models",
    srcs = ["test_models.py"],
    deps = ["//blog:models", ":conftest_lib"],
)

django_py_test(
    name = "test_views",
    srcs = ["test_views.py"],
    deps = ["//blog:views", "//blog:urls", ":conftest_lib"],
)

django_py_test(
    name = "test_import_posts",
    srcs = ["test_import_posts.py"],
    deps = [
        "//blog:models",
        "//blog/management/commands:import_posts",
        ":conftest_lib",
    ],
    data = ["//tests/fixtures"],
)

# Extractor tests don't need Django — plain py_test
py_test(
    name = "test_google_plus_extractor",
    srcs = ["test_google_plus_extractor.py"],
    main = "test_google_plus_extractor.py",
    size = "small",
    deps = [
        "//extractors:google_plus",
        requirement("beautifulsoup4"),
        requirement("lxml"),
        requirement("pytest"),
    ],
    data = ["//tests/fixtures"],
)
```

### Key Bazel conventions (from visa_bulletin)

- **One target per file**: each `.py` file gets its own `py_library` or `py_binary`.
- **IWYU (Include What You Use)**: depend on the specific target you import, not aggregate targets.
- **Migrations as filegroups**: `filegroup(name = "migrations", srcs = glob(["migrations/*.py"]))` — included in `data =` for migrate/runserver.
- **conftest.py as py_library**: Bazel doesn't auto-discover conftest; it must be an explicit dependency.
- **django_py_test macro**: reduces boilerplate by adding Django settings, DB setup, and common deps to every test target.
- **DJANGO_SETTINGS_MODULE via env**: set on every py_binary and py_test that touches Django.
- **Templates as filegroup**: `glob(["templates/**/*.html"])` collected at root, referenced via `data =`.
- **No Bazel on the instance**: 0.5 GB instance cannot run Bazel. All builds happen in CI (GitHub Actions) or locally. The instance only pulls pre-built Docker images.

## Testing

Tests run via Bazel (pytest + pytest-django under the hood):

```bash
# All tests
bazel test //tests:...

# Single test
bazel test //tests:test_models

# Extractor tests (no DB needed)
bazel test //tests:test_google_plus_extractor
```

**Test categories:**

| Category | Runner | DB needed | Example |
|----------|--------|-----------|---------|
| Model tests | django_py_test | Yes | Slug generation, uniqueness constraints, visibility filtering |
| View tests | django_py_test | Yes | HTTP status codes, context data, pagination, auth redirects |
| Import tests | django_py_test | Yes | JSONL → DB round-trip, idempotency, media path resolution |
| Extractor tests | py_test | No | HTML → JSONL parsing, edge cases, encoding |

**Phase 1 test coverage targets:**
- G+ extractor: parse known HTML fixtures → verify JSONL output fields
- Import command: load fixture JSONL → verify Post/PostMedia/PostComment records
- Models: slug generation (with/without title), uniqueness constraint on (source, source_id)
- Views: post list returns only PUBLIC posts, post detail 200/404, archive grouping

See: [HIGH_LEVEL_DESIGN.md — Phase 1 exit criteria](HIGH_LEVEL_DESIGN.md#phase-1--django-app--g-extraction--local-import)

## Models

### Post

The central model. Stores both imported social-network posts and new blog posts.

```python
class PostSource(models.IntegerChoices):
    INVALID = 0, "Invalid/Unknown"
    BLOG = 1, "Blog"             # new post written on the blog
    GOOGLE_PLUS = 2, "Google+"
    FACEBOOK = 3, "Facebook"
    TWITTER = 4, "Twitter"

class PostVisibility(models.IntegerChoices):
    PUBLIC = 1, "Public"
    UNLISTED = 2, "Unlisted"     # accessible by URL, not listed
    PRIVATE = 3, "Private"       # owner only

class Post(models.Model):
    # Content
    title = models.CharField(max_length=500, blank=True)
    slug = models.SlugField(max_length=200, unique=True)
    content_text = models.TextField(blank=True)       # plain text (for search)
    content_html = models.TextField(blank=True)        # rendered HTML (for display)
    content_markdown = models.TextField(blank=True)    # markdown source (for new posts)

    # Metadata
    created_at = models.DateTimeField(db_index=True)   # original post date
    updated_at = models.DateTimeField(auto_now=True)
    imported_at = models.DateTimeField(null=True)       # when imported into blog
    source = models.IntegerField(choices=PostSource.choices, default=PostSource.BLOG)
    source_id = models.CharField(max_length=500, blank=True, db_index=True)  # original URL or platform ID
    source_url = models.URLField(max_length=1000, blank=True)  # link back to original (if still alive)
    visibility = models.IntegerField(choices=PostVisibility.choices, default=PostVisibility.PUBLIC)

    # Location
    location_name = models.CharField(max_length=500, blank=True)
    location_lat = models.FloatField(null=True, blank=True)
    location_lng = models.FloatField(null=True, blank=True)

    # Social context
    reshared_from_author = models.CharField(max_length=300, blank=True)
    reshared_from_url = models.URLField(max_length=1000, blank=True)

    # Denormalized counts (updated on import, not live)
    reaction_count = models.IntegerField(default=0)
    comment_count = models.IntegerField(default=0)
    media_count = models.IntegerField(default=0)

    class Meta:
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['source', 'source_id'], name='post_source_lookup'),
            models.Index(fields=['visibility', '-created_at'], name='post_public_feed'),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=['source', 'source_id'],
                condition=~models.Q(source_id=''),
                name='unique_source_post',
            ),
        ]
```

### PostMedia

```python
class MediaType(models.IntegerChoices):
    IMAGE = 1, "Image"
    VIDEO = 2, "Video"
    GIF = 3, "GIF"
    LINK_EMBED = 4, "Link Embed"

class PostMedia(models.Model):
    post = models.ForeignKey(Post, on_delete=models.CASCADE, related_name='media')
    media_type = models.IntegerField(choices=MediaType.choices)
    file = models.FileField(upload_to='posts/%Y/%m/', blank=True)  # local file
    original_url = models.URLField(max_length=1000, blank=True)     # original platform URL
    caption = models.TextField(blank=True)
    position = models.IntegerField(default=0)  # ordering within post

    # Image-specific
    width = models.IntegerField(null=True, blank=True)
    height = models.IntegerField(null=True, blank=True)

    # Link embed specific
    embed_title = models.CharField(max_length=500, blank=True)
    embed_url = models.URLField(max_length=1000, blank=True)

    class Meta:
        ordering = ['position']
```

### PostComment

```python
class PostComment(models.Model):
    post = models.ForeignKey(Post, on_delete=models.CASCADE, related_name='comments')
    author_name = models.CharField(max_length=300)
    author_url = models.URLField(max_length=1000, blank=True)
    text = models.TextField()
    created_at = models.DateTimeField(null=True, blank=True)
    source_id = models.CharField(max_length=500, blank=True)

    class Meta:
        ordering = ['created_at']
```

### PostReaction

```python
class ReactionType(models.IntegerChoices):
    PLUS_ONE = 1, "+1"
    LIKE = 2, "Like"
    RETWEET = 3, "Retweet"
    OTHER = 10, "Other"

class PostReaction(models.Model):
    post = models.ForeignKey(Post, on_delete=models.CASCADE, related_name='reactions')
    reaction_type = models.IntegerField(choices=ReactionType.choices)
    user_name = models.CharField(max_length=300)
    user_url = models.URLField(max_length=1000, blank=True)

    class Meta:
        indexes = [models.Index(fields=['post'], name='reaction_post_idx')]
```

### Tag

```python
class Tag(models.Model):
    name = models.CharField(max_length=200, unique=True)
    slug = models.SlugField(max_length=200, unique=True)

class PostTag(models.Model):
    post = models.ForeignKey(Post, on_delete=models.CASCADE, related_name='post_tags')
    tag = models.ForeignKey(Tag, on_delete=models.CASCADE, related_name='post_tags')

    class Meta:
        unique_together = ['post', 'tag']
```

## URL Scheme

```
/                           ← post list (paginated, public only)
/post/<slug>/               ← single post
/archive/                   ← full archive grouped by year
/archive/<year>/            ← posts from year
/tag/<slug>/                ← posts with tag
/source/<name>/             ← posts from a source (google_plus, facebook, twitter, blog)
/search/                    ← full-text search results (?q= query param)
/word-cloud/                ← word cloud with clickable terms → /search/?q=<word>
/new/                       ← create post (auth required)
/post/<slug>/edit/          ← edit post (auth required)
/api/upload-image/          ← image upload endpoint for editor (auth required)
```

## Views

### Public views (no auth)

- **PostListView**: paginated list (20/page), filters by visibility=PUBLIC, ordered by `-created_at`. Shows title (or first ~200 chars), date, source icon, thumbnail.
- **PostDetailView**: full post with media gallery, comments, reactions. Renders `content_html` directly (imported) or renders `content_markdown` via markdown lib (new posts).
- **ArchiveView**: posts grouped by year/month.
- **TagView**: filtered post list.
- **SourceView**: filtered by source network.

### Authenticated views (owner only)

- **PostCreateView**: Markdown editor with live preview, image upload, tag picker, visibility selector.
- **PostUpdateView**: same editor, pre-populated.
- **ImageUploadView**: AJAX endpoint, saves to media dir, returns URL for embedding.

## Templates

Minimal, clean design. Mobile-friendly. Dark/light mode via CSS prefers-color-scheme.

**Post rendering logic:**
- Imported posts (`source != BLOG`): render `content_html` as-is (preserves original formatting)
- New posts (`source == BLOG`): render `content_markdown` through Python-Markdown with extensions (extra, toc, fenced_code, codehilite)

**Media display:**
- Single image: full-width with lightbox on click
- Multiple images: grid/masonry layout with lightbox
- Videos: HTML5 `<video>` tag
- Link embeds: card with title + thumbnail

## Slug Generation

For imported posts (often no title):
```python
def generate_slug(post):
    date_prefix = post.created_at.strftime('%Y-%m-%d')
    if post.title:
        return slugify(f"{date_prefix}-{post.title[:80]}")
    # Use first few words of content
    words = post.content_text[:60].split()
    text_part = '-'.join(words[:8])
    return slugify(f"{date_prefix}-{text_part}")
```

Collision: append `-2`, `-3`, etc.

## Search

Simple approach first: PostgreSQL full-text search on `content_text` field via `SearchVector`. No Elasticsearch needed at ~3–5k posts.

```python
Post.objects.annotate(
    search=SearchVector('content_text', 'title', config='russian')  # or 'english' — detect per post
).filter(search=query)
```

**SearchView** (`/search/?q=<query>`): returns a paginated list of matching public posts, ordered by relevance rank.

### Word Cloud

A word cloud visualises the most-frequent meaningful words across all public posts.

**WordCloudView** (`/word-cloud/`):
- Counts word frequencies in `content_text` of all public posts; strips stop words (Russian + English).
- Returns word–frequency pairs rendered client-side (e.g. `wordcloud2.js` or CSS-scaled `<span>` tags).
- Clicking a word (or phrase) in the cloud navigates to `/search/?q=<word>` and shows the list of top posts containing that term.

No separate view is needed for word-click results — the standard `SearchView` handles it.

## RSS Feed

Django syndication framework. Feed at `/feed/` with latest 20 public posts.

## Admin

Register all models in Django admin for bulk operations:
- Post admin with list filters (source, visibility, date range), search on content_text
- Inline PostMedia on Post admin
- Bulk actions: change visibility, add tags

## Performance Considerations

- ~5k total posts — no pagination performance concerns
- Media served directly by Nginx (bypass Django)
- Template fragment caching for post list if needed (unlikely at this scale)
- PostgreSQL indexes on (visibility, created_at) for feed queries
