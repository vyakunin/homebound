"""Ensure Django is configured before any test imports."""
import os

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "django_config.settings")
os.environ.setdefault("RUNNING_TESTS", "1")

import django  # noqa: E402

django.setup()
