import aiosqlite
import os

DB_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "polybot.db")
SCHEMA_PATH = os.path.join(os.path.dirname(__file__), "schema.sql")


async def get_db() -> aiosqlite.Connection:
    db = await aiosqlite.connect(DB_PATH)
    db.row_factory = aiosqlite.Row
    await db.execute("PRAGMA journal_mode=WAL")
    await db.execute("PRAGMA foreign_keys=ON")
    return db


async def init_db():
    with open(SCHEMA_PATH) as f:
        schema = f.read()
    db = await get_db()
    try:
        await db.executescript(schema)
        # Idempotent migrations: add columns if missing
        for col_sql in [
            "ALTER TABLE trades ADD COLUMN dry_run BOOLEAN DEFAULT 1",
            "ALTER TABLE trades ADD COLUMN order_id TEXT",
            "ALTER TABLE trades ADD COLUMN order_status TEXT DEFAULT 'filled'",
        ]:
            try:
                await db.execute(col_sql)
            except Exception:
                pass  # Column already exists
        await db.commit()
    finally:
        await db.close()
