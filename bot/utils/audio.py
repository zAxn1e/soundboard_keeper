from __future__ import annotations

import shutil
from typing import Optional


def normalize_sound_name(name: Optional[str]) -> str:
    if not name:
        return ""
    return " ".join(name.split()).strip()


def sound_name_key(name: str) -> str:
    return normalize_sound_name(name).casefold()


def normalize_category(category: str) -> str:
    cleaned = " ".join(category.split()).strip().lower()
    if not cleaned:
        return "uncategorized"
    return cleaned.replace(" ", "_")


def derive_category(sound_name: str) -> str:
    clean_name = normalize_sound_name(sound_name)
    if not clean_name:
        return "uncategorized"

    first_idx: Optional[int] = None
    for sep in ("_", "-", ":"):
        idx = clean_name.find(sep)
        if idx > 0 and (first_idx is None or idx < first_idx):
            first_idx = idx

    if first_idx is None:
        return "uncategorized"

    prefix = clean_name[:first_idx].strip()
    if not prefix:
        return "uncategorized"
    return normalize_category(prefix)


def ffmpeg_available() -> bool:
    return shutil.which("ffmpeg") is not None
