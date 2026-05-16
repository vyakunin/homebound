"""Bazel macro that adds standard Django test deps to every test target."""

load("@rules_python//python:defs.bzl", "py_test")
load("@homebound_pip//:requirements.bzl", "requirement")

def django_py_test(name, srcs, deps, data = None, size = "small", env = None, **kwargs):
    """Macro that adds common Django test deps and environment vars."""
    all_deps = list(deps) + [
        ":django_setup",
        "//django_config:settings",
        "//blog:apps",
        requirement("Django"),
        requirement("pytest"),
        requirement("pytest-django"),
    ]
    all_data = list(data or []) + ["//blog:migrations"]
    all_env = dict(env or {})
    all_env.setdefault("DJANGO_SETTINGS_MODULE", "django_config.settings")
    all_env.setdefault("RUNNING_TESTS", "1")
    py_test(
        name = name,
        srcs = srcs,
        main = srcs[0],
        deps = all_deps,
        data = all_data,
        env = all_env,
        size = size,
        **kwargs
    )
