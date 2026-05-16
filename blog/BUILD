load("@rules_python//python:defs.bzl", "py_library")
load("@homebound_pip//:requirements.bzl", "requirement")

py_library(
    name = "models",
    srcs = ["models.py"],
    visibility = ["//visibility:public"],
    deps = [
        requirement("Django"),
        requirement("Markdown"),
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
        ":forms",
        ":models",
        ":stop_words",
        requirement("Django"),
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
