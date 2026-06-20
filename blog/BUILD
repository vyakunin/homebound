load("@rules_python//python:defs.bzl", "py_binary", "py_library")
load("@homebound_pip//:requirements.bzl", "requirement")

py_library(
    name = "models",
    srcs = ["models.py"],
    visibility = ["//visibility:public"],
    deps = [
        requirement("Django"),
        requirement("Markdown"),
        requirement("pgvector"),
    ],
)

py_library(
    name = "embeddings",
    srcs = ["embeddings.py"],
    visibility = ["//visibility:public"],
    deps = [
        requirement("httpx"),
    ],
)

py_library(
    name = "bot_throttle",
    srcs = ["bot_throttle.py"],
    visibility = ["//visibility:public"],
    deps = [
        ":models",
        requirement("Django"),
    ],
)

py_library(
    name = "bot_retrieval",
    srcs = ["bot_retrieval.py"],
    visibility = ["//visibility:public"],
    deps = [
        ":embeddings",
        ":models",
        requirement("Django"),
        requirement("pgvector"),
    ],
)

py_library(
    name = "bot",
    srcs = ["bot.py"],
    visibility = ["//visibility:public"],
    deps = [
        ":bot_retrieval",
        requirement("anthropic"),
    ],
)

# ── SFT dataset builder modules (stdlib + internal blog.* only) ──────────
py_library(
    name = "sft_common",
    srcs = ["sft_common.py"],
    visibility = ["//visibility:public"],
    deps = [":models"],
)

py_library(
    name = "sft_qgen",
    srcs = ["sft_qgen.py"],
    visibility = ["//visibility:public"],
)

py_library(
    name = "sft_resilience",
    srcs = ["sft_resilience.py"],
    visibility = ["//visibility:public"],
)

py_library(
    name = "sft_abstention",
    srcs = ["sft_abstention.py"],
    visibility = ["//visibility:public"],
    deps = [
        ":bot",
        ":bot_retrieval",
        ":models",
        ":sft_common",
        ":sft_qgen",
    ],
)

py_library(
    name = "sft_grounded",
    srcs = ["sft_grounded.py"],
    visibility = ["//visibility:public"],
    deps = [
        ":bot",
        ":bot_retrieval",
        ":models",
        ":sft_common",
        ":sft_qgen",
    ],
)

py_library(
    name = "sft_contrastive",
    srcs = ["sft_contrastive.py"],
    visibility = ["//visibility:public"],
    deps = [
        ":bot",
        ":bot_retrieval",
        ":models",
        ":sft_abstention",
        ":sft_common",
        ":sft_grounded",
        ":sft_qgen",
    ],
)

py_library(
    name = "sft_transfer",
    srcs = ["sft_transfer.py"],
    visibility = ["//visibility:public"],
    deps = [
        ":bot",
        ":bot_retrieval",
        ":models",
        ":sft_abstention",
        ":sft_common",
        ":sft_grounded",
        ":sft_qgen",
    ],
)

# `bazel run //blog:generate_embeddings -- [--limit N] [--rehash] ...`
# Delegates to the Django management command of the same name. Needs the
# full runserver-style dep set because Django's startup loads every entry
# in INSTALLED_APPS — allauth in particular pulls in requests/PyJWT.
py_binary(
    name = "generate_embeddings",
    srcs = ["embed_runner.py"],
    main = "embed_runner.py",
    visibility = ["//visibility:public"],
    env = {"DJANGO_SETTINGS_MODULE": "django_config.settings"},
    data = [":migrations"],
    deps = [
        ":adapters",
        ":admin",
        ":apps",
        ":embeddings",
        ":forms",
        ":models",
        ":views",
        "//blog/management/commands:generate_embeddings",
        "//blog/templatetags:blog_tags",
        "//blog/templatetags:init",
        "//django_config:settings",
        "//django_config:urls",
        requirement("Django"),
        requirement("psycopg2-binary"),
        requirement("django-allauth"),
        requirement("requests"),
        requirement("PyJWT"),
        requirement("cryptography"),
        requirement("Pillow"),
    ],
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

py_library(
    name = "forms",
    srcs = ["forms.py"],
    visibility = ["//visibility:public"],
    deps = [
        ":models",
        requirement("Django"),
        requirement("Markdown"),
    ],
)

py_library(
    name = "stop_words",
    srcs = ["stop_words.py"],
    visibility = ["//visibility:public"],
)

py_library(
    name = "views",
    srcs = ["views.py"],
    visibility = ["//visibility:public"],
    deps = [
        ":bot",
        ":bot_throttle",
        ":embeddings",
        ":forms",
        ":models",
        ":stop_words",
        requirement("Django"),
        requirement("Markdown"),
        requirement("pgvector"),
        requirement("pymorphy3"),
        requirement("nltk"),
    ],
)

py_library(
    name = "feeds",
    srcs = ["feeds.py"],
    visibility = ["//visibility:public"],
    deps = [
        ":models",
        requirement("Django"),
    ],
)

py_library(
    name = "sitemaps",
    srcs = ["sitemaps.py"],
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
        ":feeds",
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

filegroup(
    name = "migrations",
    srcs = ["//blog/migrations:migrations"],
    visibility = ["//visibility:public"],
)

py_library(
    name = "templatetags",
    deps = ["//blog/templatetags:blog_tags"],
    visibility = ["//visibility:public"],
)
