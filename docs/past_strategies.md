# Polybot — Estrategias Pasadas

## 1. Composite Signal (Mar 2-8, 2025)

**Modelo**: Señal compuesta ponderada de 4 fuentes
- Momentum (60%): tanh(0.5·ret_1m + 0.3·ret_5m + 0.2·EMA_cross)
- RSI (15%): (RSI_14 - 50) / 50
- Vol Regime (15%): -(ATR_5/ATR_20 - 1) × 2
- Book Skew (10%): -orderbook_imbalance (inverted)

**Filtros**: |composite| in [threshold, max_signal], trend filter (EMA alignment)

**Config final**:
- Entry price: [0.46, 0.65]
- Trade threshold: 0.10
- Trend filter: ON
- Kelly fraction: 0.5
- Invert UP signal: OFF

**Resultados**: 280 trades reales
- Win Rate: 47.1%
- PnL total: -$137.35
- Breakeven necesario: ~55% (con 10% taker fee)

**Por qué no funcionó**:
- WR 47% no supera breakeven de 55%
- Signal 0.15-0.25 peor que random (24-32% WR)
- Late entry (90s restantes) = precios 80-99¢ = sin edge
- Solo combo rentable: entry ~50¢ + signal ≥0.30 (56% WR), pero muy pocos trades

**Archivo**: `bot/signals.py` (no importado, mantenido como referencia)

---

## 2. Fair Value v1 — Paper Trading (Mar 8-9, 2025)

**Modelo**: P(up) = Φ(μ/σ) desde volatilidad realizada
- σ_5m = std de retornos 5-candle (últimas 60 ventanas ≈ 1hr)
- μ_5m = EMA de retornos recientes + momentum live 1-min (30% blend)
- Edge = P(side) - market_price × 1.10

**Config**: Sin filtro de edge band, sin filtro de vol, prob cap en 0.95

**Resultados**: 144 paper trades
- Win Rate: 59% overall (+$18.05 simulado)
- Edge 5-10%: 71% WR, +$12.91 (sweet spot)
- Edge >15%: 53% WR — modelo sobreconfiado
- P(est) >75%: 46% WR, -$4.80 — overconfidence penalty
- Down tiene más edge que Up ($13.46 vs $4.59)

**Insights que llevaron a v2**:
- Edge 4-8% tight: 73% WR, +$15.71 (37 trades)
- Vol medium (0.1-0.2%): 62% WR — con vol baja no hay señal, alta = caos
- Down + edge 4-8% + mkt 40-55¢: 83% WR (12 trades)
