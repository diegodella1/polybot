# Polybot — Estrategia de Trading

Bot automatizado para mercados binarios de 5 minutos "Bitcoin Up or Down" en Polymarket.

---

## Goal

**$42.71 → $1,000 en 30 días** (desde 6 de marzo 2026)

Requiere ~11% diario promedio con compounding. Basado en el primer día completo: 72% WR, +$13.40 (+31%).

---

## Mercado Objetivo

- **Producto**: Polymarket BTC Up/Down 5-minute binary markets
- **Payout**: Binario — cada share vale $1 si gana, $0 si pierde
- **Frecuencia**: Un mercado nuevo cada 5 minutos, 24/7
- **Fee**: 10% taker en entrada, sin fee en redemption
- **Settlement**: Polygon (USDC.e)

---

## Señal Compuesta

Señal compuesta ∈ [-1, 1] que agrega 3 componentes activos:

### 1. Momentum (peso: 0.70)

Dirección de corto plazo del BTC basado en klines de 1 minuto de Binance.

```
raw = 0.5 × ret_1m + 0.3 × ret_5m + 0.2 × (EMA5 - EMA15) / price
signal = tanh(raw × 150)
```

### 2. RSI (peso: 0.20)

14-period RSI normalizado a [-1, 1]: `(RSI - 50) / 50`

### 3. Book Skew (peso: 0.10)

Desequilibrio del orderbook en Polymarket: `(bid_vol - ask_vol) / total` sobre top 5 niveles.

### Agregación

```
composite = Σ(weight_i × value_i) / Σ(active_weights)
```

Solo componentes activos (no-None) ponderan. Trade ejecutable si `|composite| ≥ 0.10`.

Positivo → UP, Negativo → DOWN.

### RAG Pattern Matching

k-NN (k=10) sobre vector de 8 features (returns, EMA deviations, vol ratio, RSI, time remaining). Se auto-alimenta con cada trade resuelto. ~263 patrones acumulados.

---

## Risk Management

8 gates secuenciales — cualquier fallo bloquea el trade:

| # | Check | Condición de bloqueo |
|---|-------|---------------------|
| 1 | Bot habilitado | `enabled = false` |
| 2 | Señal mínima | `\|signal\| < 0.10` |
| 3 | Bankroll floor | `bankroll < $2` |
| 4 | Pérdidas consecutivas | `≥ 5 losses` → cooldown 1 round |
| 5 | Circuit breaker | `win_rate` debajo del mínimo sobre últimos 30 trades |
| 6 | Exposición máxima | `exposure ≥ $5` |
| 7 | Posición abierta | Solo 1 posición simultánea |
| 8 | Cooldown entre trades | Configurable (actualmente 0s) |

### Drawdown Multiplier

Reduce sizing progresivamente según pérdida diaria: de 1.0 (sin pérdida) a 0.25 (al límite).

---

## Position Sizing

Proporcional a la fuerza de la señal:

```
t = (|signal| - threshold) / (0.5 - threshold), clamped [0, 1]
fraction = base_pct + t × (max_pct - base_pct)
size_usd = bankroll × fraction × drawdown_multiplier
size_usd = clamp(size_usd, min_trade, bankroll × max_trade_pct)
shares = floor(size_usd / entry_price), min 5
```

---

## Filtros de Entrada

| Filtro | Valor | Razón |
|--------|-------|-------|
| Precio mínimo | 38¢ | Evitar tokens baratos |
| Precio máximo | 65¢ | Evitar tokens caros (bajo upside) |
| Spread máximo | 5¢ | Costo de ejecución tolerable |
| Tiempo restante mínimo | 30s | Suficiente para ejecutar |

---

## Ejecución

- **Tipo**: BUY limit order GTC
- **Precio**: best_ask + slippage (~3%, mín 2¢)
- **Verificación**: Poll cada 5s por 15s, cancela si no se llena
- **TP/SL**: Desactivado (double fee = 20% drag, no rinde)
- **Resolución**: Espera expiración binaria, auto-redeem ganadores

### Resolución Non-blocking

El monitoreo y resolución corren en background (`asyncio.create_task`). El loop principal sigue evaluando señales cada 5s.

- CLOB API: 4 retries × 10s
- On-chain fallback: 8 retries × 15s
- Recovery periódico: cada ~1 min
- Botón manual "unclog" en dashboard

---

## Flujo de un Round (~5 segundos)

```
DISCOVER → ¿hay mercado activo?
  ↓ sí
SIGNAL → calcular composite (momentum, RSI, book skew)
  ↓ |composite| ≥ 0.10
RISK → pasar 8 checks
  ↓ todo OK
SIZE → calcular shares según señal + drawdown
  ↓ filtros de precio/spread OK
EXECUTE → colocar orden, esperar fill
  ↓ filled (background task)
MONITOR → esperar resolución del mercado
  ↓
RESOLVE → calcular P&L, auto-redeem, RAG store
```

---

## Parámetros Actuales (6 marzo 2026)

| Parámetro | Valor |
|-----------|-------|
| Bankroll | ~$42.71 USDC.e |
| Dry run | No (live trading) |
| Trade threshold | 0.10 |
| Max trade | 20% del bankroll |
| Base trade | 7% del bankroll |
| Min trade | $2.50 |
| TP/SL | Desactivado |
| Daily loss limit | 50% |
| Circuit breaker | 30 trades window |
| Cooldown | 0s |
| Polling | 5 segundos |

---

## Performance (6 marzo 2026)

| Métrica | Valor |
|---------|-------|
| Hoy | 13/18 (72% WR), +$13.40 |
| Last 20 | 12/20 (60% WR) |
| Overall | 40/110 (36% WR) |
| RAG patterns | 263 |

### Insights

- Señales > 0.14 tienen mejor WR que señales 0.10-0.13
- Momentum (70%) es el driver principal
- TP/SL con 10% fee en cada leg no rinde — mejor esperar resolución binaria
- CLOB API rara vez reporta `closed: true` — on-chain es el fallback confiable
- Resolución puede tardar ~2 min on-chain después del cierre del mercado

---

## Infraestructura

| Componente | Detalle |
|------------|---------|
| Runtime | Python 3.11 + asyncio |
| Hardware | Raspberry Pi 5, 8GB RAM |
| Data BTC | Binance WebSocket (klines 1m) |
| Data Polymarket | WebSocket + REST fallback |
| Ejecución | py-clob-client, EOA mode |
| DB | SQLite local |
| Dashboard | FastAPI + WebSocket + HTML/JS (PWA) |
| Notificaciones | Telegram |
| Deploy | systemd service |
