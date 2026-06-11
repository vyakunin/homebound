load("@rules_python//python:defs.bzl", "py_binary")
load("@homebound_pip//:requirements.bzl", "requirement")

py_binary(
    name = "exchange_fb_long_lived_token",
    srcs = ["exchange_fb_long_lived_token.py"],
    main = "exchange_fb_long_lived_token.py",
    python_version = "PY3",
    deps = [],
    visibility = ["//visibility:public"],
)

py_binary(
    name = "sync_extension_shared",
    srcs = ["sync_extension_shared.py"],
    main = "sync_extension_shared.py",
    python_version = "PY3",
    deps = [],
    visibility = ["//visibility:public"],
)

py_binary(
    name = "protoc_betterproto",
    srcs = ["protoc_betterproto.py"],
    main = "protoc_betterproto.py",
    deps = [
        requirement("grpcio-tools"),
        requirement("betterproto"),
    ],
    visibility = ["//visibility:public"],
)

py_binary(
    name = "fb_export_storage_state",
    srcs = [
        "fb_browser_session.py",
        "fb_export_storage_state.py",
    ],
    main = "fb_export_storage_state.py",
    python_version = "PY3",
    deps = [],
    visibility = ["//visibility:public"],
)

py_binary(
    name = "fb_post_playwright",
    srcs = [
        "fb_browser_session.py",
        "fb_post_playwright.py",
    ],
    main = "fb_post_playwright.py",
    python_version = "PY3",
    deps = [],
    visibility = ["//visibility:public"],
)
