"""Shared utilities used by all extractors.

These are pure-Python helpers with no Django dependency.
"""
from __future__ import annotations

import shutil
from datetime import datetime, timezone
from pathlib import Path


def fix_facebook_encoding(text: str) -> str:
    """Decode Facebook's mojibake encoding.

    Facebook JSON archives store non-ASCII characters as latin-1 byte values
    re-encoded into unicode codepoints. For example, the Cyrillic letter В
    becomes the two-character sequence 'Ð\x92' (latin-1 bytes 0xD0 0x92).
    Encoding the string back to latin-1 and decoding as UTF-8 recovers the
    original text.
    """
    try:
        return text.encode('latin-1').decode('utf-8')
    except (UnicodeDecodeError, UnicodeEncodeError):
        return text


def copy_media_to_dated_dir(src: Path, media_dir: Path, created_at: datetime | None) -> str:
    """Copy a media file into media_dir/YYYY/MM/ and return its relative path.

    The relative path is relative to media_dir's parent (i.e. output_dir),
    matching the local_path convention used by import_posts.
    """
    if created_at is not None:
        year_month = f'{created_at.year:04d}/{created_at.month:02d}'
    else:
        year_month = 'unknown'

    dest_dir = media_dir / year_month
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest_file = dest_dir / src.name

    if not dest_file.exists():
        shutil.copy2(src, dest_file)

    return str(dest_file.relative_to(media_dir.parent))
