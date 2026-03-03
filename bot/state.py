"""Persistent key-value state backed by SQLite."""

import json
from datetime import datetime, timezone
from db import get_db


class BotState:
    async def get(self, key: str, default=None):
        db = await get_db()
        try:
            cursor = await db.execute(
                "SELECT value FROM bot_state WHERE key = ?", (key,)
            )
            row = await cursor.fetchone()
            if row is None:
                return default
            return json.loads(row["value"])
        finally:
            await db.close()

    async def set(self, key: str, value):
        db = await get_db()
        try:
            now = datetime.now(timezone.utc).isoformat()
            await db.execute(
                """INSERT INTO bot_state (key, value, updated_at)
                   VALUES (?, ?, ?)
                   ON CONFLICT(key) DO UPDATE SET value = ?, updated_at = ?""",
                (key, json.dumps(value), now, json.dumps(value), now),
            )
            await db.commit()
        finally:
            await db.close()

    async def get_many(self, keys: list[str]) -> dict:
        db = await get_db()
        try:
            placeholders = ",".join("?" for _ in keys)
            cursor = await db.execute(
                f"SELECT key, value FROM bot_state WHERE key IN ({placeholders})",
                keys,
            )
            rows = await cursor.fetchall()
            result = {}
            for row in rows:
                result[row["key"]] = json.loads(row["value"])
            return result
        finally:
            await db.close()

    async def set_many(self, pairs: dict):
        db = await get_db()
        try:
            now = datetime.now(timezone.utc).isoformat()
            for key, value in pairs.items():
                await db.execute(
                    """INSERT INTO bot_state (key, value, updated_at)
                       VALUES (?, ?, ?)
                       ON CONFLICT(key) DO UPDATE SET value = ?, updated_at = ?""",
                    (key, json.dumps(value), now, json.dumps(value), now),
                )
            await db.commit()
        finally:
            await db.close()


state = BotState()
