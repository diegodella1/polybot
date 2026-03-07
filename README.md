# Polybot

Automated trading bot for [Polymarket](https://polymarket.com) BTC Up/Down 5-minute binary markets. Runs 24/7 on a Raspberry Pi, makes autonomous trading decisions based on real-time BTC price momentum, orderbook analysis, and pattern recognition.

> **Disclaimer:** This is an experimental trading bot. Not financial advice. Use at your own risk.

---

## How It Works

Every 5 seconds, Polybot:

1. **Discovers** the current BTC 5-minute binary market on Polymarket
2. **Computes** a composite trading signal from multiple data sources
3. **Checks** 10 risk gates before allowing a trade
4. **Sizes** the position proportionally to signal strength and drawdown
5. **Executes** a limit order on the Polymarket CLOB (Polygon network)
6. **Resolves** the trade at market expiration and auto-redeems winnings

```
BTC Price Feed ──┐
                 ├──▶ Signals ──▶ Risk Check ──▶ Sizing ──▶ Execute ──▶ Resolve
Orderbook Feed ──┘                                                        │
                                                                          ▼
                                                                    Auto-Redeem
```

---

## Architecture

```
polybot/
├── bot/                        # Core trading engine
│   ├── engine.py               # Main loop: discover → signal → risk → size → execute → resolve
│   ├── signals.py              # Composite signal (momentum, RSI, vol regime, book skew)
│   ├── risk.py                 # 10-gate risk gatekeeper
│   ├── sizing.py               # Signal-proportional position sizing
│   ├── executor.py             # py-clob-client order execution (EOA mode)
│   ├── wallet.py               # Web3 ops: balances, redeem, swap
│   ├── config.py               # YAML config with hot-reload
│   ├── market_discovery.py     # Market fetching & filtering
│   └── state.py                # In-memory bot state
│
├── data/                       # Real-time data feeds
│   ├── binance_ws.py           # BTC/USDT kline WebSocket
│   ├── polymarket_ws.py        # Polymarket orderbook WebSocket
│   └── buffer.py               # Ring buffer with EMA, ATR, RSI
│
├── rag/                        # Pattern recognition (k-NN)
│   ├── pattern_store.py        # k-NN cosine similarity (k=10)
│   ├── features.py             # 8D feature vector extraction
│   └── sentiment.py            # DuckDuckGo + GPT-4o-mini
│
├── web/                        # Dashboard & API
│   ├── app.py                  # FastAPI application factory
│   ├── api.py                  # REST + WebSocket endpoints
│   ├── auth.py                 # Session-based admin auth
│   ├── ws_handler.py           # Live trade/signal broadcaster
│   └── static/                 # HTML/JS/CSS dashboard
│
├── notifications/
│   └── telegram.py             # Trade alerts via Telegram
│
├── db/
│   └── schema.sql              # SQLite schema
│
├── tests/                      # pytest test suite
├── main.py                     # Entry point
├── config.yaml                 # Trading parameters
└── polybot.service             # systemd unit file
```

---

## Signal Engine

The composite signal combines four weighted components into a single value in `[-1, +1]`:

| Component | Weight | Source | Description |
|-----------|--------|--------|-------------|
| **Momentum** | 60% | Binance WS | `0.5×ret_1m + 0.3×ret_5m + 0.2×(EMA5-EMA15)/price`, scaled with `tanh(×50)` |
| **RSI** | 15% | Binance WS | 14-period RSI normalized: `(RSI - 50) / 50` |
| **Vol Regime** | 15% | Binance WS | `-(ATR5/ATR20 - 1) × 2` — penalizes choppy markets, favors calm conditions |
| **Book Skew** | 10% | Polymarket WS | Inverted orderbook imbalance — heavy bid side acts as contrarian sell signal |

- Positive composite → BTC going **UP**
- Negative composite → BTC going **DOWN**
- Trade only when `|composite| ≥ threshold` (default: 0.10)
- Null signals are excluded from the weighted sum (dynamic renormalization)

---

## Risk Management

Ten sequential gates must all pass before a trade is allowed:

| # | Gate | Blocks When |
|---|------|-------------|
| 1 | Bot enabled | Master switch is off |
| 2 | Signal strength | `\|signal\| < threshold` |
| 3 | Bankroll floor | Balance below emergency minimum |
| 4 | Consecutive losses | Too many losses in a row → cooldown |
| 5 | Circuit breaker | Win rate below minimum over N trades |
| 6 | Max exposure | Open positions exceed limit |
| 7 | Position guard | Already holding a position |
| 8 | Trade cooldown | Min 2 minutes between trades (anti rapid-fire) |
| 9 | Post-loss cooldown | 5-minute pause after every loss |
| 10 | Entry price filter | Price must be in [0.46, 0.65] range |

---

## Decision Algorithm

Every 5 seconds the bot runs this pipeline. Every gate is a hard filter — one failure skips the round:

```
 1. Discover current 5-min BTC market on Polymarket
 2. Compute composite signal from 4 sources:
      momentum = tanh(0.5·ret_1m + 0.3·ret_5m + 0.2·EMA_cross) × 60%
      rsi      = (RSI_14 - 50) / 50                              × 15%
      vol      = -(ATR_5/ATR_20 - 1) × 2                         × 15%
      skew     = -orderbook_imbalance                             × 10%
      composite ∈ [-1, +1] → positive = UP, negative = DOWN
 3. Filter: |composite| must be in [threshold, max_signal]
 4. Trend filter: signal direction must align with EMA trend
 5. Risk gates (10 checks): bankroll floor, loss streaks, cooldowns, exposure
 6. Orderbook validation: fresh, non-stale, bid < ask
 7. Entry price must be in [0.46, 0.65] — only near-50/50 markets
 8. Spread check: reject if bid-ask spread > 5¢
 9. Half-Kelly sizing: bet proportional to edge × bankroll
10. Execute limit order on-chain (Polygon)
11. Hold to expiration — no TP/SL (double fee kills profits)
12. Resolve: winner redeems $1.00/share → USDC.e back to wallet
```

---

## Position Sizing

Signal-proportional sizing with drawdown scaling:

```
base position   = bankroll × lerp(base_pct, max_pct, signal_strength)
drawdown factor = 1.0 - (|daily_loss| / loss_limit) × 0.75    # [0.25, 1.0]
final size      = base × drawdown_factor
                  clamped to [min_trade_usd, bankroll × max_trade_pct]
```

Stronger signals get larger positions. Losing days automatically shrink bet sizes.

---

## Dashboard

Public web dashboard with live updates via WebSocket:

- **Stat cards** — Wallet balance, daily P&L, net profit, win rate
- **Equity curve** — Cumulative performance chart (1H to ALL timeframes)
- **Live status panel** — Current state, market, signal, last decision, round count
- **Trade log** — History with side, entry price, BTC price, outcome, P&L
- **Signal gauges** — Real-time composite + individual signal bars
- **Risk panel** — Loss streak, daily loss, sizing multiplier, cooldowns, circuit breaker
- **Admin panel** — Config editor, wallet management, API key setup (password-protected)

---

## Setup

### Prerequisites

- Python 3.11+
- A Polymarket account with CLOB API credentials
- An EOA wallet with USDC.e on Polygon
- (Optional) Telegram bot token for trade alerts
- (Optional) OpenAI API key for sentiment analysis

### Installation

```bash
git clone https://github.com/diegodella1/polybot.git
cd polybot

python -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

### Configuration

1. **Create `.env`** with your credentials:

```env
POLYMARKET_PRIVATE_KEY=0x...
POLYMARKET_API_KEY=...
POLYMARKET_API_SECRET=...
POLYMARKET_API_PASSPHRASE=...

# Optional
INITIAL_BANKROLL=50.0
ADMIN_PASSWORD=your_password
TELEGRAM_TOKEN=bot_token_from_botfather
TELEGRAM_CHAT_ID=your_chat_id
OPENAI_API_KEY=sk-...
```

You can also derive API credentials from your private key:

```bash
python setup_keys.py
```

2. **Edit `config.yaml`** to tune trading parameters (or use the admin panel at `/admin`).

### Run

```bash
# Direct
source venv/bin/activate
python main.py

# As systemd service
sudo cp polybot.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable polybot
sudo systemctl start polybot
```

The dashboard will be available at `http://localhost:8888`.

### Useful Commands

```bash
sudo systemctl status polybot       # Service status
sudo journalctl -u polybot -f       # Follow logs
sudo systemctl restart polybot      # Restart after config changes
```

---

## Config Reference

All parameters in `config.yaml` are hot-reloadable from the admin panel.

### Sizing

| Parameter | Default | Description |
|-----------|---------|-------------|
| `max_trade_pct` | 0.20 | Max position size as % of bankroll |
| `base_trade_pct` | 0.07 | Base size at threshold signal |
| `min_trade_usd` | 2.5 | Minimum trade size in USD |
| `max_exposure_usd` | 5 | Max simultaneous exposure |

### Risk

| Parameter | Default | Description |
|-----------|---------|-------------|
| `trade_threshold` | 0.10 | Minimum \|composite\| to trade |
| `max_signal` | 1.0 | Maximum \|composite\| (1.0 = no cap) |
| `daily_loss_limit_pct` | 0.50 | Daily loss limit (% of bankroll) |
| `max_consecutive_losses` | 5 | Losses before cooldown activates |
| `cooldown_rounds` | 1 | Rounds to pause after loss streak |
| `bankroll_floor_usd` | 2 | Emergency stop below this balance |
| `circuit_breaker_window` | 30 | Trades to evaluate for win rate |
| `min_win_rate` | 0 | Min win rate (0 = disabled) |

### Entry Filters

| Parameter | Default | Description |
|-----------|---------|-------------|
| `min_entry_price` | 0.46 | Min price to enter (cents) |
| `max_entry_price` | 0.65 | Max price to enter (cents) |
| `max_spread_cents` | 5 | Max bid-ask spread |
| `min_time_remaining_sec` | 30 | Min seconds left in market |
| `trade_cooldown_seconds` | 120 | Min seconds between trades |
| `post_loss_cooldown_seconds` | 300 | Pause after a loss (seconds) |

### Signal Weights

| Parameter | Default | Description |
|-----------|---------|-------------|
| `weights.momentum` | 0.60 | BTC price momentum weight |
| `weights.rsi` | 0.15 | RSI indicator weight |
| `weights.vol_ratio` | 0.15 | Volatility regime weight |
| `weights.book_skew` | 0.10 | Inverted orderbook imbalance weight |

### Toggles

| Parameter | Default | Description |
|-----------|---------|-------------|
| `dry_run` | false | Paper trading mode (no real orders) |
| `bot_enabled` | true | Master enable/disable |
| `use_tp_sl` | false | Stop-loss / take-profit (not recommended due to double fees) |
| `rag_enabled` | true | k-NN pattern matching |
| `telegram_enabled` | true | Telegram notifications |
| `invert_up_signal` | false | Flip UP signals to DOWN |

---

## API Endpoints

### Public

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/api/status` | Engine status |
| `GET` | `/api/trades` | Trade history |
| `GET` | `/api/stats/daily` | Daily stats |
| `GET` | `/api/stats/summary` | Win rate, P&L, totals |
| `GET` | `/api/signals` | Last computed signals |
| `GET` | `/api/rag/patterns` | Recent patterns |
| `WS` | `/api/trades/live` | Live trade/signal stream |

### Admin (requires login)

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/api/auth/login` | Authenticate |
| `GET` | `/api/config` | Get config |
| `PUT` | `/api/config` | Update config |
| `POST` | `/api/bot/start` | Start engine |
| `POST` | `/api/bot/stop` | Stop engine |
| `GET` | `/api/wallet/balances` | On-chain balances |
| `POST` | `/api/wallet/redeem-all` | Redeem winning positions |
| `POST` | `/api/wallet/send` | Transfer USDC.e |
| `POST` | `/api/keys/save` | Save API keys |
| `POST` | `/api/keys/derive` | Derive keys from private key |

---

## Database

SQLite with four tables:

- **`trades`** — Full trade history (entry, exit, P&L, signal details, BTC price)
- **`daily_stats`** — Aggregated daily performance
- **`bot_state`** — Key-value store for runtime state
- **`patterns`** — 8D feature vectors for k-NN pattern matching

---

## Market Mechanics

Polymarket's BTC 5-minute binary markets work as follows:

- Every 5 minutes, a new market opens: "Will BTC go Up or Down?"
- Buy **UP** tokens (0-100¢) or **DOWN** tokens (0-100¢)
- At expiration, winning side pays **$1.00 per share**, losing side pays **$0.00**
- **10% taker fee** on entry (buy orders). No fee on redemption
- Polymarket uses the **Polygon** network (USDC.e)

**Fee impact example:**
- Buy 5 shares @ 50¢ = $2.50 cost + $0.25 fee = $2.75 total
- Win: redeem 5 × $1.00 = $5.00 → profit = $5.00 - $2.75 = **+$2.25**
- Lose: $0.00 → loss = **-$2.75**

---

## Testing

```bash
source venv/bin/activate
python -m pytest tests/ -v
```

Tests cover risk checks, signal computation, and position sizing.

---

## Tech Stack

| Component | Technology |
|-----------|-----------|
| Language | Python 3.11, asyncio |
| Web | FastAPI + Uvicorn |
| Trading | py-clob-client (Polymarket CLOB) |
| Blockchain | Web3.py (Polygon) |
| Database | SQLite + aiosqlite |
| Data feeds | Binance WS, Polymarket WS |
| Indicators | NumPy (EMA, ATR, RSI) |
| ML | k-NN cosine similarity |
| Notifications | python-telegram-bot |
| Sentiment | DuckDuckGo + GPT-4o-mini |
| Hosting | Raspberry Pi 5 (8GB) + systemd |

---

## License

Private project. All rights reserved.
