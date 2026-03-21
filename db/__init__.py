import aiosqlite
import os

DB_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "polybot.db")
SCHEMA_PATH = os.path.join(os.path.dirname(__file__), "schema.sql")

# Current schema version — bump when adding new migrations
SCHEMA_VERSION = 3


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

        # Run versioned migrations
        await _run_migrations(db)
    finally:
        await db.close()


async def _run_migrations(db: aiosqlite.Connection):
    """Run incremental migrations using PRAGMA user_version."""
    cursor = await db.execute("PRAGMA user_version")
    row = await cursor.fetchone()
    current_version = row[0] if row else 0

    if current_version < 2:
        await _migrate_v2(db)

    if current_version < 3:
        await _migrate_v3(db)

    if current_version < SCHEMA_VERSION:
        await db.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        await db.commit()


async def _migrate_v2(db: aiosqlite.Connection):
    """Schema v2: strategy v2 columns and tables."""
    # New columns in trades (all nullable for backward compat)
    for col_sql in [
        "ALTER TABLE trades ADD COLUMN tau_at_entry REAL",
        "ALTER TABLE trades ADD COLUMN best_price_at_decision REAL",
        "ALTER TABLE trades ADD COLUMN fill_slippage REAL",
        "ALTER TABLE trades ADD COLUMN phat_at_entry REAL",
        "ALTER TABLE trades ADD COLUMN residual REAL",
    ]:
        try:
            await db.execute(col_sql)
        except Exception:
            pass  # Column already exists

    # Decisions telemetry table
    await db.execute("""
        CREATE TABLE IF NOT EXISTS decisions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts TEXT NOT NULL,
            market TEXT,
            decision TEXT NOT NULL,
            tau REAL,
            delta_pct REAL,
            sigma_pct REAL,
            q_yes REAL,
            spread REAL,
            p0 REAL,
            p_ob REAL,
            p_hat REAL,
            p_hat_low REAL,
            p_req REAL,
            edge REAL,
            created_at TEXT DEFAULT (datetime('now'))
        )
    """)

    # Slippage events table
    await db.execute("""
        CREATE TABLE IF NOT EXISTS slippage_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            trade_id INTEGER REFERENCES trades(id),
            best_at_decision REAL NOT NULL,
            fill_price REAL NOT NULL,
            slippage REAL NOT NULL,
            tick_size REAL,
            created_at TEXT DEFAULT (datetime('now'))
        )
    """)

    # Indices
    await db.execute(
        "CREATE INDEX IF NOT EXISTS idx_decisions_ts ON decisions(ts)"
    )
    await db.execute(
        "CREATE INDEX IF NOT EXISTS idx_slippage_trade ON slippage_events(trade_id)"
    )

    await db.commit()


async def _migrate_v3(db: aiosqlite.Connection):
    """Schema v3: market_duration column for multi-timeframe support."""
    for col_sql in [
        "ALTER TABLE trades ADD COLUMN market_duration INTEGER DEFAULT 300",
        "ALTER TABLE paper_trades ADD COLUMN market_duration INTEGER DEFAULT 300",
    ]:
        try:
            await db.execute(col_sql)
        except Exception:
            pass  # Column already exists
    await db.commit()
