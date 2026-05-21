load("@rules_python//python:defs.bzl", "py_binary")
load("@rules_python//python/uv:lock.bzl", "lock")
load("@homebound_pip//:requirements.bzl", "requirement")

# Regenerate requirements_lock.txt from pyproject.toml using Bazel-managed uv:
#   bazel run //:requirements.update
# (uv is downloaded automatically — no system install needed)
lock(
    name = "requirements",
    srcs = ["pyproject.toml"],
    out = "requirements_lock.txt",
    generate_hashes = False,
)

# Dev server
py_binary(
    name = "runserver",
    srcs = ["manage.py"],
    main = "manage.py",
    args = ["runserver", "8089", "--noreload"],
    data = [
        "//blog:migrations",
        "//:templates",
        "//:static_files",
    ],
    env = {"DJANGO_SETTINGS_MODULE": "django_config.settings"},
    deps = [
        "//django_config:settings",
        "//django_config:urls",
        "//django_config:wsgi",
        "//blog:adapters",
        "//blog:apps",
        "//blog:forms",
        "//blog:models",
        "//blog:views",
        "//blog:admin",
        "//blog/templatetags:blog_tags",
        "//blog/templatetags:init",
        requirement("Django"),
        requirement("psycopg2-binary"),
        requirement("django-allauth"),
        # allauth.socialaccount.providers.google (in INSTALLED_APPS) imports these at startup
        requirement("requests"),
        requirement("PyJWT"),
        requirement("cryptography"),
        # PostMedia.og_image_file is an ImageField — Django system check fails without Pillow
        requirement("Pillow"),
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
        # Django's startup checks load ROOT_URLCONF, which transitively
        # imports the entire view layer — migrate needs the runserver
        # superset of deps for `manage.py` to even start.
        "//django_config:settings",
        "//django_config:urls",
        "//blog:adapters",
        "//blog:admin",
        "//blog:apps",
        "//blog:forms",
        "//blog:models",
        "//blog:views",
        "//blog/templatetags:blog_tags",
        "//blog/templatetags:init",
        requirement("Django"),
        requirement("psycopg2-binary"),
        requirement("django-allauth"),
        requirement("requests"),
        requirement("PyJWT"),
        requirement("cryptography"),
        requirement("Pillow"),
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
        requirement("psycopg2-binary"),
    ],
)

# Proto binary importer
py_binary(
    name = "import_posts",
    srcs = ["manage.py"],
    main = "manage.py",
    args = ["import_posts"],
    env = {"DJANGO_SETTINGS_MODULE": "django_config.settings"},
    deps = [
        "//django_config:settings",
        "//django_config:urls",
        "//blog:adapters",
        "//blog:apps",
        "//blog:forms",
        "//blog:models",
        "//blog:sitemaps",
        "//blog:views",
        "//blog:admin",
        "//blog/management/commands:import_posts",
        requirement("Django"),
        requirement("psycopg2-binary"),
        requirement("Pillow"),
        requirement("django-allauth"),
        # allauth.socialaccount.providers.google (in INSTALLED_APPS) imports these at startup
        requirement("requests"),
        requirement("PyJWT"),
        requirement("cryptography"),
    ],
)

# Wipe all Facebook posts and reimport from latest (or given) activity log ZIP.
py_binary(
    name = "fb_reimport",
    srcs = ["manage.py"],
    main = "manage.py",
    args = ["fb_reimport"],
    env = {"DJANGO_SETTINGS_MODULE": "django_config.settings"},
    deps = [
        "//django_config:settings",
        "//django_config:urls",
        "//blog:adapters",
        "//blog:apps",
        "//blog:forms",
        "//blog:models",
        "//blog:views",
        "//blog:admin",
        "//blog/management/commands:fb_reimport",
        requirement("Django"),
        requirement("psycopg2-binary"),
        requirement("Pillow"),
        requirement("django-allauth"),
        requirement("requests"),
        requirement("PyJWT"),
        requirement("cryptography"),
    ],
)

# Wipe all Twitter/X posts and reimport from latest (or given) export ZIP.
py_binary(
    name = "x_reimport",
    srcs = ["manage.py"],
    main = "manage.py",
    args = ["x_reimport"],
    env = {"DJANGO_SETTINGS_MODULE": "django_config.settings"},
    deps = [
        "//django_config:settings",
        "//django_config:urls",
        "//blog:adapters",
        "//blog:apps",
        "//blog:forms",
        "//blog:models",
        "//blog:views",
        "//blog:admin",
        "//blog/management/commands:x_reimport",
        requirement("Django"),
        requirement("psycopg2-binary"),
        requirement("django-allauth"),
        requirement("requests"),
        requirement("PyJWT"),
        requirement("cryptography"),
    ],
)

# Import historical tweets from Wayback Machine for a given Twitter handle.
py_binary(
    name = "wayback_twitter_import",
    srcs = ["manage.py"],
    main = "manage.py",
    args = ["wayback_twitter_import"],
    env = {"DJANGO_SETTINGS_MODULE": "django_config.settings"},
    deps = [
        "//django_config:settings",
        "//django_config:urls",
        "//blog:adapters",
        "//blog:apps",
        "//blog:forms",
        "//blog:models",
        "//blog:views",
        "//blog:admin",
        "//blog:sitemaps",
        "//blog:feeds",
        "//blog/management/commands:wayback_twitter_import",
        requirement("Django"),
        requirement("psycopg2-binary"),
        requirement("django-allauth"),
        requirement("requests"),
        requirement("PyJWT"),
        requirement("cryptography"),
        requirement("beautifulsoup4"),
        requirement("lxml"),
        requirement("Pillow"),
    ],
)

filegroup(
    name = "templates",
    srcs = glob(["templates/**/*.html"]),
    visibility = ["//visibility:public"],
)

filegroup(
    name = "static_files",
    srcs = glob(["static/**"]),
    visibility = ["//visibility:public"],
)
