load("@rules_python//python:defs.bzl", "py_library", "py_test")
load("@homebound_pip//:requirements.bzl", "requirement")
load(":django_test.bzl", "django_py_test")

py_library(
    name = "django_setup",
    srcs = ["django_setup.py"],
    deps = [
        requirement("Django"),
        requirement("psycopg2-binary"),
        "//django_config:settings",
    ],
)

py_library(
    name = "conftest_lib",
    srcs = ["conftest.py"],
    deps = [
        ":django_setup",
        "//blog:models",
        requirement("pytest"),
        requirement("pytest-django"),
        requirement("django-allauth"),
        requirement("requests"),
        requirement("PyJWT"),
        requirement("cryptography"),
    ],
)

filegroup(
    name = "fixtures",
    srcs = glob(["fixtures/**"]),
    visibility = ["//visibility:public"],
)

django_py_test(
    name = "test_models",
    srcs = ["test_models.py"],
    deps = [
        "//blog:models",
        ":conftest_lib",
    ],
)

django_py_test(
    name = "test_views",
    srcs = ["test_views.py"],
    deps = [
        "//blog:adapters",
        "//blog:feeds",
        "//blog:forms",
        "//blog:sitemaps",
        "//blog:stop_words",
        "//blog:views",
        "//blog:urls",
        "//blog:templatetags",
        "//django_config:urls",
        ":conftest_lib",
        requirement("django-allauth"),
    ],
)

django_py_test(
    name = "test_blog_tags",
    srcs = ["test_blog_tags.py"],
    deps = [
        "//blog:models",
        "//blog:templatetags",
        "//blog:urls",
        "//blog:views",
        "//extractors:facebook_api",
        ":conftest_lib",
        requirement("django-allauth"),
    ],
)

django_py_test(
    name = "test_feeds",
    srcs = ["test_feeds.py"],
    deps = [
        "//blog:feeds",
        "//blog:urls",
        "//blog:templatetags",
        ":conftest_lib",
        requirement("django-allauth"),
    ],
)

django_py_test(
    name = "test_import_posts",
    srcs = ["test_import_posts.py"],
    deps = [
        "//blog:models",
        "//blog/management/commands:import_posts",
        "//proto:post_record",
        ":conftest_lib",
    ],
    data = [":fixtures"],
)

# Extractor tests are standalone — no Django dep needed
py_test(
    name = "test_google_plus_extractor",
    srcs = ["test_google_plus_extractor.py"],
    main = "test_google_plus_extractor.py",
    size = "small",
    deps = [
        "//extractors:google_plus",
        "//proto:post_record",
        requirement("beautifulsoup4"),
        requirement("lxml"),
        requirement("pytest"),
    ],
    data = [":fixtures"],
)

py_test(
    name = "test_facebook_api_extractor",
    srcs = ["test_facebook_api_extractor.py"],
    main = "test_facebook_api_extractor.py",
    size = "small",
    deps = [
        "//extractors:facebook_api",
        "//extractors:posts_io",
        "//proto:post_record",
        requirement("requests"),
        requirement("pytest"),
    ],
)

py_test(
    name = "test_facebook_extractor",
    srcs = ["test_facebook_extractor.py"],
    main = "test_facebook_extractor.py",
    size = "small",
    deps = [
        "//extractors:base",
        "//extractors:facebook",
        "//extractors:google_plus",
        "//extractors:posts_io",
        "//proto:post_record",
        requirement("beautifulsoup4"),
        requirement("lxml"),
        requirement("pytest"),
    ],
    data = [":fixtures"],
)

py_test(
    name = "test_activity_log_extractor",
    srcs = ["test_activity_log_extractor.py"],
    main = "test_activity_log_extractor.py",
    size = "small",
    deps = [
        "//extractors:activity_log",
        "//extractors:base",
        "//extractors:posts_io",
        "//proto:post_record",
        requirement("pytest"),
    ],
    data = [":fixtures"],
)

py_test(
    name = "test_activity_log_dedup_stability",
    srcs = ["test_activity_log_dedup_stability.py"],
    main = "test_activity_log_dedup_stability.py",
    size = "small",
    deps = [
        "//extractors:activity_log",
        "//extractors:posts_io",
        "//proto:post_record",
        requirement("pytest"),
    ],
)

py_test(
    name = "test_twitter_extractor",
    srcs = ["test_twitter_extractor.py"],
    main = "test_twitter_extractor.py",
    size = "small",
    deps = [
        "//extractors:twitter_log",
        "//extractors:base",
        "//extractors:posts_io",
        "//proto:post_record",
        requirement("pytest"),
    ],
)

py_test(
    name = "test_wayback_twitter_extractor",
    srcs = ["test_wayback_twitter_extractor.py"],
    main = "test_wayback_twitter_extractor.py",
    size = "small",
    deps = [
        "//extractors:wayback_twitter_log",
        "//extractors:posts_io",
        "//proto:post_record",
        requirement("beautifulsoup4"),
        requirement("lxml"),
        requirement("pytest"),
    ],
)
