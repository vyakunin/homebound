load("@rules_python//python:defs.bzl", "py_library")
load("@homebound_pip//:requirements.bzl", "requirement")

py_library(
    name = "settings",
    srcs = ["settings.py"],
    visibility = ["//visibility:public"],
    deps = [
        ":context_processors",
        requirement("Django"),
    ],
)

py_library(
    name = "context_processors",
    srcs = ["context_processors.py"],
    visibility = ["//visibility:public"],
    deps = [requirement("Django")],
)

py_library(
    name = "urls",
    srcs = ["urls.py"],
    visibility = ["//visibility:public"],
    deps = [
        "//blog:sitemaps",
        "//blog:urls",
        requirement("Django"),
        requirement("django-allauth"),
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
