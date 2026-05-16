"""Bazel macro: py_betterproto_library.

Generates betterproto Python dataclasses from .proto files via a genrule, then
wraps the result in a py_library.  All srcs must live in the same Bazel package
(one directory).  grpcio-tools resolves well-known types automatically.

Consumer usage:
    load("//proto:betterproto.bzl", "py_betterproto_library")

    py_betterproto_library(
        name = "post_record",
        srcs = ["post_record.proto", ...],
        visibility = ["//visibility:public"],
    )

Downstream targets just add:
    deps = ["//proto:post_record"]
"""

load("@rules_python//python:defs.bzl", "py_library")
load("@homebound_pip//:requirements.bzl", "requirement")

def py_betterproto_library(name, srcs, deps = [], visibility = None):
    # One generated .py file per .proto source (betterproto 1:1 mapping).
    outs = [src.replace(".proto", ".py") for src in srcs]

    native.genrule(
        name = name + "_gen",
        srcs = srcs,
        outs = outs,
        # $(SRCS) expands to workspace-relative proto paths; last arg is output dir.
        cmd = "$(execpath //tools:protoc_betterproto) $(SRCS) $(RULEDIR)",
        tools = ["//tools:protoc_betterproto"],
        visibility = ["//visibility:private"],
    )

    py_library(
        name = name,
        srcs = [":" + name + "_gen"],
        deps = deps + [requirement("betterproto")],
        visibility = visibility,
    )
