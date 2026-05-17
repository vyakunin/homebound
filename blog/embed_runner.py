"""Bazel entry-point for `//blog:generate_embeddings`.

Equivalent to `python manage.py generate_embeddings [args...]` but lets
Bazel co-locate the binary target with the management command in
`blog/BUILD`. Spec from `NEXT.md` Phase 2 names the target
``//blog:generate_embeddings``; that path needs a binary under //blog.
"""

import os
import sys


def main() -> None:
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "django_config.settings")
    from django.core.management import execute_from_command_line

    execute_from_command_line(["manage.py", "generate_embeddings", *sys.argv[1:]])


if __name__ == "__main__":
    main()
