load("@rules_python//python:defs.bzl", "py_library")
load("@homebound_pip//:requirements.bzl", "requirement")

py_library(
    name = "import_posts",
    srcs = ["import_posts.py"],
    visibility = ["//visibility:public"],
    deps = [
        "//blog:models",
        "//extractors:posts_io",
        "//proto:post_record",
        requirement("Django"),
    ],
)

py_library(
    name = "fb_reimport",
    srcs = ["fb_reimport.py"],
    visibility = ["//visibility:public"],
    deps = [
        "//blog:models",
        "//extractors:activity_log",
        ":import_posts",
        requirement("Django"),
    ],
)

py_library(
    name = "x_reimport",
    srcs = ["x_reimport.py"],
    visibility = ["//visibility:public"],
    deps = [
        "//blog:models",
        "//extractors:twitter_log",
        ":import_posts",
        requirement("Django"),
    ],
)

py_library(
    name = "wayback_twitter_import",
    srcs = ["wayback_twitter_import.py"],
    visibility = ["//visibility:public"],
    deps = [
        "//blog:models",
        "//extractors:wayback_twitter_log",
        ":import_posts",
        requirement("Django"),
    ],
)
