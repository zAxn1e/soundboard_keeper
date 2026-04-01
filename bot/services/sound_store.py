from __future__ import annotations

import datetime as dt
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from bot.utils.audio import derive_category, normalize_category, normalize_sound_name, sound_name_key


@dataclass(frozen=True)
class SoundRecord:
    guild_id: int
    name: str
    name_key: str
    category: str
    category_key: str
    file_path: str
    volume: int
    uploader_user_id: int
    created_at: str


class SoundStore:
    def __init__(self, db_path: str) -> None:
        self.db_path = Path(db_path)
        if self.db_path.parent != Path(""):
            self.db_path.parent.mkdir(parents=True, exist_ok=True)

        self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._init_schema()

    def _init_schema(self) -> None:
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS sounds (
                guild_id INTEGER NOT NULL,
                name TEXT NOT NULL,
                name_key TEXT NOT NULL,
                category TEXT NOT NULL DEFAULT 'uncategorized',
                category_key TEXT NOT NULL DEFAULT 'uncategorized',
                file_path TEXT NOT NULL,
                volume INTEGER NOT NULL,
                uploader_user_id INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                PRIMARY KEY (guild_id, name_key)
            )
            """
        )

        columns = {
            str(row["name"]): str(row["type"])
            for row in self._conn.execute("PRAGMA table_info(sounds)").fetchall()
        }
        if "category" not in columns:
            self._conn.execute(
                "ALTER TABLE sounds ADD COLUMN category TEXT NOT NULL DEFAULT 'uncategorized'"
            )
        if "category_key" not in columns:
            self._conn.execute(
                "ALTER TABLE sounds ADD COLUMN category_key TEXT NOT NULL DEFAULT 'uncategorized'"
            )

        self._conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_sounds_guild_category
            ON sounds (guild_id, category_key)
            """
        )

        rows = self._conn.execute(
            """
            SELECT guild_id, name_key, name
            FROM sounds
            WHERE category IS NULL
                OR category = ''
                OR category_key IS NULL
                OR category_key = ''
                OR category = 'uncategorized'
                OR category_key = 'uncategorized'
            """
        ).fetchall()
        for row in rows:
            category = derive_category(str(row["name"]))
            category_key = normalize_category(category)
            self._conn.execute(
                """
                UPDATE sounds
                SET category = ?, category_key = ?
                WHERE guild_id = ? AND name_key = ?
                """,
                (category, category_key, int(row["guild_id"]), str(row["name_key"])),
            )

        self._conn.commit()

    def add_sound(
        self,
        *,
        guild_id: int,
        name: str,
        file_path: str,
        volume: int,
        uploader_user_id: int,
    ) -> SoundRecord:
        clean_name = normalize_sound_name(name)
        key = sound_name_key(clean_name)
        category = derive_category(clean_name)
        category_key = normalize_category(category)
        created_at = dt.datetime.now(dt.timezone.utc).isoformat()

        try:
            self._conn.execute(
                """
                INSERT INTO sounds (
                    guild_id, name, name_key, category, category_key,
                    file_path, volume, uploader_user_id, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    guild_id,
                    clean_name,
                    key,
                    category,
                    category_key,
                    file_path,
                    volume,
                    uploader_user_id,
                    created_at,
                ),
            )
            self._conn.commit()
        except sqlite3.IntegrityError as err:
            raise ValueError("duplicate") from err

        return SoundRecord(
            guild_id=guild_id,
            name=clean_name,
            name_key=key,
            category=category,
            category_key=category_key,
            file_path=file_path,
            volume=volume,
            uploader_user_id=uploader_user_id,
            created_at=created_at,
        )

    def get_sound(self, guild_id: int, name: str) -> Optional[SoundRecord]:
        row = self._conn.execute(
            """
            SELECT guild_id, name, name_key, category, category_key, file_path, volume, uploader_user_id, created_at
            FROM sounds
            WHERE guild_id = ? AND name_key = ?
            """,
            (guild_id, sound_name_key(name)),
        ).fetchone()
        return self._row_to_record(row)

    def get_sound_in_category(self, guild_id: int, category: str, name: str) -> Optional[SoundRecord]:
        row = self._conn.execute(
            """
            SELECT guild_id, name, name_key, category, category_key, file_path, volume, uploader_user_id, created_at
            FROM sounds
            WHERE guild_id = ? AND category_key = ? AND name_key = ?
            """,
            (guild_id, normalize_category(category), sound_name_key(name)),
        ).fetchone()
        return self._row_to_record(row)

    def delete_sound(self, guild_id: int, name: str) -> Optional[SoundRecord]:
        current = self.get_sound(guild_id, name)
        if current is None:
            return None

        self._conn.execute(
            "DELETE FROM sounds WHERE guild_id = ? AND name_key = ?",
            (guild_id, current.name_key),
        )
        self._conn.commit()
        return current

    def update_sound(
        self,
        guild_id: int,
        name: str,
        *,
        new_name: Optional[str],
        new_volume: Optional[int],
    ) -> SoundRecord:
        current = self.get_sound(guild_id, name)
        if current is None:
            raise KeyError("missing")

        target_name = normalize_sound_name(new_name) if new_name is not None else current.name
        target_key = sound_name_key(target_name)
        target_category = derive_category(target_name)
        target_category_key = normalize_category(target_category)
        target_volume = current.volume if new_volume is None else new_volume

        if target_key != current.name_key:
            existing = self.get_sound(guild_id, target_name)
            if existing is not None:
                raise ValueError("duplicate")

        self._conn.execute(
            """
            UPDATE sounds
            SET name = ?, name_key = ?, category = ?, category_key = ?, volume = ?
            WHERE guild_id = ? AND name_key = ?
            """,
            (
                target_name,
                target_key,
                target_category,
                target_category_key,
                target_volume,
                guild_id,
                current.name_key,
            ),
        )
        self._conn.commit()

        updated = self.get_sound(guild_id, target_name)
        if updated is None:
            raise RuntimeError("failed to fetch updated sound")
        return updated

    def list_sounds_by_category(self, guild_id: int, category: str) -> list[SoundRecord]:
        rows = self._conn.execute(
            """
            SELECT guild_id, name, name_key, category, category_key, file_path, volume, uploader_user_id, created_at
            FROM sounds
            WHERE guild_id = ? AND category_key = ?
            ORDER BY name COLLATE NOCASE ASC
            """,
            (guild_id, normalize_category(category)),
        ).fetchall()
        return [self._row_to_record(row) for row in rows if row is not None]

    def list_categories(self, guild_id: int) -> list[tuple[str, int]]:
        rows = self._conn.execute(
            """
            SELECT category, COUNT(*) AS count
            FROM sounds
            WHERE guild_id = ?
            GROUP BY category_key, category
            ORDER BY category COLLATE NOCASE ASC
            """,
            (guild_id,),
        ).fetchall()
        return [(str(row["category"]), int(row["count"])) for row in rows]

    def search_categories(self, guild_id: int, query: str, limit: int = 25) -> list[str]:
        rows = self._conn.execute(
            """
            SELECT DISTINCT category
            FROM sounds
            WHERE guild_id = ? AND category_key LIKE ?
            ORDER BY category COLLATE NOCASE ASC
            LIMIT ?
            """,
            (guild_id, f"%{query.strip().casefold()}%", max(1, limit)),
        ).fetchall()
        return [str(row["category"]) for row in rows]

    def search_names(
        self,
        guild_id: int,
        query: str,
        *,
        limit: int = 25,
        category: Optional[str] = None,
    ) -> list[str]:
        pattern = f"%{query.strip().casefold()}%"
        if category:
            rows = self._conn.execute(
                """
                SELECT name
                FROM sounds
                WHERE guild_id = ? AND category_key = ? AND name_key LIKE ?
                ORDER BY name COLLATE NOCASE ASC
                LIMIT ?
                """,
                (guild_id, normalize_category(category), pattern, max(1, limit)),
            ).fetchall()
        else:
            rows = self._conn.execute(
                """
                SELECT name
                FROM sounds
                WHERE guild_id = ? AND name_key LIKE ?
                ORDER BY name COLLATE NOCASE ASC
                LIMIT ?
                """,
                (guild_id, pattern, max(1, limit)),
            ).fetchall()
        return [str(row["name"]) for row in rows]

    @staticmethod
    def _row_to_record(row: Optional[sqlite3.Row]) -> Optional[SoundRecord]:
        if row is None:
            return None
        return SoundRecord(
            guild_id=int(row["guild_id"]),
            name=str(row["name"]),
            name_key=str(row["name_key"]),
            category=str(row["category"]),
            category_key=str(row["category_key"]),
            file_path=str(row["file_path"]),
            volume=int(row["volume"]),
            uploader_user_id=int(row["uploader_user_id"]),
            created_at=str(row["created_at"]),
        )
