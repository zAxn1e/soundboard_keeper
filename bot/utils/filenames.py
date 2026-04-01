from __future__ import annotations

import uuid
from pathlib import Path


def extension_from_filename(filename: str) -> str:
    return Path(filename).suffix.lower()


def make_storage_filename(original_filename: str) -> str:
    return f"{uuid.uuid4().hex}{extension_from_filename(original_filename)}"


def is_safe_child_path(path: Path, parent: Path) -> bool:
    try:
        path.resolve().relative_to(parent.resolve())
        return True
    except Exception:
        return False
