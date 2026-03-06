# Polybot — Definición de Estrategia de Trading

Bot automatizado para mercados binarios de 5 minutos "Bitcoin Up or Down" en Polymarket.

---

## Mercado Objetivo

- **Producto**: Polymarket BTC Up/Down 5-minute binary markets
- **Payout**: Binario — cada share vale $1 si gana, $0 si pierde
- **Frecuencia**: Un mercado nuevo cada 5 minutos, 24/7
- **Instrumento subyacente**: BTC/USDT (Binance)

---

## Señal Compuesta

La decisión de entrada se basa en una señal compuesta ∈ [-1, 1] que agrega 5 componentes:

### 1. Momentum (peso: 0.35)

Dirección de corto plazo del BTC basado en klines de 1 minuto de Binance.

```
raw = 0.5 × ret_1m + 0.3 × ret_5m + 0.2 × (EMA5 - EMA15) / price
signal = tanh(raw × 150)
```

- `ret_Nm` = retorno porcentual sobre N candles (usa precio live, no solo velas cerradas)
- El scale=150 es agresivo: movimientos de ~0.67% saturan la señal a ±1
- 50% del peso en ret_1m lo hace muy reactivo al último movimiento

### 2. Book Skew (peso: 0.25)

Desequilibrio del orderbook del token Up en Polymarket.

```
imbalance = (bid_volume - ask_volume) / (bid_volume + ask_volume)
```

- Calculado sobre los top 5 niveles de precio
- Requiere mínimo 0.10 de volumen en cada lado
- Si el book está vacío o unilateral → no contribuye

### 3. RAG Pattern Matching (peso: 0.15)

k-NN (k=10) sobre patrones históricos almacenados post-trade.

**Vector de features (8 dimensiones)**:
| # | Feature |
|---|---------|
| 0 | ret_1m |
| 1 | ret_5m |
| 2 | ret_15m |
| 3 | desviación EMA rápida (EMA5/price - 1) |
| 4 | desviación EMA lenta (EMA15/price - 1) |
| 5 | vol_ratio (ATR5/ATR20) |
| 6 | RSI(14) normalizado (0-1) |
| 7 | tiempo restante normalizado (sec_remaining/300) |

```
similarities = cosine_similarity(current_features, historical_matrix)
top_10 = 10 vecinos más cercanos
win_ratio = wins_in_top10 / 10
signal = (win_ratio - 0.5) × 2.0    // 0.5 → 0, 0.8 → +0.6, 0.2 → -0.6
```

- Requiere mínimo 10 patrones almacenados para activarse
- Se auto-alimenta: cada trade resuelto agrega un patrón

### 4. Fair Value (peso: 0.10)

Detecta mispricing entre lo que BTC implica y lo que Polymarket cotiza.

```
btc_implied_prob = 0.5 + momentum × 0.5
gap = btc_implied_prob - midpoint_polymarket
signal = tanh(gap × 10)
```

- Si BTC sube fuerte (mom=0.8 → implied=0.9) pero el token Up cotiza a 0.55, hay un gap de 0.35
- Gaps de ~10% ya generan señales fuertes

### 5. Sentiment (peso: 0.05)

Análisis de sentimiento de noticias BTC via DuckDuckGo + GPT-4o-mini.

- Busca "bitcoin BTC price analysis today sentiment" (5 resultados)
- GPT-4o-mini clasifica sentimiento en [-1.0, +1.0]
- Cache de 30 minutos
- Costo: centavos/día (~150 tokens por llamada)

### Régimen de Volatilidad (multiplicador, no componente)

Modifica los pesos de momentum y fair_value según el régimen:

```
vol_ratio = ATR(5) / ATR(20)

Alta vol (>1.5): momentum ×1.3, fair_value ×0.7
Baja vol (<0.7): momentum ×0.7, fair_value ×1.3
Normal: interpolación lineal
```

### Agregación

```
composite = Σ(weight_i × value_i) / Σ(active_weights)
```

Solo se ponderan componentes activos (no-None, no-zero) — los inactivos no diluyen.

### Inversión de Señal

Con `invert_up_signal: true`, señales positivas (UP) se invierten a negativas (DOWN). Actualmente el bot **siempre opera DOWN**.

### Threshold

Trade ejecutable si `|composite| ≥ 0.10`

---

## Risk Management

Checks evaluados en orden — cualquier fallo bloquea el trade:

| # | Check | Condición de bloqueo |
|---|-------|---------------------|
| 1 | Bot habilitado | `enabled = false` |
| 2 | Señal mínima | `|signal| < 0.10` |
| 3 | Bankroll floor | `bankroll < $5` (emergency stop) |
| 4 | Pérdidas consecutivas | `≥ 3 losses` → cooldown de 1 round |
| 5 | Circuit breaker | `win_rate < 30%` sobre últimos 30 trades |
| 6 | Exposición máxima | `exposure ≥ $5` |
| 7 | Posición abierta | Solo 1 posición simultánea |
| 8 | Cooldown entre trades | Mínimo 15 segundos entre trades |

### Drawdown Multiplier

Reduce sizing progresivamente según pérdida diaria acumulada:

```
Si daily_pnl ≥ 0: multiplier = 1.0
Sino:
  t = min(|daily_pnl| / (bankroll × 15%), 1.0)
  multiplier = 1.0 - t × 0.75
```

Va de 1.0 (sin pérdida) a 0.25 (al límite de pérdida diaria del 15%).

---

## Position Sizing

Sizing proporcional a la fuerza de la señal (no Kelly clásico):

```
t = min((|signal| - threshold) / (0.5 - threshold), 1.0)
fraction = 5% + t × 3%                    // 5% en threshold, 8% en señal máxima
size_usd = bankroll × fraction × drawdown_multiplier
size_usd = clamp(size_usd, $1, bankroll × 8%)
shares = floor(size_usd / entry_price)
```

**Rango efectivo**: con bankroll $20 → trades de $1.00 a $1.60.

---

## Filtros de Entrada

| Filtro | Valor | Razón |
|--------|-------|-------|
| Precio mínimo | 25¢ | Evitar tokens muy baratos (alto riesgo, bajo reward) |
| Precio máximo | 65¢ | Evitar tokens caros (bajo upside) |
| Spread máximo | 5¢ | Costo de ejecución tolerable |
| Tiempo restante mínimo | 40s | Suficiente para ejecutar y monitorear |

---

## Ejecución

### Entrada
- **Tipo**: BUY limit order GTC (Good Till Cancelled)
- **Precio**: best_ask + slippage (~3%, mín 2¢)
- **Verificación**: Poll cada 5s por 15s, cancela si no se llena
- **Post-fill**: Aprueba allowance de tokens para futura venta

### Stop-Loss / Take-Profit (monitoreo activo)
- Check cada ~6 segundos durante la vida del mercado
- **Stop-loss**: si `best_bid ≤ entry × 0.85` → vende FOK al bid - 1¢
- **Take-profit**: si `best_bid ≥ entry × 1.10` → vende FOK al bid - 1¢

### Resolución
- **Early exit** (SL/TP): `pnl = proceeds - cost`
- **Win** (resolución de mercado): `pnl = shares × $1.00 - cost`
- **Loss** (resolución de mercado): `pnl = -cost`
- Auto-redeem de tokens ganadores on-chain (CTF → USDC.e)

---

## Flujo Completo de un Round (~10 segundos)

```
DISCOVER → ¿hay mercado activo?
  ↓ sí
FILTROS → ¿≥40s restantes? ¿data fresca?
  ↓ sí
SIGNAL → calcular composite de 5 componentes
  ↓ |composite| ≥ 0.10
RISK → pasar 8 checks
  ↓ todo OK
SIZE → calcular shares según señal + drawdown
  ↓ filtros de precio/spread OK
EXECUTE → colocar orden, esperar fill
  ↓ filled
MONITOR → check SL/TP cada 6s hasta resolución
  ↓
RESOLVE → calcular P&L, actualizar estado
  ↓
POST-TRADE → RAG pattern store, stats, auto-redeem
```

---

## Infraestructura

| Componente | Detalle |
|------------|---------|
| Runtime | Python 3.11 + asyncio |
| Hardware | Raspberry Pi 5, 8GB RAM |
| Data BTC | Binance WebSocket (klines 1m) |
| Data Polymarket | WebSocket + REST fallback (orderbook) |
| Ejecución | py-clob-client, EOA mode (firma directa) |
| LLM | OpenAI GPT-4o-mini (solo sentiment, ~centavos/día) |
| DB | SQLite local (trades, stats, patterns) |
| Dashboard | FastAPI + WebSocket push + HTML/JS estático |
| Notificaciones | Telegram (alertas de trades) |
| Deploy | systemd service |

---

## Parámetros Actuales

| Parámetro | Valor |
|-----------|-------|
| Bankroll | ~$14 USDC |
| Dry run | Sí (simulando, alimentando RAG) |
| Lado | Solo DOWN (inversión activa) |
| Trade threshold | 0.10 |
| Max trade | 8% del bankroll |
| Stop-loss | -15% del entry |
| Take-profit | +10% del entry |
| Daily loss limit | 15% del bankroll |
| Circuit breaker | Win rate < 30% sobre 30 trades |
| Cooldown | 15 segundos entre trades |

---

## Observaciones para Review

1. **Asimetría SL/TP**: Stop-loss a -15% pero take-profit a +10%. En trades que salen por SL/TP (no por resolución binaria), se necesita >60% win rate para ser profitable. La mayoría de trades se resuelven por expiración binaria.

2. **Siempre DOWN**: Con la inversión activa, el bot nunca opera UP. Esto implica un sesgo direccional que asume que el modelo predice UP incorrectamente.

3. **Circuit breaker generoso**: 30% win rate mínimo permite operar perdiendo 70% de las veces.

4. **Sizing pequeño**: Rango efectivo $1.00-$1.60 por trade. Diseñado para aprendizaje con bajo riesgo.

5. **RAG se auto-entrena**: Incluye datos de dry run. Si la simulación no es representativa de condiciones reales (sin slippage real, fills instantáneos), los patrones podrían no transferir bien a live.

6. **Fair value depende de momentum**: No es una señal independiente — si momentum es 0, fair_value también tiende a 0. Son parcialmente redundantes.

7. **Config muerta**: `kelly_fraction`, `min_estimated_winrate`, `max_estimated_winrate`, `daily_profit_target_pct`, `circuit_breaker_hours` están en config pero no se usan en código.
