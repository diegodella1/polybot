// Polybot Dashboard — v2

const API = '';
let ws = null;
let wsRetries = 0;
let equityChart = null;
let prevBtcPrice = null;
let currentTradeThreshold = 0.06;
const activityLog = [];  // max 8 entries
const MAX_ACTIVITY = 8;

// --- Init ---
document.addEventListener('DOMContentLoaded', () => {
    initChart();
    fetchStatus();
    fetchTrades();
    connectWS();
    fetchBtcPrice();
    setInterval(fetchStatus, 10000);
    setInterval(fetchTrades, 15000);  // Refresh trades periodically (catches resolved PENDINGs)
    setInterval(fetchBtcPrice, 15000);
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
        const res = await fetch(`${API}/api/trades?limit=20`);
        const trades = await res.json();
        renderTrades(trades, false);
        updateEquityChart(trades);
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
    if (data.trade_threshold) currentTradeThreshold = data.trade_threshold;

    // Status badge
    const badge = document.getElementById('statusBadge');
    const text = document.getElementById('statusText');

    if (data.running) {
        badge.className = 'status-badge running';
        const mode = data.dry_run ? 'DRY RUN' : 'LIVE';
        text.textContent = `RUNNING \u00b7 ${mode}`;
    } else {
        badge.className = 'status-badge stopped';
        text.textContent = 'STOPPED';
    }

    // Bankroll
    const bankroll = data.bankroll || 0;
    const bankrollEl = document.getElementById('bankroll');
    animateValue(bankrollEl, bankroll, '$', '', 2);

    // Wallet balance (on-chain)
    const walletEl = document.getElementById('walletBalance');
    if (walletEl && data.wallet_balance != null) {
        walletEl.textContent = `Wallet: $${data.wallet_balance.toFixed(2)}`;
    }

    // Daily PnL
    const dailyPnl = data.daily_pnl || 0;
    const pnlEl = document.getElementById('dailyPnl');
    const sign = dailyPnl >= 0 ? '+' : '';
    animateValue(pnlEl, dailyPnl, sign + '$', '', 2);
    pnlEl.className = `card-value ${dailyPnl >= 0 ? 'positive' : 'negative'}`;

    if (bankroll > 0) {
        const pct = (dailyPnl / bankroll * 100);
        const pctEl = document.getElementById('dailyPnlPct');
        pctEl.textContent = `${dailyPnl >= 0 ? '+' : ''}${pct.toFixed(1)}% today`;
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

    // Invert UP signal flag
    const invertEl = document.getElementById('riskInvert');
    if (invertEl) {
        const on = data.invert_up_signal;
        invertEl.textContent = on ? 'ON' : 'OFF';
        invertEl.style.color = on ? 'var(--yellow)' : 'var(--text-dim)';
        const invertIcon = invertEl.closest('.risk-item')?.querySelector('.risk-icon');
        if (invertIcon) invertIcon.className = 'risk-icon ' + (on ? 'yellow' : 'green');
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

// --- Signals ---
function handleSignal(sig) {
    updateSignalBar('Mom', sig.momentum);
    updateSignalBar('Skew', sig.book_skew);
    updateSignalBar('FV', sig.fair_value);
    updateSignalBar('RAG', sig.rag_pattern);
    updateSignalBar('Sent', sig.sentiment);
    updateGauge(sig.composite);
    updateGaugeExplanation(sig.composite);
}

function updateSignalBar(id, value) {
    const fill = document.getElementById(`sig${id}`);
    const val = document.getElementById(`sig${id}Val`);
    if (!fill || !val) return;

    if (value == null) {
        val.textContent = '\u2014';
        val.style.color = 'var(--text-dim)';
        fill.style.width = '0';
        return;
    }

    val.textContent = value.toFixed(3);
    val.style.color = value >= 0 ? 'var(--green)' : 'var(--red)';

    const pct = Math.min(Math.abs(value) * 50, 50);
    fill.style.width = `${pct}%`;

    if (value >= 0) {
        fill.className = 'signal-fill positive';
        fill.style.left = '50%';
        fill.style.right = 'auto';
    } else {
        fill.className = 'signal-fill negative';
        fill.style.right = '50%';
        fill.style.left = 'auto';
    }
}

function updateGauge(value) {
    const fill = document.getElementById('gaugeFill');
    const label = document.getElementById('gaugeValue');
    if (!fill || !label) return;

    if (value == null) {
        label.textContent = '\u2014';
        label.style.color = 'var(--text-dim)';
        fill.setAttribute('stroke-dashoffset', '314.16');
        return;
    }

    // Circumference = 2 * PI * 50 = 314.16
    const circumference = 314.16;
    // Map -1..+1 to 0..100%
    const normalized = (value + 1) / 2; // 0..1
    const offset = circumference * (1 - normalized);

    fill.setAttribute('stroke-dashoffset', offset.toFixed(2));

    // Color based on value
    const color = value >= 0.1 ? 'var(--green)' : value <= -0.1 ? 'var(--red)' : 'var(--accent)';
    fill.setAttribute('stroke', color);
    fill.style.filter = `drop-shadow(0 0 6px ${value >= 0.1 ? 'var(--green-glow)' : value <= -0.1 ? 'var(--red-glow)' : 'var(--accent-glow)'})`;

    label.textContent = value.toFixed(3);
    label.style.color = color;
}

// --- Gauge Explanation ---
function updateGaugeExplanation(value) {
    const el = document.getElementById('gaugeExplanation');
    if (!el) return;

    if (value == null) {
        el.textContent = 'Calculando señales...';
        el.style.color = 'var(--text-dim)';
        return;
    }

    const threshold = currentTradeThreshold || 0.06;
    if (Math.abs(value) >= threshold) {
        const dir = value > 0 ? 'UP' : 'DOWN';
        el.textContent = `Señal tradeable (${Math.abs(value).toFixed(3)} ≥ ${threshold}) — ${dir}`;
        el.style.color = 'var(--green)';
    } else if (value === 0) {
        el.textContent = 'Calculando señales...';
        el.style.color = 'var(--text-dim)';
    } else {
        el.textContent = `Señal débil (${Math.abs(value).toFixed(3)} < ${threshold})`;
        el.style.color = 'var(--yellow)';
    }
}

// --- Activity Log ---
function handleRoundUpdate(data) {
    const time = data.timestamp
        ? new Date(data.timestamp).toLocaleTimeString('en-US', { hour12: false, hour: '2-digit', minute: '2-digit' })
        : '--:--';

    activityLog.unshift({
        time,
        decision: data.decision || 'skip',
        reason: data.reason || '',
    });

    if (activityLog.length > MAX_ACTIVITY) {
        activityLog.length = MAX_ACTIVITY;
    }

    renderActivityLog();
}

function renderActivityLog() {
    const container = document.getElementById('activityLog');
    if (!container) return;

    container.innerHTML = '';

    for (const entry of activityLog) {
        const div = document.createElement('div');
        div.className = 'activity-entry';

        const badgeLabel = entry.decision === 'trade' ? 'TRADE'
            : entry.decision === 'wait' ? 'WAIT' : 'SKIP';

        div.innerHTML = `
            <span class="activity-time">${entry.time}</span>
            <span class="activity-dot ${entry.decision}"></span>
            <span class="activity-reason">${entry.reason}</span>
            <span class="activity-badge ${entry.decision}">${badgeLabel}</span>
        `;
        container.appendChild(div);
    }
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

    // Summary stats
    const resolved = trades.filter(t => t.outcome);
    const wins = resolved.filter(t => t.outcome === 'win' || t.outcome === 'take_profit').length;
    const total = resolved.length;

    if (total > 0) {
        const wr = (wins / total * 100);
        const wrEl = document.getElementById('winRate');
        animateValue(wrEl, wr, '', '%', 1);
        document.getElementById('winRateSub').textContent = `${wins}W / ${total - wins}L`;
    }

    const todayEl = document.getElementById('tradesToday');
    todayEl.textContent = trades.length;

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

        let resultHtml;
        if (t.outcome === 'win' || t.outcome === 'take_profit') {
            const label = t.outcome === 'take_profit' ? 'TP' : '';
            resultHtml = `<span class="badge badge-win">${label} +$${(t.pnl || 0).toFixed(2)}</span>`;
        } else if (t.outcome === 'loss' || t.outcome === 'stop_loss') {
            const label = t.outcome === 'stop_loss' ? 'SL' : '';
            resultHtml = `<span class="badge badge-loss">${label} -$${Math.abs(t.pnl || 0).toFixed(2)}</span>`;
        } else if (!t.outcome) {
            resultHtml = `<span class="badge badge-pending">PENDING</span>`;
        } else {
            resultHtml = `<span class="badge badge-pending">${t.outcome.toUpperCase()}</span>`;
        }

        const btcLabel = t.btc_price ? `$${Number(t.btc_price).toLocaleString('en-US', {maximumFractionDigits: 0})}` : '—';

        tr.dataset.tradeId = t.id || '';
        tr.innerHTML = `
            <td>${time}</td>
            <td>${(t.signal_score || 0).toFixed(2)}</td>
            <td class="${sideClass}">${sideLabel}</td>
            <td>$${(t.size_usd || 0).toFixed(2)}</td>
            <td>${(t.entry_price || 0).toFixed(2)}\u00a2</td>
            <td>${btcLabel}</td>
            <td class="result-cell">${resultHtml}</td>
        `;
        body.appendChild(tr);
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
                legend: { display: false },
                tooltip: {
                    backgroundColor: 'rgba(13,13,22,0.9)',
                    borderColor: 'rgba(255,255,255,0.1)',
                    borderWidth: 1,
                    titleFont: { family: "'Inter', sans-serif", size: 11 },
                    bodyFont: { family: "'SF Mono', monospace", size: 12 },
                    padding: 10,
                    displayColors: false,
                    callbacks: {
                        label: (ctx) => `$${ctx.parsed.y.toFixed(2)}`
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
                    ticks: {
                        color: '#6a6a82',
                        font: { family: "'SF Mono', monospace", size: 10 },
                        callback: v => `$${v}`,
                        maxTicksLimit: 5,
                    },
                    grid: { color: 'rgba(255,255,255,0.03)' },
                    border: { display: false },
                }
            }
        }
    });
}

function updateEquityChart(trades) {
    if (!equityChart || !trades.length) return;

    const resolved = trades.filter(t => t.bankroll_after != null).reverse();
    if (!resolved.length) return;

    equityChart.data.labels = resolved.map(t => {
        const d = new Date(t.timestamp);
        return d.toLocaleTimeString('en-US', { hour12: false, hour: '2-digit', minute: '2-digit' });
    });
    equityChart.data.datasets[0].data = resolved.map(t => t.bankroll_after);
    equityChart.update('none');
}
