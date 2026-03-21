// Polybot Dashboard — v3 (unified filters)

const API = '';
let ws = null;
let wsRetries = 0;
let equityChart = null;
let prevBtcPrice = null;
let roundCount = 0;
let lastStatusData = null;

// --- Global filter state ---
const filters = { mode: 'all', duration: 0, range: '7d' };

// --- Init ---
if ('serviceWorker' in navigator) {
    navigator.serviceWorker.register('/static/sw.js').catch(() => {});
}

document.addEventListener('DOMContentLoaded', () => {
    initChart();
    applyFilters();
    fetchStatus();
    connectWS();
    fetchBtcPrice();
    restoreCollapsibles();

    setInterval(fetchStatus, 5000);
    setInterval(() => applyFilters(), 15000);
    setInterval(fetchBtcPrice, 15000);

    // Global filter bar
    document.getElementById('filterBar').addEventListener('click', (e) => {
        const btn = e.target.closest('.pill');
        if (!btn) return;
        const filterType = btn.dataset.filter;
        const value = btn.dataset.value;

        // Update active state within group
        btn.closest('.filter-group').querySelectorAll('.pill').forEach(b => b.classList.remove('active'));
        btn.classList.add('active');

        // Update filter state
        if (filterType === 'mode') filters.mode = value;
        else if (filterType === 'duration') filters.duration = parseInt(value);
        else if (filterType === 'range') filters.range = value;

        applyFilters();
    });

    // Sortable columns
    document.querySelectorAll('th.sortable').forEach(th => {
        th.addEventListener('click', () => sortTable(th.dataset.sort));
    });
});

// --- Apply all filters (refetch trades + chart) ---
let _lastTradesData = [];
let _sortCol = null;
let _sortAsc = false;

async function applyFilters() {
    try {
        const params = new URLSearchParams({
            limit: '500',
            mode: filters.mode,
            duration: filters.duration,
        });
        const [tradesRes, equityRes] = await Promise.all([
            fetch(`${API}/api/trades?${params}`),
            fetch(`${API}/api/stats/equity-series?mode=${filters.mode}&duration=${filters.duration}&range=${filters.range}`),
        ]);
        const trades = await tradesRes.json();
        const equity = await equityRes.json();

        _lastTradesData = trades;
        renderTrades(trades);
        updateEquityChart(equity);
        renderWinRate(trades);
    } catch (e) {
        console.error('Filter apply failed:', e);
    }
}

// --- WebSocket ---
function connectWS() {
    const proto = location.protocol === 'https:' ? 'wss' : 'ws';
    ws = new WebSocket(`${proto}://${location.host}/api/trades/live`);
    ws.onopen = () => { wsRetries = 0; };
    ws.onmessage = (e) => {
        const msg = JSON.parse(e.data);
        if (msg.event === 'trade') {
            if (msg.data.event_type === 'position_update') handlePositionUpdate(msg.data);
            else { fetchStatus(); applyFilters(); }
        }
        if (msg.event === 'signal') handleSignal(msg.data);
        if (msg.event === 'status') fetchStatus();
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
    } catch (e) {}
}

// --- Status ---
async function fetchStatus() {
    try {
        const res = await fetch(`${API}/api/status`);
        const data = await res.json();
        updateStatus(data);
    } catch (e) {}
}

// --- Animated counter ---
function animateValue(el, end, prefix, suffix, decimals) {
    const start = parseFloat(el.dataset.current || '0');
    const duration = 600;
    const startTime = performance.now();
    function tick(now) {
        const elapsed = now - startTime;
        const progress = Math.min(elapsed / duration, 1);
        const eased = 1 - Math.pow(1 - progress, 3);
        const current = start + (end - start) * eased;
        el.textContent = (prefix || '') + current.toFixed(decimals) + (suffix || '');
        if (progress < 1) requestAnimationFrame(tick);
        else el.dataset.current = String(end);
    }
    requestAnimationFrame(tick);
}

// --- Update Status UI ---
function updateStatus(data) {
    lastStatusData = data;

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

    // Training banner
    const tb = document.getElementById('trainingBanner');
    if (tb) tb.style.display = data.dry_run ? 'block' : 'none';

    // Wallet
    const bankroll = data.bankroll || 0;
    const walletBal = data.wallet_balance;
    const realBal = walletBal != null ? walletBal : bankroll;
    const initialDeposit = data.initial_deposit || bankroll;
    const bankrollOpen = data.bankroll_open || initialDeposit;

    animateValue(document.getElementById('walletTotal'), realBal, '$', '', 2);
    const unredeemed = data.unredeemed_value || 0;
    const depositLine = `Deposited: $${initialDeposit.toFixed(2)}`;
    const unredeemedLine = unredeemed > 0 ? ` \u00b7 Pending: $${unredeemed.toFixed(2)}` : '';
    document.getElementById('walletDeposit').textContent = depositLine + unredeemedLine;

    // Daily PnL
    const dailyPnl = data.daily_pnl || 0;
    const pnlEl = document.getElementById('dailyPnl');
    const sign = dailyPnl >= 0 ? '+' : '';
    animateValue(pnlEl, dailyPnl, sign + '$', '', 2);
    pnlEl.className = `card-value ${dailyPnl >= 0 ? 'positive' : 'negative'}`;

    if (bankrollOpen > 0) {
        const pct = (dailyPnl / bankrollOpen * 100);
        document.getElementById('dailyPnlPct').textContent = `${dailyPnl >= 0 ? '+' : ''}${pct.toFixed(1)}% \u00b7 Open: $${bankrollOpen.toFixed(2)}`;
    }

    const fees = data.daily_fees || 0;
    const feesEl = document.getElementById('dailyFees');
    if (feesEl && fees > 0) {
        const gross = dailyPnl + fees;
        feesEl.textContent = `Gross: +$${gross.toFixed(2)} \u00b7 Fees: -$${fees.toFixed(2)}`;
    }

    // Net Profit
    const netProfit = realBal - initialDeposit;
    const netEl = document.getElementById('netProfit');
    const netSign = netProfit >= 0 ? '+' : '';
    animateValue(netEl, netProfit, netSign + '$', '', 2);
    netEl.className = `card-value ${netProfit >= 0 ? 'positive' : 'negative'}`;
    const roi = initialDeposit > 0 ? (netProfit / initialDeposit * 100) : 0;
    document.getElementById('netProfitSub').textContent = `ROI: ${roi >= 0 ? '+' : ''}${roi.toFixed(1)}%`;

    // Risk
    const cl = data.consecutive_losses || 0;
    const streakEl = document.getElementById('riskStreak');
    streakEl.textContent = cl > 0 ? `${cl} losses` : '0';
    streakEl.style.color = cl >= 3 ? 'var(--red)' : '';

    const dailyLoss = Math.abs(Math.min(data.daily_pnl || 0, 0));
    const dailyLossLimit = bankroll * (data.daily_loss_limit_pct || 0.15);
    document.getElementById('riskDailyLoss').textContent = dailyLoss > 0 ? `-$${dailyLoss.toFixed(2)}` : '$0.00';
    if (dailyLossLimit > 0) {
        const pct = Math.min((dailyLoss / dailyLossLimit) * 100, 100);
        const bar = document.getElementById('riskDailyBar');
        bar.style.width = pct + '%';
        bar.className = 'progress-fill' + (pct > 80 ? ' danger' : pct > 50 ? ' warning' : '');
    }

    const ddEl = document.getElementById('riskDrawdown');
    if (ddEl && data.drawdown_multiplier != null) {
        const mult = data.drawdown_multiplier;
        ddEl.textContent = `${mult.toFixed(2)}x`;
        ddEl.style.color = mult >= 0.9 ? 'var(--green)' : mult >= 0.5 ? 'var(--yellow)' : 'var(--red)';
        const ddIcon = ddEl.closest('.risk-item')?.querySelector('.risk-icon');
        if (ddIcon) ddIcon.className = 'risk-icon ' + (mult >= 0.9 ? 'green' : mult >= 0.5 ? 'yellow' : 'red');
    }

    const cdEl = document.getElementById('riskCooldown');
    cdEl.textContent = data.cooldown_remaining > 0 ? `${data.cooldown_remaining} rounds` : 'No';
    cdEl.style.color = data.cooldown_remaining > 0 ? 'var(--yellow)' : '';

    const cbEl = document.getElementById('riskCircuit');
    if (data.circuit_breaker) {
        cbEl.textContent = 'TRIPPED';
        cbEl.style.color = 'var(--red)';
        cbEl.closest('.risk-item').querySelector('.risk-icon').className = 'risk-icon red';
    } else {
        cbEl.textContent = 'OK';
        cbEl.style.color = 'var(--green)';
    }

    const tcdEl = document.getElementById('riskTradeCooldown');
    if (tcdEl) {
        const cd = data.trade_cooldown_seconds || 0;
        tcdEl.textContent = cd > 0 ? `${Math.floor(cd / 60)}m` : 'Off';
        tcdEl.style.color = cd > 0 ? 'var(--accent)' : 'var(--text-dim)';
    }

    if (data.signals) handleSignal(data.signals);
}

// --- Signals (Fair Value) ---
function handleSignal(sig) {
    if (!sig) return;
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
    const edgeEl = document.getElementById('fvEdge');
    if (edgeEl) {
        const pct = (sig.edge * 100).toFixed(1);
        edgeEl.textContent = sig.edge > 0 ? `+${pct}%` : `${pct}%`;
        edgeEl.style.color = sig.has_edge ? 'var(--green)' : sig.edge > 0 ? 'var(--yellow)' : 'var(--text-dim)';
    }
    const explEl = document.getElementById('fvExplanation');
    if (explEl) {
        if (sig.has_edge && sig.side) {
            explEl.textContent = `Tradeable: P(${sig.side}) exceeds breakeven + fee`;
            explEl.style.color = 'var(--green)';
        } else if (sig.edge > 0) {
            explEl.textContent = 'Insufficient edge (out of band)';
            explEl.style.color = 'var(--yellow)';
        } else {
            explEl.textContent = 'No edge detected';
            explEl.style.color = 'var(--text-dim)';
        }
    }
    const probUp = sig.prob_up || 0.5;
    const probDown = sig.prob_down || 0.5;
    setMetric('fvProbUp', (probUp * 100).toFixed(1) + '%', probUp > 0.55 ? 'var(--green)' : null);
    setMetric('fvProbDown', (probDown * 100).toFixed(1) + '%', probDown > 0.55 ? 'var(--red)' : null);
    setBar('fvProbUpBar', probUp * 100);
    setBar('fvProbDownBar', probDown * 100);
    setMetric('fvVol', sig.vol_5m != null ? (sig.vol_5m * 100).toFixed(4) + '%' : '\u2014');
    setMetric('fvDrift', sig.drift_5m != null ? (sig.drift_5m > 0 ? '+' : '') + (sig.drift_5m * 100).toFixed(4) + '%' : '\u2014',
        sig.drift_5m > 0 ? 'var(--green)' : sig.drift_5m < 0 ? 'var(--red)' : null);
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
    if (marketEl) marketEl.textContent = data.market || '\u2014';
    const signalEl = el('lsSignal');
    if (signalEl) {
        if (data.signal != null) {
            const s = data.signal;
            signalEl.textContent = (s > 0 ? '+' : '') + s.toFixed(3);
            signalEl.style.color = s > 0 ? 'var(--green)' : s < 0 ? 'var(--red)' : 'var(--text-dim)';
        } else {
            signalEl.textContent = '\u2014';
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

// --- Trades Table ---
function renderTrades(trades) {
    const body = document.getElementById('tradesBody');
    body.innerHTML = '';

    const sorted = _sortCol ? sortData(trades, _sortCol, _sortAsc) : trades;

    for (let i = 0; i < Math.min(sorted.length, 50); i++) {
        const t = sorted[i];
        const tr = document.createElement('tr');

        const time = t.timestamp
            ? new Date(t.timestamp).toLocaleTimeString('en-US', { hour12: false, hour: '2-digit', minute: '2-digit' })
            : '';

        const typeBadge = t.dry_run
            ? '<span class="badge badge-paper">PAPER</span>'
            : '<span class="badge badge-live-tag">LIVE</span>';

        const tfMin = Math.round((t.market_duration || 300) / 60);
        const tfBadge = `<span class="badge badge-tf badge-tf-${tfMin}">${tfMin}m</span>`;

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
            resultHtml = `<span class="badge badge-pending badge-unclog" title="Click to resolve" onclick="unclogTrades(event)">PENDING</span>`;
        } else {
            resultHtml = `<span class="badge badge-pending">${t.outcome.toUpperCase()}</span>`;
        }

        const btcLabel = t.btc_price ? `$${Number(t.btc_price).toLocaleString('en-US', {maximumFractionDigits: 0})}` : '\u2014';

        tr.dataset.tradeId = t.id || '';
        tr.innerHTML = `
            <td>${time}</td>
            <td>${typeBadge}</td>
            <td>${tfBadge}</td>
            <td class="${sideClass}">${sideLabel}</td>
            <td>$${(t.size_usd || 0).toFixed(2)}</td>
            <td>${(t.entry_price || 0).toFixed(2)}\u00a2</td>
            <td>${btcLabel}</td>
            <td class="result-cell">${resultHtml}</td>
        `;
        body.appendChild(tr);
    }
}

function renderWinRate(trades) {
    const resolved = trades.filter(t => t.outcome);
    const wins = resolved.filter(t => t.outcome === 'win' || t.outcome === 'take_profit').length;
    const total = resolved.length;
    if (total > 0) {
        const wr = (wins / total * 100);
        animateValue(document.getElementById('winRate'), wr, '', '%', 1);
        document.getElementById('winRateSub').textContent = `${wins}W / ${total - wins}L \u00b7 ${total} trades`;
    } else {
        document.getElementById('winRate').textContent = '\u2014';
        document.getElementById('winRateSub').textContent = '0 trades';
    }
}

// --- Sort ---
function sortData(data, col, asc) {
    return [...data].sort((a, b) => {
        let va = a[col], vb = b[col];
        if (va == null) va = '';
        if (vb == null) vb = '';
        if (typeof va === 'number' && typeof vb === 'number') return asc ? va - vb : vb - va;
        return asc ? String(va).localeCompare(String(vb)) : String(vb).localeCompare(String(va));
    });
}

function sortTable(col) {
    if (_sortCol === col) _sortAsc = !_sortAsc;
    else { _sortCol = col; _sortAsc = true; }

    // Update header indicators
    document.querySelectorAll('th.sortable').forEach(th => {
        th.classList.remove('sort-asc', 'sort-desc');
        if (th.dataset.sort === _sortCol) th.classList.add(_sortAsc ? 'sort-asc' : 'sort-desc');
    });

    renderTrades(_lastTradesData);
}

// --- Unclog Pending Trades ---
async function unclogTrades(e) {
    if (e) e.stopPropagation();
    document.querySelectorAll('.badge-unclog').forEach(b => {
        b.textContent = '...';
        b.style.pointerEvents = 'none';
    });
    try {
        const resp = await fetch('/api/trades/resolve-pending', { method: 'POST' });
        if (resp.status === 401) { window.location.href = '/admin'; return; }
        const data = await resp.json();
        if (data.remaining_pending === 0) applyFilters();
        else {
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

// --- Position Update ---
function handlePositionUpdate(data) {
    const { trade_id, unrealized_pnl, unrealized_pct } = data;
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

// --- Equity Chart (5 series: paper/live equity, paper/live WR, PnL) ---
function initChart() {
    const ctx = document.getElementById('equityChart').getContext('2d');
    const gradientPaper = ctx.createLinearGradient(0, 0, 0, 360);
    gradientPaper.addColorStop(0, 'rgba(96, 165, 250, 0.15)');
    gradientPaper.addColorStop(1, 'rgba(96, 165, 250, 0.01)');

    equityChart = new Chart(ctx, {
        type: 'line',
        data: {
            labels: [],
            datasets: [
                { // 0: Paper Equity
                    label: 'Equity (Paper)',
                    data: [],
                    borderColor: '#60a5fa',
                    backgroundColor: gradientPaper,
                    fill: true,
                    tension: 0.3,
                    pointRadius: 2,
                    pointHitRadius: 8,
                    pointHoverRadius: 4,
                    borderWidth: 2,
                    yAxisID: 'y',
                },
                { // 1: Live Equity
                    label: 'Equity (Live)',
                    data: [],
                    borderColor: '#10b981',
                    backgroundColor: 'transparent',
                    fill: false,
                    tension: 0.3,
                    pointRadius: 2,
                    pointHitRadius: 8,
                    pointHoverRadius: 4,
                    borderWidth: 2,
                    yAxisID: 'y',
                },
                { // 2: Cumulative PnL
                    label: 'Cumulative PnL',
                    data: [],
                    borderColor: '#f59e0b',
                    backgroundColor: 'transparent',
                    fill: false,
                    tension: 0.3,
                    pointRadius: 0,
                    pointHitRadius: 8,
                    pointHoverRadius: 3,
                    borderWidth: 1.5,
                    yAxisID: 'y',
                },
                { // 3: Paper WR
                    label: 'WR (Paper)',
                    data: [],
                    borderColor: 'rgba(96, 165, 250, 0.6)',
                    backgroundColor: 'transparent',
                    fill: false,
                    tension: 0.3,
                    pointRadius: 0,
                    pointHitRadius: 8,
                    pointHoverRadius: 3,
                    borderWidth: 1.5,
                    borderDash: [4, 3],
                    yAxisID: 'yWR',
                },
                { // 4: Live WR
                    label: 'WR (Live)',
                    data: [],
                    borderColor: 'rgba(16, 185, 129, 0.6)',
                    backgroundColor: 'transparent',
                    fill: false,
                    tension: 0.3,
                    pointRadius: 0,
                    pointHitRadius: 8,
                    pointHoverRadius: 3,
                    borderWidth: 1.5,
                    borderDash: [4, 3],
                    yAxisID: 'yWR',
                },
            ]
        },
        options: {
            responsive: true,
            maintainAspectRatio: false,
            interaction: { intersect: false, mode: 'index' },
            plugins: {
                legend: {
                    display: true,
                    labels: {
                        color: '#6a6a82',
                        font: { family: "'Inter', sans-serif", size: 10 },
                        boxWidth: 12,
                        padding: 10,
                        usePointStyle: true,
                    },
                    onClick: (e, legendItem, legend) => {
                        const idx = legendItem.datasetIndex;
                        const meta = legend.chart.getDatasetMeta(idx);
                        meta.hidden = !meta.hidden;
                        legend.chart.update();
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
                            if (ctx.dataset.yAxisID === 'yWR') {
                                return `${ctx.dataset.label}: ${(ctx.parsed.y * 100).toFixed(0)}%`;
                            }
                            return `${ctx.dataset.label}: $${ctx.parsed.y.toFixed(2)}`;
                        },
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
                    title: { display: true, text: 'USD', color: '#6a6a82', font: { size: 10 } },
                    ticks: {
                        color: '#6a6a82',
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
                    title: { display: true, text: 'Win Rate', color: '#6a6a82', font: { size: 10 } },
                    min: 0,
                    max: 1,
                    ticks: {
                        color: '#6a6a82',
                        font: { family: "'SF Mono', monospace", size: 10 },
                        callback: v => `${(v * 100).toFixed(0)}%`,
                        maxTicksLimit: 5,
                    },
                    grid: { drawOnChartArea: false },
                    border: { display: false },
                },
            }
        }
    });
}

function updateEquityChart(equity) {
    if (!equityChart) return;
    const { live, paper } = equity;

    // Merge all timestamps for unified x-axis
    const allPoints = [
        ...paper.map(p => ({ ...p, src: 'paper' })),
        ...live.map(p => ({ ...p, src: 'live' })),
    ].sort((a, b) => new Date(a.ts) - new Date(b.ts));

    if (!allPoints.length) return;

    const useDateLabel = ['7d', '30d', 'all'].includes(filters.range);
    const timestamps = [...new Set(allPoints.map(p => p.ts))].sort();

    const paperMap = new Map(paper.map(p => [p.ts, p]));
    const liveMap = new Map(live.map(p => [p.ts, p]));

    const labels = [];
    const paperEquity = [];
    const liveEquity = [];
    const pnlData = [];
    const paperWR = [];
    const liveWR = [];

    let lastPaper = null, lastLive = null, lastPnl = null;
    let lastPaperWR = null, lastLiveWR = null;

    for (const ts of timestamps) {
        const d = new Date(ts);
        if (useDateLabel) {
            labels.push(d.toLocaleDateString('en-US', { month: 'short', day: 'numeric' })
                + ' ' + d.toLocaleTimeString('en-US', { hour12: false, hour: '2-digit', minute: '2-digit' }));
        } else {
            labels.push(d.toLocaleTimeString('en-US', { hour12: false, hour: '2-digit', minute: '2-digit' }));
        }

        if (paperMap.has(ts)) {
            const p = paperMap.get(ts);
            lastPaper = p.bankroll;
            lastPaperWR = p.wr;
            lastPnl = p.cum_pnl;
        }
        if (liveMap.has(ts)) {
            const l = liveMap.get(ts);
            lastLive = l.bankroll;
            lastLiveWR = l.wr;
            // If both exist, sum PnL; otherwise use whichever is available
            if (lastPnl === null) lastPnl = l.cum_pnl;
            else lastPnl = l.cum_pnl; // live takes precedence for combined PnL
        }

        paperEquity.push(lastPaper);
        liveEquity.push(lastLive);
        pnlData.push(lastPnl);
        paperWR.push(lastPaperWR);
        liveWR.push(lastLiveWR);
    }

    equityChart.data.labels = labels;
    equityChart.data.datasets[0].data = paperEquity;
    equityChart.data.datasets[1].data = liveEquity;
    equityChart.data.datasets[2].data = pnlData;
    equityChart.data.datasets[3].data = paperWR;
    equityChart.data.datasets[4].data = liveWR;
    equityChart.update('none');
}

// --- Collapsible Panels ---
function togglePanel(btn) {
    const panel = btn.closest('.collapsible-panel');
    const body = panel.querySelector('.collapsible-body');
    body.classList.toggle('open');
    btn.querySelector('.chevron').classList.toggle('rotated', body.classList.contains('open'));
    saveCollapsibles();
}

function saveCollapsibles() {
    const state = {};
    document.querySelectorAll('.collapsible-panel').forEach(p => {
        const key = p.dataset.panel;
        state[key] = p.querySelector('.collapsible-body').classList.contains('open');
    });
    localStorage.setItem('polybot_panels', JSON.stringify(state));
}

function restoreCollapsibles() {
    try {
        const state = JSON.parse(localStorage.getItem('polybot_panels'));
        if (!state) return;
        document.querySelectorAll('.collapsible-panel').forEach(p => {
            const key = p.dataset.panel;
            if (key in state) {
                const body = p.querySelector('.collapsible-body');
                if (state[key]) body.classList.add('open');
                else body.classList.remove('open');
                p.querySelector('.chevron').classList.toggle('rotated', state[key]);
            }
        });
    } catch (e) {}
}
