import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent

# Load .env if present (local dev). Production injects env vars via Docker/systemd.
_env_file = BASE_DIR / '.env'
if _env_file.exists():
    for _line in _env_file.read_text().splitlines():
        _line = _line.strip()
        if _line and not _line.startswith('#') and '=' in _line:
            _key, _, _val = _line.partition('=')
            os.environ.setdefault(_key.strip(), _val.strip())

SECRET_KEY = os.environ.get('SECRET_KEY', 'dev-secret-key-do-not-use-in-production')
DEBUG = os.environ.get('DEBUG', 'True') == 'True'
ALLOWED_HOSTS = os.environ.get('ALLOWED_HOSTS', 'localhost,127.0.0.1').split(',')

INSTALLED_APPS = [
    'django.contrib.admin',
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.sessions',
    'django.contrib.messages',
    'django.contrib.staticfiles',
    'django.contrib.sites',
    'django.contrib.sitemaps',
    'django.contrib.postgres',
    'allauth',
    'allauth.account',
    'allauth.socialaccount',
    'allauth.socialaccount.providers.google',
    'blog',
]

MIDDLEWARE = [
    'django.middleware.security.SecurityMiddleware',
    'django.contrib.sessions.middleware.SessionMiddleware',
    'django.middleware.common.CommonMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'django.contrib.messages.middleware.MessageMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
    'allauth.account.middleware.AccountMiddleware',
]

ROOT_URLCONF = 'django_config.urls'

TEMPLATES = [
    {
        'BACKEND': 'django.template.backends.django.DjangoTemplates',
        'DIRS': [BASE_DIR / 'templates'],
        'APP_DIRS': True,
        'OPTIONS': {
            'context_processors': [
                'django.template.context_processors.debug',
                'django.template.context_processors.request',
                'django.contrib.auth.context_processors.auth',
                'django.contrib.messages.context_processors.messages',
                'django_config.context_processors.analytics',
                'django_config.context_processors.bot_settings',
            ],
        },
    },
]

WSGI_APPLICATION = 'django_config.wsgi.application'

# Use SQLite in-memory when running tests to avoid needing PostgreSQL in CI
if os.environ.get('RUNNING_TESTS') == '1':
    DATABASES = {
        'default': {
            'ENGINE': 'django.db.backends.sqlite3',
            'NAME': ':memory:',
        }
    }
else:
    DATABASES = {
        'default': {
            'ENGINE': 'django.db.backends.postgresql',
            'NAME': os.environ.get('DB_NAME', 'homebound'),
            'USER': os.environ.get('DB_USER', 'postgres'),
            'PASSWORD': os.environ.get('DB_PASSWORD', ''),
            'HOST': os.environ.get('DB_HOST', 'localhost'),
            'PORT': os.environ.get('DB_PORT', '5432'),
        }
    }

AUTH_PASSWORD_VALIDATORS = [
    {'NAME': 'django.contrib.auth.password_validation.UserAttributeSimilarityValidator'},
    {'NAME': 'django.contrib.auth.password_validation.MinimumLengthValidator'},
    {'NAME': 'django.contrib.auth.password_validation.CommonPasswordValidator'},
    {'NAME': 'django.contrib.auth.password_validation.NumericPasswordValidator'},
]

LANGUAGE_CODE = 'en-us'
TIME_ZONE = 'UTC'
USE_I18N = True
USE_TZ = True

STATIC_URL = '/static/'
STATIC_ROOT = BASE_DIR / 'staticfiles'
STATICFILES_DIRS = [BASE_DIR / 'static']

# Content-hashed static filenames so deploys auto-invalidate caches. Without
# this, CSS/JS edits don't reach users until their browser's max-age expires
# (May 18 2026 incident: nginx served stale style.css for hours after deploy
# because Cloudflare + browsers cached the un-hashed URL with
# `immutable, max-age=2592000`). Manifest storage renames files at
# collectstatic time to e.g. ``style.abc123.css``; the {% static %} tag
# resolves to the hashed URL. In tests/DEBUG the in-memory manifest may not
# be ready, so fall back to non-hashed storage there.
if not os.environ.get('RUNNING_TESTS') == '1':
    STORAGES = {
        'default': {'BACKEND': 'django.core.files.storage.FileSystemStorage'},
        'staticfiles': {
            'BACKEND': 'django.contrib.staticfiles.storage.ManifestStaticFilesStorage',
        },
    }

MEDIA_URL = '/media/'
MEDIA_ROOT = os.environ.get('MEDIA_ROOT', str(BASE_DIR / 'media'))

AUTHENTICATION_BACKENDS = [
    'django.contrib.auth.backends.ModelBackend',
    'allauth.account.auth_backends.AuthenticationBackend',
]

SITE_ID = 1

SOCIALACCOUNT_PROVIDERS = {
    'google': {
        'SCOPE': ['profile', 'email'],
        'AUTH_PARAMS': {'access_type': 'online'},
        'APP': {
            'client_id': os.environ.get('GOOGLE_OAUTH_CLIENT_ID', ''),
            'secret': os.environ.get('GOOGLE_OAUTH_SECRET', ''),
        },
    },
}

ALLOWED_LOGIN_EMAIL = os.environ.get('BLOG_OWNER_EMAIL', '')
ACCOUNT_ADAPTER = 'blog.adapters.SingleUserAccountAdapter'
SOCIALACCOUNT_ADAPTER = 'blog.adapters.SingleUserSocialAdapter'

LOGIN_URL = '/accounts/google/login/'
LOGIN_REDIRECT_URL = '/'
ACCOUNT_LOGOUT_REDIRECT_URL = '/'

DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'

POSTS_PER_PAGE = 20

# Public bot widget — gated behind ?bot=1 until the user has reviewed
# sample transcripts (see no_prod_experimentation.mdc). Flip to True
# only after sign-off lands; ./bot/ then becomes publicly accessible
# and a small "Ask Vladimir" footer link appears.
BOT_PUBLIC = os.environ.get('BOT_PUBLIC', 'False') == 'True'

# Rate limits (Django-layer; nginx is the hard limit). DAILY windows —
# the bot is a personal-blog widget, not a production service; daily
# caps map cleanly to the "toy LLM budget" framing visitors see when
# the cap is hit.
#
# Calibrated for ~$50/mo at Haiku 4.5 pricing, measured against the
# v3 sample dump with the padded ~7200-token persona that lifts us
# above Haiku's 4096-token prompt-cache threshold. Steady-state per-Q
# cost is $0.0036 (vs $0.006 unpadded), so 480/day × 30 × $0.0036 ≈
# $51/mo. The response cache compounds savings further when popular
# questions repeat.
BOT_PER_IP_RATE_LIMIT_PER_DAY = int(os.environ.get('BOT_PER_IP_RATE_LIMIT_PER_DAY', '30'))
BOT_SITE_RATE_LIMIT_PER_DAY = int(os.environ.get('BOT_SITE_RATE_LIMIT_PER_DAY', '480'))

# Tiered model selection. Default = Haiku 4.5 for cost; allow one
# Sonnet 4.6 call per IP per day, only if the question is non-trivial
# (>= BOT_SONNET_MIN_WORDS). Trivial questions ("чей крым?") stay on
# Haiku regardless — Sonnet doesn't add much for one-liners.
BOT_DEFAULT_MODEL = os.environ.get('BOT_DEFAULT_MODEL', 'claude-haiku-4-5')
BOT_PREMIUM_MODEL = os.environ.get('BOT_PREMIUM_MODEL', 'claude-sonnet-4-6')

# Dual-model language routing (added 2026-05-19). Python-side language
# classifier picks RU or EN persona+model per request. ``BOT_MODEL_EN``
# falls back to ``BOT_DEFAULT_MODEL`` if unset. ``BOT_MODEL_RU``
# defaults to DeepSeek-chat (V3) on OpenRouter — best Russian voice
# we tested on OpenRouter's catalog, materially cleaner register than
# Qwen 2.5-72B at comparable cost ($0.32 in / $0.89 out per 1M).
# Slash in the model name routes to the OpenRouter adapter; bare
# model name routes to Anthropic.
BOT_MODEL_RU = os.environ.get('BOT_MODEL_RU', 'deepseek/deepseek-chat')
BOT_MODEL_EN = os.environ.get('BOT_MODEL_EN', BOT_DEFAULT_MODEL)
BOT_SONNET_PER_IP_PER_DAY = int(os.environ.get('BOT_SONNET_PER_IP_PER_DAY', '1'))
BOT_SONNET_MIN_WORDS = int(os.environ.get('BOT_SONNET_MIN_WORDS', '6'))

# Public contact handles for the cap-exhausted handoff message and the
# widget's footer DM buttons. Read from env so they can be rotated /
# disabled without a code change.
BOT_CONTACT_WHATSAPP_URL = os.environ.get('BOT_CONTACT_WHATSAPP_URL', 'https://wa.me/16509655983')
BOT_CONTACT_TELEGRAM_URL = os.environ.get('BOT_CONTACT_TELEGRAM_URL', 'https://t.me/vyakunin')

# Optional analytics snippet rendered just before </head> on every page.
# Set to the full <script>…</script> for GoatCounter / Plausible / etc.
ANALYTICS_SCRIPT = os.environ.get('ANALYTICS_SCRIPT', '')

# Proxy / HTTPS settings (Nginx terminates SSL, forwards via X-Forwarded-Proto)
SECURE_PROXY_SSL_HEADER = ('HTTP_X_FORWARDED_PROTO', 'https')
CSRF_TRUSTED_ORIGINS = [
    f'https://{host}' for host in ALLOWED_HOSTS
    if host not in ('localhost', '127.0.0.1', '')
]
