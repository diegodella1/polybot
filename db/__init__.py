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
        # Create paper_trades table if missing (fair value paper trading)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS paper_trades (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                condition_id TEXT NOT NULL,
                side TEXT NOT NULL CHECK(side IN ('up', 'down')),
                prob_estimated REAL NOT NULL,
                market_price REAL NOT NULL,
                edge REAL NOT NULL,
                vol_5m REAL,
                drift_5m REAL,
                price_up REAL,
                price_down REAL,
                btc_price REAL,
                outcome TEXT CHECK(outcome IN ('win', 'loss')),
                pnl_simulated REAL,
                resolved_at TEXT,
                created_at TEXT DEFAULT (datetime('now'))
            )
        """)
        await db.commit()
    finally:
        await db.close()
