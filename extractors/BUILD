load("@rules_python//python:defs.bzl", "py_binary", "py_library")
load("@homebound_pip//:requirements.bzl", "requirement")

py_library(
    name = "base",
    srcs = ["base.py"],
    visibility = ["//visibility:public"],
)

py_library(
    name = "posts_io",
    srcs = ["posts_io.py"],
    visibility = ["//visibility:public"],
    deps = [
        "//proto:post_record",
    ],
)

py_library(
    name = "google_plus",
    srcs = ["google_plus.py"],
    visibility = ["//visibility:public"],
    deps = [
        ":posts_io",
        "//proto:post_record",
        requirement("beautifulsoup4"),
        requirement("lxml"),
    ],
)

py_binary(
    name = "google_plus_bin",
    srcs = ["google_plus.py"],
    main = "google_plus.py",
    visibility = ["//visibility:public"],
    deps = [
        ":posts_io",
        "//proto:post_record",
        requirement("beautifulsoup4"),
        requirement("lxml"),
    ],
)

py_library(
    name = "facebook",
    srcs = ["facebook.py"],
    visibility = ["//visibility:public"],
    deps = [
        ":base",
        ":posts_io",
        "//proto:post_record",
    ],
)

py_binary(
    name = "facebook_bin",
    srcs = ["facebook.py"],
    main = "facebook.py",
    visibility = ["//visibility:public"],
    deps = [
        ":base",
        ":posts_io",
        "//proto:post_record",
    ],
)

py_library(
    name = "facebook_api",
    srcs = ["facebook_api.py"],
    visibility = ["//visibility:public"],
    deps = [
        ":base",
        ":posts_io",
        "//proto:post_record",
        requirement("requests"),
    ],
)

py_binary(
    name = "facebook_api_bin",
    srcs = ["facebook_api.py"],
    main = "facebook_api.py",
    visibility = ["//visibility:public"],
    deps = [
        ":base",
        ":posts_io",
        "//proto:post_record",
        requirement("requests"),
    ],
)

py_library(
    name = "activity_log",
    srcs = ["activity_log.py"],
    visibility = ["//visibility:public"],
    deps = [
        ":base",
        ":posts_io",
        "//proto:post_record",
    ],
)

py_binary(
    name = "activity_log_bin",
    srcs = ["activity_log.py"],
    main = "activity_log.py",
    visibility = ["//visibility:public"],
    deps = [
        ":base",
        ":posts_io",
        "//proto:post_record",
    ],
)

py_library(
    name = "twitter_log",
    srcs = ["twitter_log.py"],
    visibility = ["//visibility:public"],
    deps = [
        ":base",
        ":posts_io",
        "//proto:post_record",
    ],
)

py_binary(
    name = "twitter_log_bin",
    srcs = ["twitter_log.py"],
    main = "twitter_log.py",
    visibility = ["//visibility:public"],
    deps = [
        ":base",
        ":posts_io",
        "//proto:post_record",
    ],
)

py_library(
    name = "wayback_twitter_log",
    srcs = ["wayback_twitter_log.py"],
    visibility = ["//visibility:public"],
    deps = [
        ":posts_io",
        "//proto:post_record",
        requirement("beautifulsoup4"),
        requirement("lxml"),
        requirement("wayback"),
    ],
)

py_binary(
    name = "wayback_twitter_log_bin",
    srcs = ["wayback_twitter_log.py"],
    main = "wayback_twitter_log.py",
    visibility = ["//visibility:public"],
    deps = [
        ":posts_io",
        "//proto:post_record",
        requirement("beautifulsoup4"),
        requirement("lxml"),
        requirement("wayback"),
    ],
)
