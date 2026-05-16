# Auth & Editor — Design

> Part of [Phase 3](HIGH_LEVEL_DESIGN.md#phase-3--auth--posting-ui) in the [high-level plan](HIGH_LEVEL_DESIGN.md).

## Authentication

### Requirements

- Single-user blog: only one Google account (the owner) can log in
- No registration, no password-based auth
- All write operations require auth; all reads are public (for public posts)

### Implementation: django-allauth + Google OAuth 2.0

```
User clicks "Sign in" → Google OAuth consent → callback → django-allauth creates/matches Django User → session
```

**Setup:**
1. Google Cloud Console: create OAuth 2.0 client ID (Web application)
   - Authorized redirect URI: `https://vyakunin.org/accounts/google/login/callback/`
2. django-allauth configuration:

```python
# settings.py
INSTALLED_APPS = [
    ...
    'allauth',
    'allauth.account',
    'allauth.socialaccount',
    'allauth.socialaccount.providers.google',
]

AUTHENTICATION_BACKENDS = [
    'allauth.account.auth_backends.AuthenticationBackend',
]

SOCIALACCOUNT_PROVIDERS = {
    'google': {
        'SCOPE': ['profile', 'email'],
        'AUTH_PARAMS': {'access_type': 'online'},
        'APP': {
            'client_id': env('GOOGLE_OAUTH_CLIENT_ID'),
            'secret': env('GOOGLE_OAUTH_SECRET'),
        },
    },
}

# Single-user restriction
ALLOWED_LOGIN_EMAIL = env('BLOG_OWNER_EMAIL')  # e.g., "vyakunin@gmail.com"

ACCOUNT_ADAPTER = 'blog.adapters.SingleUserAccountAdapter'
SOCIALACCOUNT_ADAPTER = 'blog.adapters.SingleUserSocialAdapter'
```

**Adapter (restricts login to owner):**
```python
# blog/adapters.py
from allauth.account.adapter import DefaultAccountAdapter
from allauth.socialaccount.adapter import DefaultSocialAccountAdapter
from django.conf import settings

class SingleUserAccountAdapter(DefaultAccountAdapter):
    def is_open_for_signup(self, request):
        return False  # no registration

class SingleUserSocialAdapter(DefaultSocialAccountAdapter):
    def pre_social_login(self, request, sociallogin):
        email = sociallogin.account.extra_data.get('email', '')
        if email != settings.ALLOWED_LOGIN_EMAIL:
            raise PermissionDenied("Not authorized")
```

### Auth UI

- Login: small "Sign in" link in footer (not prominent — it's a personal blog, not a platform)
- Logged in: floating toolbar at top with "New Post", "Edit", "Sign out"
- Session timeout: default Django session (2 weeks)

## Post Editor

### Requirements

- Write in Markdown with live preview
- Drag-and-drop image upload (inserted as `![](url)` into markdown)
- Set title, tags, visibility, date override
- Mobile-usable (responsive)

### Frontend: EasyMDE

[EasyMDE](https://github.com/Ionaru/easy-markdown-editor) — mature, maintained Markdown editor with:
- Toolbar (bold, italic, heading, link, image, code, quote, list)
- Side-by-side live preview
- Image drag-and-drop (with custom upload handler)
- Spell check
- Fullscreen mode

**No npm/build step needed** — load from CDN or vendor the JS file.

### Editor Page Layout

```
┌─────────────────────────────────────────────┐
│ Title: [________________________________]    │
│                                              │
│ Tags:  [tag1] [tag2] [+ add]                │
│                                              │
│ Visibility: (●) Public  ( ) Unlisted         │
│                                              │
│ Date: [2026-03-29]  (default: now)           │
│                                              │
│ ┌─────────────────────────────────────────┐  │
│ │ B I H | 🔗 📷 | code quote | preview   │  │
│ ├─────────────────────────────────────────┤  │
│ │                                         │  │
│ │  Markdown editor area                   │  │
│ │  (EasyMDE)                              │  │
│ │                                         │  │
│ │  Drop images here                       │  │
│ │                                         │  │
│ └─────────────────────────────────────────┘  │
│                                              │
│ [Save Draft]  [Publish]                      │
└─────────────────────────────────────────────┘
```

### Image Upload Flow

```
User drops image onto editor
  → JS: FormData POST to /api/upload-image/
  → Django view: validate (auth, file type, size < 10MB)
  → Save to MEDIA_ROOT/posts/YYYY/MM/filename.jpg
  → Return JSON: {"url": "/media/posts/2026/03/filename.jpg"}
  → JS: insert ![](url) at cursor position
```

**View:**
```python
@require_POST
@login_required
def upload_image(request):
    file = request.FILES['image']
    # Validate: size, content type
    path = default_storage.save(
        f"posts/{now().strftime('%Y/%m')}/{file.name}",
        file,
    )
    return JsonResponse({'url': default_storage.url(path)})
```

### Markdown Rendering

New posts stored as Markdown in `content_markdown`. Rendered to `content_html` on save:

```python
import markdown

MARKDOWN_EXTENSIONS = [
    'extra',           # tables, fenced code, footnotes
    'toc',             # table of contents
    'codehilite',      # syntax highlighting
    'smarty',          # smart quotes
]

def render_markdown(md_text):
    return markdown.markdown(md_text, extensions=MARKDOWN_EXTENSIONS)
```

`content_html` is cached (re-rendered on edit). `content_text` is a plain-text extraction for search.

### Edit Existing Posts

- Imported posts (G+, FB, Twitter): show `content_html` in a textarea for minor edits. No Markdown conversion (would lose formatting). Primarily use for fixing broken links or adding context.
- Blog posts: full Markdown editor with the source.

## Dependencies

| Package | Version | Purpose |
|---------|---------|---------|
| django-allauth | >=0.60 | Google OAuth |
| markdown | >=3.5 | Markdown → HTML |
| Pillow | >=10.0 | Image validation/thumbnails |

Frontend (CDN, no build step):
- EasyMDE CSS + JS
- (Optional) Tagify for tag input
