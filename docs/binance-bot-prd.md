# PRD: Bot de Trading Autónomo — Binance Spot

## Resumen

Bot de trading autónomo para crypto spot en Binance, con señales técnicas filtradas por un LLM que aporta contexto macro/noticias. Capital inicial: $100 USDT. Objetivo: 10-20% mensual neto. Corre en Raspberry Pi 5 como servicio, reutilizando la infraestructura de Polybot.

## Problema

El bot actual de Polymarket pierde dinero por diseño: 10% taker fee en mercados binarios de 5 minutos requiere >55% WR para break-even. Binance spot cobra 0.1% (0.075% con BNB), haciendo viable cualquier edge >0.5%.

## Objetivos

| Métrica | Target | Mínimo viable |
|---------|--------|---------------|
| Win rate | 55%+ | 52% |
| PnL mensual | +$10-20 (10-20%) | +$0 (no perder) |
| Max drawdown | -10% | -15% (kill switch) |
| Trades/día | 3-5 | 1-2 |
| Fee mensual total | <$5 (5%) | <$8 |
| Uptime | 95%+ | 90% |

## Arquitectura

```
[Binance WS] ──> [Price Buffer + Indicadores]
                         |
                    [Signal Engine]
                         |
                  [LLM Context Filter] ──> [Calendar API / News Feed]
                         |
                    [Risk Manager]
                         |
                    [Position Sizer]
                         |
                    [Executor] ──> [Binance REST API]
                         |
                    [Trade DB + Dashboard]
```

### Componentes

#### 1. Data Layer
- **Binance WebSocket**: klines 1m, 5m, 15m para pares seleccionados
- **Price Buffer**: ring buffer con EMAs, RSI, ATR, VWAP, Bollinger Bands
- **Orderbook**: top 10 levels para detectar soporte/resistencia inmediato

#### 2. Signal Engine (sin LLM)
Genera señales técnicas puras, determinísticas y rápidas.

**Estrategias (seleccionadas por régimen):**

- **Trend Following** (régimen trending): EMA crossover + ADX + volumen
- **Mean Reversion** (régimen ranging): RSI extremos + Bollinger bounce + volumen bajo
- **Breakout** (régimen compresión): squeeze de Bollinger + volumen spike

**Timeframe principal**: 15 minutos
- Lo suficientemente lento para que la latencia del Pi no importe
- Lo suficientemente rápido para capturar movimientos intraday
- 1m y 5m como confirmación, no como señal primaria

**Output**: score [-1, +1] + dirección + confianza + régimen detectado

#### 3. LLM Context Filter
NO decide trades. Solo responde: "¿hay razón para NO operar ahora?"

**Input estructurado (no scraping libre):**
- Calendario económico (API de investing.com o similar)
- Fear & Greed Index
- Funding rates de futuros (sentimiento)
- Eventos conocidos (halving, merge, FOMC) de calendario estático

**Modelo**: GPT-4o-mini, temperature=0, max_tokens=100

**Prompt template**:
```
Dado el contexto actual del mercado crypto:
- Próximo evento macro: {evento}
- Fear & Greed: {fg_index}
- BTC funding rate: {funding}
- Hora UTC: {hora}

El bot quiere {comprar/vender} {par} basado en señal técnica.
¿Hay razón fuerte para NO ejecutar? Responde JSON:
{"block": true/false, "reason": "..."}
```

**Fallback**: si el LLM falla (timeout, error, rate limit), el bot opera igual. El filtro es opt-out.

**Costo estimado**: 5 calls/día × $0.005 = $0.75/mes

#### 4. Risk Manager
- **Max por trade**: 2% del capital ($2 con $100)
- **Max exposure total**: 10% del capital
- **Max trades/día**: 5
- **Stop loss**: obligatorio en cada trade (ATR-based, 1.5× ATR)
- **Take profit**: 2:1 risk/reward mínimo
- **Kill switch**: -15% drawdown total → apaga el bot, notifica por Telegram
- **Cooldown post-loss**: 30 minutos
- **Correlación**: no abrir 2 posiciones en pares correlacionados (BTC + ETH)

#### 5. Position Sizer
- Half-Kelly basado en WR histórica de los últimos 50 trades
- Floor: $5 (mínimo de Binance)
- Ceiling: 3% del capital
- Scaling: reduce sizing durante drawdown (drawdown multiplier)

#### 6. Executor
- **Órdenes**: limit orders (maker fee 0.1%, no taker)
- **Timeout**: si no llena en 30s, cancela y re-evalúa
- **Slippage protection**: max 0.2% desviación del precio esperado
- **Stop loss**: stop-limit order inmediata post-fill
- **API**: python-binance o binance-connector-python

#### 7. Pares
Empezar con 2-3 pares de alta liquidez:
- **BTC/USDT**: principal, más datos, más predecible
- **ETH/USDT**: segundo par, correlacionado pero con beta mayor
- **SOL/USDT**: opcional, más volátil, más oportunidades

No agregar más pares hasta validar rentabilidad con estos.

## Regímenes de Mercado

Detector automático que clasifica el mercado actual:

| Régimen | Indicadores | Estrategia | Sizing |
|---------|-------------|------------|--------|
| Trending | ADX > 25, EMAs alineadas | Trend following | Normal |
| Ranging | ADX < 20, precio entre BBands | Mean reversion | Reducido |
| Volatile | ATR > 2× promedio | Solo breakouts confirmados | Mínimo |
| Quiet | ATR < 0.5× promedio, volumen bajo | No operar | 0 |

## Stack Técnico

| Componente | Tecnología |
|------------|------------|
| Runtime | Python 3.11 + asyncio |
| Exchange API | python-binance |
| Indicadores | ta-lib o pandas-ta + numpy |
| LLM | OpenAI API (GPT-4o-mini) |
| DB | SQLite (como Polybot) |
| Dashboard | FastAPI + static HTML (reutilizar Polybot) |
| Notificaciones | Telegram |
| Deploy | systemd en Pi 5 |
| Calendario | investpy o API gratuita |

## Base de Datos

### Tabla: trades
```sql
CREATE TABLE trades (
    id INTEGER PRIMARY KEY,
    pair TEXT NOT NULL,
    side TEXT NOT NULL,          -- buy/sell
    entry_price REAL,
    exit_price REAL,
    size_usdt REAL,
    quantity REAL,
    stop_loss REAL,
    take_profit REAL,
    signal_score REAL,
    signal_regime TEXT,
    llm_blocked INTEGER DEFAULT 0,
    llm_reason TEXT,
    entry_time TEXT,
    exit_time TEXT,
    pnl REAL,
    fees REAL,
    outcome TEXT,               -- win/loss/stopped/cancelled
    notes TEXT,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);
```

### Tabla: daily_stats
```sql
CREATE TABLE daily_stats (
    date TEXT PRIMARY KEY,
    trades INTEGER,
    wins INTEGER,
    losses INTEGER,
    pnl REAL,
    fees REAL,
    max_drawdown REAL,
    regime TEXT
);
```

## Fases de Desarrollo

### Fase 0: Paper Trading (semana 1-2)
- Data feeds + indicadores + signal engine
- Ejecutar en modo dry_run (log trades sin ejecutar)
- Dashboard básico mostrando señales y trades simulados
- **Criterio de salida**: 200+ trades simulados, WR > 52%, PnL simulado positivo

### Fase 1: Live Mínimo (semana 3-4)
- Executor real con Binance API
- Solo BTC/USDT, sizing mínimo ($5)
- Stop loss obligatorio
- Kill switch activo
- **Criterio de salida**: 50+ trades reales, no bugs críticos, PnL ≥ $0

### Fase 2: LLM Filter (semana 5-6)
- Integrar GPT-4o-mini como filtro de contexto
- Calendario económico
- Comparar WR con/sin filtro (A/B tracking)
- **Criterio de salida**: evidencia de que el filtro mejora (o al menos no empeora)

### Fase 3: Multi-par + Optimización (semana 7+)
- Agregar ETH/USDT
- Auto-tuning de parámetros con Optuna (bayesian optimization)
- Detector de régimen más sofisticado
- Considerar agregar SOL/USDT

## Validación Obligatoria

Antes de ir live:
1. Paper trading 2 semanas mínimo con 200+ trades
2. WR > 52% sostenido (no solo promedio, rolling window)
3. Sharpe ratio > 0.5
4. Max drawdown simulado < 15%
5. Todos los edge cases testeados: exchange offline, API rate limit, WiFi caído, Pi reboot

## Riesgos y Mitigaciones

| Riesgo | Probabilidad | Impacto | Mitigación |
|--------|-------------|---------|-----------|
| Sin edge real (como Polymarket) | Alta | Total | Paper trading obligatorio, kill switch |
| Overfitting a backtest | Alta | Alto | Walk-forward validation, out-of-sample |
| API key comprometida | Baja | Total | IP whitelist, no withdrawal permission |
| Pi se reinicia mid-trade | Media | Medio | Crash recovery, stop loss en exchange |
| Binance restringe Argentina | Baja | Alto | VPN, exchange alternativo (Bybit) |
| LLM alucinando | Media | Bajo | Fallback: operar sin filtro |
| Fees comen ganancias | Media | Alto | Max 5 trades/día, limit orders |
| Flash crash / black swan | Baja | Alto | Stop loss siempre, max 10% exposure |

## Estructura de Archivos

```
binancebot/
├── main.py                 # Entry point: engine + FastAPI
├── config.yaml             # Parámetros editables
├── .env                    # API keys, secrets
├── bot/
│   ├── engine.py           # Main loop
│   ├── signals.py          # Indicadores técnicos
│   ├── regime.py           # Detector de régimen
│   ├── risk.py             # Risk manager
│   ├── sizing.py           # Position sizer
│   ├── executor.py         # Binance API orders
│   ├── config.py           # Config loader
│   └── state.py            # State manager
├── llm/
│   ├── context_filter.py   # LLM filter
│   └── calendar.py         # Economic calendar
├── data/
│   ├── binance_ws.py       # WebSocket feed
│   └── buffer.py           # Ring buffer + indicators
├── web/
│   ├── app.py              # FastAPI routes
│   └── static/             # Dashboard HTML/JS/CSS
├── notifications/
│   └── telegram.py         # Alerts
├── db.py                   # SQLite
├── tests/
│   ├── test_signals.py
│   ├── test_risk.py
│   └── test_sizing.py
└── docs/
    └── strategy.md
```

## Reutilización de Polybot

Componentes que se copian directo o con mínima adaptación:
- `bot/state.py` — state manager (idéntico)
- `bot/config.py` — config loader (idéntico)
- `bot/risk.py` — estructura base (adaptar checks)
- `bot/sizing.py` — half-Kelly (idéntico)
- `data/buffer.py` — ring buffer (agregar indicadores)
- `data/binance_ws.py` — ya existe
- `web/` — dashboard (adaptar cards)
- `notifications/telegram.py` — idéntico
- `db.py` — idéntico

Componentes nuevos:
- `bot/executor.py` — Binance spot orders (reescribir)
- `bot/signals.py` — indicadores técnicos nuevos
- `bot/regime.py` — detector de régimen (nuevo)
- `llm/context_filter.py` — filtro LLM (nuevo)

## Presupuesto Mensual

| Item | Costo |
|------|-------|
| Trading fees (5 trades/día × 0.1%) | ~$3-5 |
| GPT-4o-mini (5 calls/día) | ~$0.75 |
| Infraestructura (Pi 5) | $0 (ya corre) |
| **Total** | **~$4-6/mes** |

Para ser rentable: necesita generar >$6/mes neto = 6% sobre $100.

## Criterios de Éxito / Fracaso

**Éxito** (seguir):
- WR > 52% después de 100 trades reales
- PnL neto positivo después de fees
- Max drawdown < 15%

**Fracaso** (parar y re-evaluar):
- WR < 48% después de 100 trades
- Drawdown > 15% en cualquier momento
- PnL negativo después de 200 trades
- Fees > ganancias brutas sostenidamente
