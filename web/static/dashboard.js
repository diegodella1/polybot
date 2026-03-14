// Polybot Dashboard — v2

const API = '';
let ws = null;
let wsRetries = 0;
let equityChart = null;
let allTrades = [];       // full trade list for chart filtering
let chartRange = '7d';    // current chart filter
let tradeMode = 'all';   // paper | live | all
let prevBtcPrice = null;

let roundCount = 0;
let statsDateFilter = 'all'; // 'all' | 'today' | 'YYYY-MM-DD'
let lastStatusData = null;   // cache for re-rendering filtered stats

// Goal config
const GOAL_TARGET = 1000;
const GOAL_START_BALANCE = 42.71;
const GOAL_START_DATE = new Date('2026-03-06T00:00:00Z');
const GOAL_DAYS = 30;

function updateGoal(currentBalance) {
    const now = new Date();
    const elapsed = (now - GOAL_START_DATE) / (1000 * 60 * 60 * 24);
    const daysLeft = Math.max(0, Math.ceil(GOAL_DAYS - elapsed));
    const pct = Math.min(100, Math.max(0, ((currentBalance - GOAL_START_BALANCE) / (GOAL_TARGET - GOAL_START_BALANCE)) * 100));

    const fill = document.getElementById('goalFill');
    const detail = document.getElementById('goalDetail');
    const days = document.getElementById('goalDays');

    if (fill) fill.style.width = pct.toFixed(1) + '%';
    if (detail) detail.textContent = `$${currentBalance.toFixed(2)} / $${GOAL_TARGET} (${pct.toFixed(1)}%)`;
    if (days) days.textContent = daysLeft > 0 ? `${daysLeft}d restantes` : 'Plazo cumplido';
}

// --- Init ---
// Register service worker for PWA
if ('serviceWorker' in navigator) {
    navigator.serviceWorker.register('/static/sw.js').catch(() => {});
}

document.addEventListener('DOMContentLoaded', () => {
    initChart();
    fetchStatus();
    fetchTrades();
    fetchPaperTrades();
    connectWS();
    fetchBtcPrice();
    setInterval(fetchStatus, 5000);
    setInterval(fetchTrades, 15000);
    setInterval(fetchPaperTrades, 15000);
    setInterval(fetchBtcPrice, 15000);

    // Chart range filters
    document.getElementById('chartFilters').addEventListener('click', (e) => {
        const btn = e.target.closest('.chart-filter-btn');
        if (!btn) return;
        document.querySelectorAll('#chartFilters .chart-filter-btn').forEach(b => b.classList.remove('active'));
        btn.classList.add('active');
        chartRange = btn.dataset.range;
        updateEquityChart(allTrades);
    });

    // Trade mode filters (All / Paper / Live)
    document.getElementById('modeFilters').addEventListener('click', (e) => {
        const btn = e.target.closest('.chart-filter-btn');
        if (!btn) return;
        document.querySelectorAll('#modeFilters .chart-filter-btn').forEach(b => b.classList.remove('active'));
        btn.classList.add('active');
        tradeMode = btn.dataset.mode;
        fetchTrades();
    });

    // Stats date filter
    const statsFilterWrap = document.querySelector('.stats-date-filter');
    const statsDateInput = document.getElementById('statsDateInput');
    if (statsFilterWrap) {
        statsFilterWrap.addEventListener('click', (e) => {
            const btn = e.target.closest('[data-stats]');
            if (!btn) return;
            statsFilterWrap.querySelectorAll('.chart-filter-btn').forEach(b => b.classList.remove('active'));
            btn.classList.add('active');
            const mode = btn.dataset.stats;
            if (mode === 'all') {
                statsDateFilter = 'all';
                statsDateInput.style.display = 'none';
                recalcFilteredStats();
            } else if (mode === 'today') {
                statsDateFilter = 'today';
                statsDateInput.style.display = 'none';
                recalcFilteredStats();
            } else if (mode === 'custom') {
                statsDateInput.style.display = '';
                if (statsDateInput.value) {
                    statsDateFilter = statsDateInput.value;
                    recalcFilteredStats();
                }
            }
        });
        statsDateInput.addEventListener('change', () => {
            if (statsDateInput.value) {
                statsDateFilter = statsDateInput.value;
                recalcFilteredStats();
            }
        });
    }
});

// --- WebSocket with reconnect backoff ---
function connectWS() {
    const proto = location.protocol === 'https:' ? 'wss' : 'ws';
    ws = new WebSocket(`${proto}://${location.host}/api/trades/live`);

    ws.onopen = () => {
        wsRetries = 0;
    };

    ws.onmessage = (e) => {
        const msg = JSON.parse(e.data);
        if (msg.event === 'trade') {
            if (msg.data.event_type === 'position_update') {
                handlePositionUpdate(msg.data);
            } else {
                handleTrade(msg.data);
            }
        }
        if (msg.event === 'signal') handleSignal(msg.data);
        if (msg.event === 'status') handleStatus(msg.data);
        if (msg.event === 'round_update') handleRoundUpdate(msg.data);
    };

    ws.onclose = () => {
        const delay = Math.min(1000 * Math.pow(2, wsRetries), 30000);
        wsRetries++;
        setTimeout(connectWS, delay);
    };

    ws.onerror = () => ws.close();
}

// --- BTC Price ---
async function fetchBtcPrice() {
    try {
        const res = await fetch('https://api.binance.com/api/v3/ticker/24hr?symbol=BTCUSDT');
        const data = await res.json();
        const price = parseFloat(data.lastPrice);
        const changePct = parseFloat(data.priceChangePercent);

        const priceEl = document.getElementById('btcPrice');
        const changeEl = document.getElementById('btcChange');

        priceEl.textContent = '$' + price.toLocaleString('en-US', { maximumFractionDigits: 0 });

        const arrow = changePct >= 0 ? '\u25b2' : '\u25bc';
        changeEl.textContent = `${arrow}${Math.abs(changePct).toFixed(1)}%`;
        changeEl.className = 'change ' + (changePct >= 0 ? 'up' : 'down');

        // Flash on price change
        if (prevBtcPrice !== null && prevBtcPrice !== price) {
            priceEl.style.transition = 'none';
            priceEl.style.color = price > prevBtcPrice ? 'var(--green)' : 'var(--red)';
            requestAnimationFrame(() => {
                requestAnimationFrame(() => {
                    priceEl.style.transition = 'color 1s';
                    priceEl.style.color = 'var(--text)';
                });
            });
        }
        prevBtcPrice = price;
    } catch (e) {
        // silently fail
    }
}

// --- Fetch ---
async function fetchStatus() {
    try {
        const res = await fetch(`${API}/api/status`);
        const data = await res.json();
        updateStatus(data);
    } catch (e) {
        console.error('Status fetch failed:', e);
    }
}

async function fetchTrades() {
    try {
        const res = await fetch(`${API}/api/trades?limit=500&mode=${tradeMode}`);
        const trades = await res.json();
        allTrades = trades;
        renderTrades(trades, false);
        updateEquityChart(trades);
        if (statsDateFilter !== 'all') recalcFilteredStats();
    } catch (e) {
        console.error('Trades fetch failed:', e);
    }
}

// --- Animated counter ---
function animateValue(el, end, prefix, suffix, decimals) {
    const start = parseFloat(el.dataset.current || '0');
    const duration = 600;
    const startTime = performance.now();

    function tick(now) {
        const elapsed = now - startTime;
        const progress = Math.min(elapsed / duration, 1);
        // ease out cubic
        const eased = 1 - Math.pow(1 - progress, 3);
        const current = start + (end - start) * eased;

        el.textContent = (prefix || '') + current.toFixed(decimals) + (suffix || '');

        if (progress < 1) {
            requestAnimationFrame(tick);
        } else {
            el.dataset.current = String(end);
        }
    }
    requestAnimationFrame(tick);
}

// --- Update UI ---
function updateStatus(data) {
    // Sync trade threshold from config
    // Status badge
    const badge = document.getElementById('statusBadge');
    const text = document.getElementById('statusText');

    if (data.running) {
        const isLive = !data.dry_run;
        badge.className = isLive ? 'status-badge running-live' : 'status-badge running';
        text.textContent = isLive ? 'LIVE \u00b7 REAL $' : 'RUNNING \u00b7 DRY RUN';
    } else {
        badge.className = 'status-badge stopped';
        text.textContent = 'STOPPED';
    }

    // Training mode banner
    const tb = document.getElementById('trainingBanner');
    if (tb) tb.style.display = data.dry_run ? 'block' : 'none';

    // Paper validation badge
    const paperBadge = document.getElementById('paperStatus');
    if (paperBadge) {
        if (!data.running) {
            paperBadge.textContent = 'OFFLINE';
            paperBadge.className = 'badge badge-paper';
        } else if (data.dry_run) {
            paperBadge.textContent = 'PAPER ONLY';
            paperBadge.className = 'badge badge-paper';
        } else {
            paperBadge.textContent = 'SHADOW';
            paperBadge.className = 'badge badge-paper';
            paperBadge.style.color = 'var(--text-secondary)';
        }
    }

    // Wallet Real (on-chain balance — the real number)
    const bankroll = data.bankroll || 0;
    const walletBal = data.wallet_balance;
    const realBal = walletBal != null ? walletBal : bankroll;
    const initialDeposit = data.initial_deposit || bankroll;
    const bankrollOpen = data.bankroll_open || initialDeposit;

    const walletEl = document.getElementById('walletTotal');
    animateValue(walletEl, realBal, '$', '', 2);

    const unredeemed = data.unredeemed_value || 0;
    const depositLine = `Invertido: $${initialDeposit.toFixed(2)}`;
    const unredeemedLine = unredeemed > 0 ? ` · Pendiente: $${unredeemed.toFixed(2)}` : '';
    document.getElementById('walletDeposit').textContent = depositLine + unredeemedLine;

    // Goal tracker
    updateGoal(realBal);

    // Daily PnL
    const dailyPnl = data.daily_pnl || 0;
    const pnlEl = document.getElementById('dailyPnl');
    const sign = dailyPnl >= 0 ? '+' : '';
    animateValue(pnlEl, dailyPnl, sign + '$', '', 2);
    pnlEl.className = `card-value ${dailyPnl >= 0 ? 'positive' : 'negative'}`;

    if (bankrollOpen > 0) {
        const pct = (dailyPnl / bankrollOpen * 100);
        const pctEl = document.getElementById('dailyPnlPct');
        pctEl.textContent = `${dailyPnl >= 0 ? '+' : ''}${pct.toFixed(1)}% · Open: $${bankrollOpen.toFixed(2)}`;
    }

    // Fees transparency
    const fees = data.daily_fees || 0;
    const feesEl = document.getElementById('dailyFees');
    if (feesEl && fees > 0) {
        const gross = dailyPnl + fees;
        feesEl.textContent = `Bruto: +$${gross.toFixed(2)} · Fees: -$${fees.toFixed(2)}`;
    }

    // Net Profit — only update from status when filter is "all"
    lastStatusData = data;
    if (statsDateFilter === 'all') {
        const netProfit = realBal - initialDeposit;
        const netEl = document.getElementById('netProfit');
        const netSign = netProfit >= 0 ? '+' : '';
        animateValue(netEl, netProfit, netSign + '$', '', 2);
        netEl.className = `card-value ${netProfit >= 0 ? 'positive' : 'negative'}`;
        const roi = initialDeposit > 0 ? (netProfit / initialDeposit * 100) : 0;
        document.getElementById('netProfitSub').textContent = `ROI: ${roi >= 0 ? '+' : ''}${roi.toFixed(1)}%`;
    }

    // Risk
    const cl = data.consecutive_losses || 0;
    const streakEl = document.getElementById('riskStreak');
    streakEl.textContent = cl > 0 ? `${cl} losses` : '0';
    if (cl >= 3) streakEl.style.color = 'var(--red)';
    else streakEl.style.color = '';

    // Daily loss bar
    const dailyLoss = Math.abs(Math.min(data.daily_pnl || 0, 0));
    const dailyLossLimit = bankroll * (data.daily_loss_limit_pct || 0.15);
    const lossEl = document.getElementById('riskDailyLoss');
    lossEl.textContent = dailyLoss > 0 ? `-$${dailyLoss.toFixed(2)}` : '$0.00';
    if (dailyLossLimit > 0) {
        const pct = Math.min((dailyLoss / dailyLossLimit) * 100, 100);
        const bar = document.getElementById('riskDailyBar');
        bar.style.width = pct + '%';
        bar.className = 'progress-fill' + (pct > 80 ? ' danger' : pct > 50 ? ' warning' : '');
    }

    // Drawdown multiplier
    const ddEl = document.getElementById('riskDrawdown');
    if (ddEl && data.drawdown_multiplier != null) {
        const mult = data.drawdown_multiplier;
        ddEl.textContent = `${mult.toFixed(2)}x`;
        ddEl.style.color = mult >= 0.9 ? 'var(--green)' : mult >= 0.5 ? 'var(--yellow)' : 'var(--red)';
        const ddIcon = ddEl.closest('.risk-item')?.querySelector('.risk-icon');
        if (ddIcon) ddIcon.className = 'risk-icon ' + (mult >= 0.9 ? 'green' : mult >= 0.5 ? 'yellow' : 'red');
    }

    // Cooldown
    const cdEl = document.getElementById('riskCooldown');
    cdEl.textContent = data.cooldown_remaining > 0 ? `${data.cooldown_remaining} rounds` : 'No';
    if (data.cooldown_remaining > 0) {
        cdEl.style.color = 'var(--yellow)';
    } else {
        cdEl.style.color = '';
    }

    // Circuit breaker
    const cbEl = document.getElementById('riskCircuit');
    if (data.circuit_breaker) {
        cbEl.textContent = 'TRIPPED';
        cbEl.style.color = 'var(--red)';
        cbEl.closest('.risk-item').querySelector('.risk-icon').className = 'risk-icon red';
    } else {
        cbEl.textContent = 'OK';
        cbEl.style.color = 'var(--green)';
    }

    // Trade cooldown
    const tcdEl = document.getElementById('riskTradeCooldown');
    if (tcdEl) {
        const cd = data.trade_cooldown_seconds || 0;
        tcdEl.textContent = cd > 0 ? `${Math.floor(cd / 60)}m` : 'Off';
        tcdEl.style.color = cd > 0 ? 'var(--accent)' : 'var(--text-dim)';
    }

    // Signals
    if (data.signals) handleSignal(data.signals);
}

// --- Signals (Fair Value) ---
function handleSignal(sig) {
    if (!sig) return;

    // Side indicator
    const sideEl = document.getElementById('fvSide');
    if (sideEl) {
        if (sig.has_edge && sig.side) {
            sideEl.textContent = sig.side.toUpperCase();
            sideEl.className = 'fv-side ' + sig.side;
        } else {
            sideEl.textContent = '\u2014';
            sideEl.className = 'fv-side none';
        }
    }

    // Edge value
    const edgeEl = document.getElementById('fvEdge');
    if (edgeEl) {
        const pct = (sig.edge * 100).toFixed(1);
        edgeEl.textContent = sig.edge > 0 ? `+${pct}%` : `${pct}%`;
        edgeEl.style.color = sig.has_edge ? 'var(--green)' : sig.edge > 0 ? 'var(--yellow)' : 'var(--text-dim)';
    }

    // Explanation
    const explEl = document.getElementById('fvExplanation');
    if (explEl) {
        if (sig.has_edge && sig.side) {
            explEl.textContent = `Tradeable: P(${sig.side}) supera breakeven + fee`;
            explEl.style.color = 'var(--green)';
        } else if (sig.edge > 0) {
            explEl.textContent = `Edge insuficiente (fuera de banda 4-8%)`;
            explEl.style.color = 'var(--yellow)';
        } else {
            explEl.textContent = 'Sin edge detectado';
            explEl.style.color = 'var(--text-dim)';
        }
    }

    // Probability bars
    const probUp = sig.prob_up || 0.5;
    const probDown = sig.prob_down || 0.5;
    setMetric('fvProbUp', (probUp * 100).toFixed(1) + '%', probUp > 0.55 ? 'var(--green)' : null);
    setMetric('fvProbDown', (probDown * 100).toFixed(1) + '%', probDown > 0.55 ? 'var(--red)' : null);
    setBar('fvProbUpBar', probUp * 100);
    setBar('fvProbDownBar', probDown * 100);

    // Vol and drift
    setMetric('fvVol', sig.vol_5m != null ? (sig.vol_5m * 100).toFixed(4) + '%' : '\u2014');
    setMetric('fvDrift', sig.drift_5m != null ? (sig.drift_5m > 0 ? '+' : '') + (sig.drift_5m * 100).toFixed(4) + '%' : '\u2014',
        sig.drift_5m > 0 ? 'var(--green)' : sig.drift_5m < 0 ? 'var(--red)' : null);

    // Market prices
    setMetric('fvMktUp', sig.price_up != null ? (sig.price_up * 100).toFixed(1) + '\u00a2' : '\u2014');
    setMetric('fvMktDown', sig.price_down != null ? (sig.price_down * 100).toFixed(1) + '\u00a2' : '\u2014');
}

function setMetric(id, text, color) {
    const el = document.getElementById(id);
    if (!el) return;
    el.textContent = text;
    if (color) el.style.color = color;
    else el.style.color = '';
}

function setBar(id, pct) {
    const el = document.getElementById(id);
    if (el) el.style.width = Math.min(Math.max(pct, 0), 100) + '%';
}


// --- Live Status Panel ---
function handleRoundUpdate(data) {
    roundCount++;

    const time = data.timestamp
        ? new Date(data.timestamp).toLocaleTimeString('en-US', { hour12: false, hour: '2-digit', minute: '2-digit', second: '2-digit' })
        : '--:--:--';

    const decision = data.decision || 'skip';
    const stateMap = { trade: 'TRADING', wait: 'WAITING', skip: 'SCANNING' };
    const colorMap = { trade: 'var(--green)', wait: 'var(--text-dim)', skip: 'var(--yellow)' };

    const el = (id) => document.getElementById(id);

    const stateEl = el('lsState');
    if (stateEl) {
        stateEl.textContent = stateMap[decision] || 'SCANNING';
        stateEl.style.color = colorMap[decision] || 'var(--text-primary)';
    }

    const marketEl = el('lsMarket');
    if (marketEl) marketEl.textContent = data.market || '—';

    const signalEl = el('lsSignal');
    if (signalEl) {
        if (data.signal != null) {
            const s = data.signal;
            signalEl.textContent = (s > 0 ? '+' : '') + s.toFixed(3);
            signalEl.style.color = s > 0 ? 'var(--green)' : s < 0 ? 'var(--red)' : 'var(--text-dim)';
        } else {
            signalEl.textContent = '—';
            signalEl.style.color = 'var(--text-dim)';
        }
    }

    const decisionEl = el('lsDecision');
    if (decisionEl) {
        const badge = decision === 'trade' ? 'TRADE' : decision === 'wait' ? 'WAIT' : 'SKIP';
        const reason = data.reason || '';
        decisionEl.innerHTML = `<span style="color:${colorMap[decision]};font-weight:700;margin-right:6px">${badge}</span>${reason}`;
    }

    const roundsEl = el('lsRounds');
    if (roundsEl) roundsEl.textContent = roundCount;

    const timeEl = el('lsTime');
    if (timeEl) timeEl.textContent = time;

    const pulse = el('livePulse');
    if (pulse) pulse.classList.add('active');
}

// --- Trades ---
function handleTrade(data) {
    fetchStatus();
    fetchTrades();
}

function handleStatus(data) {
    fetchStatus();
}

function renderTrades(trades, flash) {
    const body = document.getElementById('tradesBody');
    body.innerHTML = '';

    // Win rate — use filtered trades if filter is active
    renderWinRate(statsDateFilter === 'all' ? trades : getFilteredTrades(trades));

    for (let i = 0; i < Math.min(trades.length, 20); i++) {
        const t = trades[i];
        const tr = document.createElement('tr');

        if (flash && i === 0) {
            tr.classList.add('flash');
        }

        const time = t.timestamp
            ? new Date(t.timestamp).toLocaleTimeString('en-US', { hour12: false, hour: '2-digit', minute: '2-digit' })
            : '';

        const sideClass = t.side === 'up' ? 'side-up' : 'side-down';
        const sideLabel = t.side === 'up' ? 'UP \u25b2' : 'DOWN \u25bc';
        const modeBadge = t.dry_run ? '<span class="badge badge-paper">PAPER</span>' : '<span class="badge badge-live-tag">LIVE</span>';

        let resultHtml;
        if (t.outcome === 'win' || t.outcome === 'take_profit') {
            const label = t.outcome === 'take_profit' ? 'TP' : '';
            resultHtml = `<span class="badge badge-win">${label} +$${(t.pnl || 0).toFixed(2)}</span>`;
        } else if (t.outcome === 'loss' || t.outcome === 'stop_loss') {
            const label = t.outcome === 'stop_loss' ? 'SL' : '';
            resultHtml = `<span class="badge badge-loss">${label} -$${Math.abs(t.pnl || 0).toFixed(2)}</span>`;
        } else if (!t.outcome) {
            resultHtml = `<span class="badge badge-pending badge-unclog" title="Click to resolve" onclick="unclogTrades(event)">PENDING</span>`;
        } else {
            resultHtml = `<span class="badge badge-pending">${t.outcome.toUpperCase()}</span>`;
        }

        const btcLabel = t.btc_price ? `$${Number(t.btc_price).toLocaleString('en-US', {maximumFractionDigits: 0})}` : '—';

        tr.dataset.tradeId = t.id || '';
        tr.innerHTML = `
            <td>${time}</td>
            <td>${(t.signal_score || 0).toFixed(2)}</td>
            <td class="${sideClass}">${sideLabel} ${modeBadge}</td>
            <td>$${(t.size_usd || 0).toFixed(2)}</td>
            <td>${(t.entry_price || 0).toFixed(2)}\u00a2</td>
            <td>${btcLabel}</td>
            <td class="result-cell">${resultHtml}</td>
        `;
        body.appendChild(tr);
    }
}

// --- Unclog Pending Trades ---
async function unclogTrades(e) {
    if (e) e.stopPropagation();
    // Find all pending badges and show spinner
    document.querySelectorAll('.badge-unclog').forEach(b => {
        b.textContent = '...';
        b.style.pointerEvents = 'none';
    });
    try {
        const resp = await fetch('/api/trades/resolve-pending', { method: 'POST' });
        if (resp.status === 401) {
            // Need admin login — redirect
            window.location.href = '/admin';
            return;
        }
        const data = await resp.json();
        if (data.remaining_pending === 0) {
            fetchTrades();
        } else {
            document.querySelectorAll('.badge-unclog').forEach(b => {
                b.textContent = 'PENDING';
                b.style.pointerEvents = '';
            });
        }
    } catch (err) {
        document.querySelectorAll('.badge-unclog').forEach(b => {
            b.textContent = 'PENDING';
            b.style.pointerEvents = '';
        });
    }
}

// --- Live Position Update ---
function handlePositionUpdate(data) {
    const { trade_id, current_bid, unrealized_pnl, unrealized_pct, elapsed, wait_seconds } = data;
    // Find the row for this trade
    const rows = document.querySelectorAll('#tradesBody tr');
    for (const row of rows) {
        if (row.dataset.tradeId == trade_id) {
            const cell = row.querySelector('.result-cell');
            if (!cell) break;
            const isPositive = unrealized_pnl >= 0;
            const badgeClass = isPositive ? 'badge-win' : 'badge-loss';
            const sign = isPositive ? '+' : '';
            cell.innerHTML = `<span class="badge ${badgeClass} badge-live">${sign}${unrealized_pct}%</span>`;
            break;
        }
    }
}

// --- Chart ---
function initChart() {
    const ctx = document.getElementById('equityChart').getContext('2d');

    // Gradient fill
    const gradient = ctx.createLinearGradient(0, 0, 0, 260);
    gradient.addColorStop(0, 'rgba(139, 92, 246, 0.25)');
    gradient.addColorStop(1, 'rgba(139, 92, 246, 0.02)');

    equityChart = new Chart(ctx, {
        type: 'line',
        data: {
            labels: [],
            datasets: [{
                label: 'Bankroll',
                data: [],
                borderColor: '#8b5cf6',
                backgroundColor: gradient,
                fill: true,
                tension: 0.4,
                pointRadius: 0,
                pointHitRadius: 8,
                pointHoverRadius: 4,
                pointHoverBackgroundColor: '#8b5cf6',
                pointHoverBorderColor: '#fff',
                pointHoverBorderWidth: 2,
                borderWidth: 2,
                yAxisID: 'y',
            }, {
                label: 'Win Rate',
                data: [],
                borderColor: '#22d3ee',
                backgroundColor: 'transparent',
                fill: false,
                tension: 0.4,
                pointRadius: 0,
                pointHitRadius: 8,
                pointHoverRadius: 4,
                pointHoverBackgroundColor: '#22d3ee',
                pointHoverBorderColor: '#fff',
                pointHoverBorderWidth: 2,
                borderWidth: 1.5,
                borderDash: [4, 3],
                yAxisID: 'yWR',
            }]
        },
        options: {
            responsive: true,
            maintainAspectRatio: false,
            interaction: {
                intersect: false,
                mode: 'index',
            },
            plugins: {
                legend: {
                    display: true,
                    labels: {
                        color: '#6a6a82',
                        font: { family: "'Inter', sans-serif", size: 10 },
                        boxWidth: 12,
                        padding: 12,
                    },
                },
                tooltip: {
                    backgroundColor: 'rgba(13,13,22,0.9)',
                    borderColor: 'rgba(255,255,255,0.1)',
                    borderWidth: 1,
                    titleFont: { family: "'Inter', sans-serif", size: 11 },
                    bodyFont: { family: "'SF Mono', monospace", size: 12 },
                    padding: 10,
                    displayColors: true,
                    callbacks: {
                        label: (ctx) => {
                            if (ctx.datasetIndex === 0) return `Bankroll: $${ctx.parsed.y.toFixed(2)}`;
                            return `Win Rate: ${ctx.parsed.y.toFixed(0)}%`;
                        }
                    }
                }
            },
            scales: {
                x: {
                    display: true,
                    ticks: {
                        color: '#6a6a82',
                        font: { family: "'Inter', sans-serif", size: 10 },
                        maxTicksLimit: 8,
                    },
                    grid: { color: 'rgba(255,255,255,0.03)' },
                    border: { display: false },
                },
                y: {
                    display: true,
                    position: 'left',
                    ticks: {
                        color: '#8b5cf6',
                        font: { family: "'SF Mono', monospace", size: 10 },
                        callback: v => `$${v}`,
                        maxTicksLimit: 5,
                    },
                    grid: { color: 'rgba(255,255,255,0.03)' },
                    border: { display: false },
                },
                yWR: {
                    display: true,
                    position: 'right',
                    min: 0,
                    max: 100,
                    ticks: {
                        color: '#22d3ee',
                        font: { family: "'SF Mono', monospace", size: 10 },
                        callback: v => `${v}%`,
                        maxTicksLimit: 5,
                    },
                    grid: { display: false },
                    border: { display: false },
                }
            }
        }
    });
}

function filterByRange(trades, range) {
    if (range === 'all') return trades;
    const now = Date.now();
    const ms = { '1h': 3600e3, '6h': 6*3600e3, '1d': 86400e3, '7d': 7*86400e3, '30d': 30*86400e3 };
    const cutoff = now - (ms[range] || 3600e3);
    return trades.filter(t => new Date(t.timestamp).getTime() >= cutoff);
}

function updateEquityChart(trades) {
    if (!equityChart || !trades.length) return;

    const filtered = filterByRange(trades, chartRange);
    const resolved = filtered
        .filter(t => t.bankroll_after != null)
        .sort((a, b) => new Date(a.timestamp) - new Date(b.timestamp));
    if (!resolved.length) return;

    // Cumulative win rate (within visible range)
    let wins = 0;
    const winRates = resolved.map((t, i) => {
        if (t.pnl > 0) wins++;
        return (wins / (i + 1)) * 100;
    });

    // Format labels based on range
    const useDateLabel = chartRange === '7d' || chartRange === '30d' || chartRange === 'all';
    equityChart.data.labels = resolved.map(t => {
        const d = new Date(t.timestamp);
        if (useDateLabel) {
            return d.toLocaleDateString('en-US', { month: 'short', day: 'numeric' })
                + ' ' + d.toLocaleTimeString('en-US', { hour12: false, hour: '2-digit', minute: '2-digit' });
        }
        return d.toLocaleTimeString('en-US', { hour12: false, hour: '2-digit', minute: '2-digit' });
    });
    equityChart.data.datasets[0].data = resolved.map(t => t.bankroll_after);
    equityChart.data.datasets[1].data = winRates;
    equityChart.update('none');
}

// --- Stats Date Filter helpers ---
function getStatsFilterCutoff() {
    if (statsDateFilter === 'all') return null;
    if (statsDateFilter === 'today') {
        const now = new Date();
        return new Date(now.getFullYear(), now.getMonth(), now.getDate());
    }
    // Custom date string 'YYYY-MM-DD'
    return new Date(statsDateFilter + 'T00:00:00');
}

function getFilteredTrades(trades) {
    const cutoff = getStatsFilterCutoff();
    if (!cutoff) return trades;
    return trades.filter(t => t.timestamp && new Date(t.timestamp) >= cutoff);
}

function renderWinRate(trades) {
    const resolved = trades.filter(t => t.outcome);
    const wins = resolved.filter(t => t.outcome === 'win' || t.outcome === 'take_profit').length;
    const total = resolved.length;

    if (total > 0) {
        const wr = (wins / total * 100);
        const wrEl = document.getElementById('winRate');
        animateValue(wrEl, wr, '', '%', 1);
        document.getElementById('winRateSub').textContent = `${wins}W / ${total - wins}L · ${total} trades`;
    } else {
        document.getElementById('winRate').textContent = '\u2014';
        document.getElementById('winRateSub').textContent = '0 trades';
    }
}

// --- Paper Trades (Fair Value) ---
async function fetchPaperTrades() {
    try {
        const res = await fetch(`${API}/api/paper-trades?limit=50`);
        const data = await res.json();
        renderPaperTrades(data);
    } catch (e) {
        console.error('Paper trades fetch failed:', e);
    }
}

function renderPaperTrades(data) {
    const { trades, summary } = data;

    // Stats
    const el = (id) => document.getElementById(id);
    el('paperTotal').textContent = summary.total;

    if (summary.total > 0) {
        const wr = (summary.win_rate * 100).toFixed(1) + '%';
        el('paperWR').textContent = wr;
        el('paperWR').style.color = summary.win_rate >= 0.55 ? 'var(--green)' : summary.win_rate >= 0.45 ? 'var(--yellow)' : 'var(--red)';
    } else {
        el('paperWR').textContent = '--';
        el('paperWR').style.color = '';
    }

    const pnl = summary.total_pnl || 0;
    el('paperPnl').textContent = (pnl >= 0 ? '+' : '') + '$' + pnl.toFixed(2);
    el('paperPnl').style.color = pnl >= 0 ? 'var(--green)' : 'var(--red)';

    el('paperPending').textContent = summary.pending;

    // Table
    const body = el('paperBody');
    body.innerHTML = '';
    for (const t of trades.slice(0, 15)) {
        const tr = document.createElement('tr');
        const time = t.timestamp
            ? new Date(t.timestamp).toLocaleTimeString('en-US', { hour12: false, hour: '2-digit', minute: '2-digit' })
            : '';
        const sideClass = t.side === 'up' ? 'side-up' : 'side-down';
        const sideLabel = t.side === 'up' ? 'UP' : 'DN';

        let resultHtml;
        if (t.outcome === 'win') {
            resultHtml = `<span class="badge badge-win">WIN</span>`;
        } else if (t.outcome === 'loss') {
            resultHtml = `<span class="badge badge-loss">LOSS</span>`;
        } else if (t.outcome === 'expired') {
            resultHtml = `<span class="badge badge-pending" style="opacity:0.4">EXP</span>`;
        } else {
            resultHtml = `<span class="badge badge-pending">...</span>`;
        }

        tr.innerHTML = `
            <td>${time}</td>
            <td class="${sideClass}">${sideLabel}</td>
            <td>${(t.prob_estimated * 100).toFixed(0)}%</td>
            <td>${(t.market_price * 100).toFixed(0)}\u00a2</td>
            <td style="color:var(--green)">+${(t.edge * 100).toFixed(1)}%</td>
            <td>${t.vol_5m ? (t.vol_5m * 100).toFixed(2) + '%' : '--'}</td>
            <td>${resultHtml}</td>
        `;
        body.appendChild(tr);
    }
}

function recalcFilteredStats() {
    if (!allTrades.length) return;
    const filtered = getFilteredTrades(allTrades);

    // Win rate
    renderWinRate(filtered);

    // Net profit
    const netEl = document.getElementById('netProfit');
    const netSub = document.getElementById('netProfitSub');

    if (statsDateFilter === 'all') {
        // Restore wallet-based net profit from last status
        if (lastStatusData) {
            const walletBal = lastStatusData.wallet_balance;
            const bankroll = lastStatusData.bankroll || 0;
            const realBal = walletBal != null ? walletBal : bankroll;
            const initialDeposit = lastStatusData.initial_deposit || bankroll;
            const netProfit = realBal - initialDeposit;
            const netSign = netProfit >= 0 ? '+' : '';
            animateValue(netEl, netProfit, netSign + '$', '', 2);
            netEl.className = `card-value ${netProfit >= 0 ? 'positive' : 'negative'}`;
            const roi = initialDeposit > 0 ? (netProfit / initialDeposit * 100) : 0;
            netSub.textContent = `ROI: ${roi >= 0 ? '+' : ''}${roi.toFixed(1)}%`;
        }
        return;
    }

    // Filtered: sum PnL from resolved trades
    const resolved = filtered.filter(t => t.outcome && t.pnl != null);
    const sumPnl = resolved.reduce((s, t) => s + t.pnl, 0);
    const netSign = sumPnl >= 0 ? '+' : '';
    animateValue(netEl, sumPnl, netSign + '$', '', 2);
    netEl.className = `card-value ${sumPnl >= 0 ? 'positive' : 'negative'}`;

    // Sub-text with context
    const cutoff = getStatsFilterCutoff();
    const label = statsDateFilter === 'today' ? 'Today' :
        `Since ${cutoff.toLocaleDateString('en-US', { month: 'short', day: 'numeric' })}`;
    netSub.textContent = `${label} · ${resolved.length} trades`;
}
