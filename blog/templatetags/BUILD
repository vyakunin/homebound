load("@rules_python//python:defs.bzl", "py_library")
load("@homebound_pip//:requirements.bzl", "requirement")

py_library(
    name = "blog_tags",
    srcs = ["blog_tags.py"],
    visibility = ["//visibility:public"],
    deps = [
        "//blog:models",
        "//extractors:facebook_api",
        requirement("Django"),
    ],
)

py_library(
    name = "init",
    srcs = glob(["__init__.py"]),
    visibility = ["//visibility:public"],
    deps = [requirement("Django")],
)
