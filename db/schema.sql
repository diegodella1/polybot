CREATE TABLE IF NOT EXISTS trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL,
    condition_id TEXT NOT NULL,
    token_id TEXT NOT NULL,
    side TEXT NOT NULL CHECK(side IN ('up', 'down')),
    signal_score REAL NOT NULL,
    entry_price REAL NOT NULL,
    size_usd REAL NOT NULL,
    shares REAL NOT NULL,
    btc_price REAL,
    outcome TEXT CHECK(outcome IN ('win', 'loss', 'stop_loss', 'take_profit', NULL)),
    pnl REAL,
    bankroll_after REAL,
    signal_details TEXT,
    dry_run BOOLEAN DEFAULT 1,
    order_id TEXT,
    order_status TEXT DEFAULT 'filled',
    created_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS daily_stats (
    date TEXT PRIMARY KEY,
    trades_count INTEGER DEFAULT 0,
    wins INTEGER DEFAULT 0,
    losses INTEGER DEFAULT 0,
    skipped INTEGER DEFAULT 0,
    pnl REAL DEFAULT 0.0,
    bankroll_open REAL,
    bankroll_close REAL,
    max_drawdown REAL DEFAULT 0.0,
    best_trade REAL DEFAULT 0.0,
    worst_trade REAL DEFAULT 0.0
);

CREATE TABLE IF NOT EXISTS bot_state (
    key TEXT PRIMARY KEY,
    value TEXT,
    updated_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS patterns (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    trade_id INTEGER REFERENCES trades(id),
    features BLOB NOT NULL,
    outcome TEXT NOT NULL CHECK(outcome IN ('win', 'loss')),
    created_at TEXT DEFAULT (datetime('now'))
);

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
    outcome TEXT CHECK(outcome IN ('win', 'loss', 'expired')),
    pnl_simulated REAL,
    resolved_at TEXT,
    created_at TEXT DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_trades_timestamp ON trades(timestamp);
CREATE INDEX IF NOT EXISTS idx_trades_outcome ON trades(outcome);
CREATE INDEX IF NOT EXISTS idx_patterns_outcome ON patterns(outcome);
CREATE INDEX IF NOT EXISTS idx_paper_trades_outcome ON paper_trades(outcome);
CREATE INDEX IF NOT EXISTS idx_paper_trades_condition ON paper_trades(condition_id);
