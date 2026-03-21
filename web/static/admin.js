// Polybot Mission Control — v3

const API = '';
let adminTradeMode = 'all';

// All config fields
const FIELDS = [
    'max_trade_pct', 'base_trade_pct', 'min_trade_usd', 'max_exposure_usd',
    'daily_loss_limit_pct',
    'min_edge', 'max_edge', 'min_vol_5m', 'max_vol_5m',
    'max_spread_cents', 'min_time_remaining_sec',
    'max_consecutive_losses', 'cooldown_rounds',
    'min_entry_price', 'max_entry_price', 'stop_loss_pct', 'take_profit_pct',
    'trade_cooldown_seconds', 'post_loss_cooldown_seconds',
    'min_win_rate', 'circuit_breaker_window',
    'circuit_breaker_hours', 'min_drawdown_multiplier', 'bankroll_floor_usd',
    'dry_run', 'use_tp_sl', 'rag_enabled',
    'telegram_enabled', 'bot_enabled', 'binance_symbol',
];

const BOOL_FIELDS = ['dry_run', 'use_tp_sl', 'rag_enabled', 'telegram_enabled', 'bot_enabled'];
const INT_FIELDS = ['max_consecutive_losses', 'cooldown_rounds', 'max_spread_cents', 'min_time_remaining_sec',
    'trade_cooldown_seconds', 'post_loss_cooldown_seconds', 'circuit_breaker_window', 'circuit_breaker_hours', 'bankroll_floor_usd'];
const TEXT_FIELDS = ['binance_symbol'];

// --- Auth ---
async function authFetch(url, opts = {}) {
    const res = await fetch(url, opts);
    if (res.status === 401) {
        location.href = '/login?next=/admin';
        throw new Error('Not authenticated');
    }
    return res;
}

// --- Init ---
document.addEventListener('DOMContentLoaded', () => {
    loadConfig();
    loadStatus();
    loadTrades();
    loadStats();
    loadKeysStatus();
    loadWalletBalances();
    loadRagCount();

    // Auto-refresh
    setInterval(loadStatus, 5000);
    setInterval(loadTrades, 15000);
    setInterval(loadStats, 30000);

    // Trade mode filter
    document.getElementById('adminModeFilters').addEventListener('click', (e) => {
        const btn = e.target.closest('.chart-filter-btn');
        if (!btn) return;
        document.querySelectorAll('#adminModeFilters .chart-filter-btn').forEach(b => b.classList.remove('active'));
        btn.classList.add('active');
        adminTradeMode = btn.dataset.mode;
        loadTrades();
    });
});

// =====================
// LIVE STATUS
// =====================
async function loadStatus() {
    try {
        const res = await fetch(`${API}/api/status`);
        const d = await res.json();
        updateStatusHero(d);
        updateSignals(d.signals);
        updateRisk(d);
    } catch (e) {
        console.error('Status failed:', e);
    }
}

function updateStatusHero(d) {
    // Badge
    const badge = document.getElementById('statusBadge');
    const text = document.getElementById('statusText');
    if (d.running) {
        const isLive = !d.dry_run;
        badge.className = isLive ? 'status-badge running-live' : 'status-badge running';
        text.textContent = isLive ? 'LIVE \u00b7 REAL $' : 'RUNNING \u00b7 DRY RUN';
    } else {
        badge.className = 'status-badge stopped';
        text.textContent = 'STOPPED';
    }

    // Mode badge
    const modeBadge = document.getElementById('mcModeBadge');
    if (d.dry_run) {
        modeBadge.textContent = 'PAPER MODE';
        modeBadge.style.background = 'var(--surface-3)';
        modeBadge.style.color = 'var(--text-dim)';
    } else {
        modeBadge.textContent = 'LIVE TRADING';
        modeBadge.style.background = 'var(--red-bg)';
        modeBadge.style.color = 'var(--red)';
    }

    // Hero cards
    const bankroll = d.bankroll || 0;
    const initialDeposit = d.initial_deposit || bankroll;
    const walletBal = d.wallet_balance;
    const bankrollOpen = d.bankroll_open || initialDeposit;

    // Card 1: Wallet Real (on-chain balance)
    const walletEl = document.getElementById('mcWallet');
    if (walletBal != null) {
        walletEl.textContent = `$${walletBal.toFixed(2)}`;
    } else {
        walletEl.textContent = `$${bankroll.toFixed(2)}`;
    }
    document.getElementById('mcDeposit').textContent = `Invertido: $${initialDeposit.toFixed(2)}`;

    // Card 2: Today P&L
    const pnl = d.daily_pnl || 0;
    const pnlEl = document.getElementById('mcDailyPnl');
    pnlEl.textContent = `${pnl >= 0 ? '+' : ''}$${pnl.toFixed(2)}`;
    pnlEl.className = `card-value ${pnl >= 0 ? 'positive' : 'negative'}`;
    document.getElementById('mcDailyPct').textContent = bankrollOpen > 0
        ? `${pnl >= 0 ? '+' : ''}${(pnl / bankrollOpen * 100).toFixed(1)}% · Open: $${bankrollOpen.toFixed(2)}`
        : '';

    // Fees transparency
    const fees = d.daily_fees || 0;
    const feesEl = document.getElementById('mcDailyFees');
    if (feesEl && fees > 0) {
        const gross = pnl + fees;
        feesEl.textContent = `Bruto: +$${gross.toFixed(2)} · Fees: -$${fees.toFixed(2)}`;
    }

    // Card 3: Net Profit (wallet - initial deposit)
    const realBal = walletBal != null ? walletBal : bankroll;
    const netProfit = realBal - initialDeposit;
    const netEl = document.getElementById('mcNetProfit');
    netEl.textContent = `${netProfit >= 0 ? '+' : ''}$${netProfit.toFixed(2)}`;
    netEl.className = `card-value ${netProfit >= 0 ? 'positive' : 'negative'}`;
    const roi = initialDeposit > 0 ? (netProfit / initialDeposit * 100) : 0;
    document.getElementById('mcNetProfitSub').textContent = `ROI: ${roi >= 0 ? '+' : ''}${roi.toFixed(1)}% · R${d.round || 0}`;

    // BTC price
    if (d.current_price) {
        document.getElementById('mcBtcPrice').textContent = `$${Number(d.current_price).toLocaleString('en-US', { maximumFractionDigits: 0 })}`;
    }
}

// =====================
// SIGNALS
// =====================
function updateSignals(sig) {
    if (!sig) return;
    // Fair Value signals
    const edgeEl = document.getElementById('adminEdge');
    if (edgeEl) {
        const edge = sig.edge || 0;
        edgeEl.textContent = `${(edge * 100).toFixed(1)}% ${sig.side ? sig.side.toUpperCase() : ''}`;
        edgeEl.style.color = sig.has_edge ? 'var(--green)' : 'var(--text-dim)';
    }
    const probEl = document.getElementById('adminProb');
    if (probEl && sig.prob_up != null) {
        probEl.textContent = `P(up)=${(sig.prob_up * 100).toFixed(1)}%`;
    }
}

// =====================
// RISK
// =====================
function updateRisk(d) {
    const cl = d.consecutive_losses || 0;
    const streakEl = document.getElementById('riskStreak');
    streakEl.textContent = cl > 0 ? `${cl} losses` : '0';
    streakEl.style.color = cl >= 3 ? 'var(--red)' : '';

    const dailyLoss = Math.abs(Math.min(d.daily_pnl || 0, 0));
    const bankroll = d.bankroll || 1;
    const dailyLossLimit = bankroll * (d.daily_loss_limit_pct || 0.15);
    document.getElementById('riskDailyLoss').textContent = dailyLoss > 0 ? `-$${dailyLoss.toFixed(2)}` : '$0.00';
    if (dailyLossLimit > 0) {
        const pct = Math.min((dailyLoss / dailyLossLimit) * 100, 100);
        const bar = document.getElementById('riskDailyBar');
        bar.style.width = pct + '%';
        bar.className = 'progress-fill' + (pct > 80 ? ' danger' : pct > 50 ? ' warning' : '');
    }

    const ddEl = document.getElementById('riskDrawdown');
    if (d.drawdown_multiplier != null) {
        const mult = d.drawdown_multiplier;
        ddEl.textContent = `${mult.toFixed(2)}x`;
        ddEl.style.color = mult >= 0.9 ? 'var(--green)' : mult >= 0.5 ? 'var(--yellow)' : 'var(--red)';
    }

    const cdEl = document.getElementById('riskCooldown');
    cdEl.textContent = d.cooldown_remaining > 0 ? `${d.cooldown_remaining} rounds` : 'No';
    cdEl.style.color = d.cooldown_remaining > 0 ? 'var(--yellow)' : '';

    const cbEl = document.getElementById('riskCircuit');
    if (d.circuit_breaker) {
        cbEl.textContent = 'TRIPPED';
        cbEl.style.color = 'var(--red)';
    } else {
        cbEl.textContent = 'OK';
        cbEl.style.color = 'var(--green)';
    }

    const tcdEl = document.getElementById('riskTradeCooldown');
    if (tcdEl) {
        const cd = d.trade_cooldown_seconds || 0;
        tcdEl.textContent = cd > 0 ? `${cd}s` : 'Off';
        tcdEl.style.color = cd > 0 ? 'var(--accent)' : 'var(--text-dim)';
    }
}

// =====================
// TRADES
// =====================
async function loadTrades() {
    try {
        const res = await fetch(`${API}/api/trades?limit=20&mode=${adminTradeMode}`);
        const trades = await res.json();
        renderTrades(trades);
    } catch (e) {
        console.error('Trades failed:', e);
    }
}

function renderTrades(trades) {
    const body = document.getElementById('adminTradesBody');
    body.innerHTML = '';

    // Update hero win rate from trades
    const resolved = trades.filter(t => t.outcome);
    const wins = resolved.filter(t => t.outcome === 'win' || t.outcome === 'take_profit').length;
    const total = resolved.length;

    const wrEl = document.getElementById('mcWinRate');
    const wrSub = document.getElementById('mcWinRateSub');
    if (total > 0) {
        wrEl.textContent = `${(wins / total * 100).toFixed(1)}%`;
        wrSub.textContent = `${wins}W / ${total - wins}L`;
    }

    for (const t of trades) {
        const tr = document.createElement('tr');
        const time = t.timestamp
            ? new Date(t.timestamp).toLocaleTimeString('en-US', { hour12: false, hour: '2-digit', minute: '2-digit' })
            : '';

        const sideClass = t.side === 'up' ? 'side-up' : 'side-down';
        const sideLabel = t.side === 'up' ? 'UP \u25b2' : 'DOWN \u25bc';
        const modeBadge = t.dry_run ? '<span class="badge badge-paper">PAPER</span>' : '<span class="badge badge-live-tag">LIVE</span>';

        let resultHtml;
        if (t.outcome === 'win' || t.outcome === 'take_profit') {
            resultHtml = `<span class="badge badge-win">+$${(t.pnl || 0).toFixed(2)}</span>`;
        } else if (t.outcome === 'loss' || t.outcome === 'stop_loss') {
            resultHtml = `<span class="badge badge-loss">-$${Math.abs(t.pnl || 0).toFixed(2)}</span>`;
        } else {
            resultHtml = `<span class="badge badge-pending">PENDING</span>`;
        }

        const btcLabel = t.btc_price ? `$${Number(t.btc_price).toLocaleString('en-US', { maximumFractionDigits: 0 })}` : '\u2014';

        tr.innerHTML = `
            <td>${time}</td>
            <td>${(t.signal_score || 0).toFixed(2)}</td>
            <td class="${sideClass}">${sideLabel} ${modeBadge}</td>
            <td>$${(t.size_usd || 0).toFixed(2)}</td>
            <td>${(t.entry_price || 0).toFixed(2)}\u00a2</td>
            <td>${btcLabel}</td>
            <td>${resultHtml}</td>
        `;
        body.appendChild(tr);
    }
}

// =====================
// CONFIG
// =====================
async function loadConfig() {
    try {
        const res = await authFetch(`${API}/api/config`);
        const cfg = await res.json();

        for (const f of FIELDS) {
            const el = document.getElementById(f);
            if (!el) continue;
            if (el.tagName === 'SELECT') {
                el.value = String(cfg[f] ?? 'true');
            } else {
                el.value = cfg[f] ?? '';
            }
        }

    } catch (e) {
        console.error('Config load failed:', e);
    }
}

async function saveConfig() {
    const cfg = {};
    for (const f of FIELDS) {
        const el = document.getElementById(f);
        if (!el) continue;
        if (BOOL_FIELDS.includes(f)) {
            cfg[f] = el.value === 'true';
        } else if (TEXT_FIELDS.includes(f)) {
            cfg[f] = el.value;
        } else if (INT_FIELDS.includes(f)) {
            cfg[f] = parseInt(el.value);
        } else {
            cfg[f] = parseFloat(el.value);
        }
    }

    // Preserve non-editable fields
    try {
        const res = await authFetch(`${API}/api/config`);
        const current = await res.json();
        for (const [k, v] of Object.entries(current)) {
            if (!(k in cfg)) cfg[k] = v;
        }
    } catch (e) { /* ignore */ }

    try {
        const res = await authFetch(`${API}/api/config`, {
            method: 'PUT',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(cfg),
        });
        if (res.ok) showToast('Config saved', 'success');
        else showToast('Failed to save', 'error');
    } catch (e) {
        showToast('Network error', 'error');
    }
}

// =====================
// STATS
// =====================
async function loadStats() {
    try {
        const res = await fetch(`${API}/api/stats/summary`);
        const s = await res.json();
        document.getElementById('statTotal').textContent = s.total_trades || 0;
        document.getElementById('statWinRate').textContent =
            s.win_rate != null ? `${(s.win_rate * 100).toFixed(1)}%` : '\u2014';
        const pnlEl = document.getElementById('statPnl');
        const pnl = s.total_pnl || 0;
        pnlEl.textContent = `${pnl >= 0 ? '+' : ''}$${pnl.toFixed(2)}`;
        pnlEl.className = `card-value ${pnl >= 0 ? 'positive' : 'negative'}`;
    } catch (e) {
        console.error('Stats failed:', e);
    }
}

// =====================
// SETTINGS TABS
// =====================
function switchTab(btn) {
    const tabId = btn.dataset.tab;
    document.querySelectorAll('.settings-tab').forEach(t => t.classList.remove('active'));
    document.querySelectorAll('.settings-tab-content').forEach(c => c.classList.remove('active'));
    btn.classList.add('active');
    document.getElementById(tabId).classList.add('active');
}

// =====================
// BOT CONTROL
// =====================
async function startBot() {
    try {
        await authFetch(`${API}/api/bot/start`, { method: 'POST' });
        showToast('Bot started', 'success');
        setTimeout(loadStatus, 1000);
    } catch (e) {
        showToast('Failed to start', 'error');
    }
}

async function stopBot() {
    try {
        await authFetch(`${API}/api/bot/stop`, { method: 'POST' });
        showToast('Bot stopped', 'success');
        setTimeout(loadStatus, 1000);
    } catch (e) {
        showToast('Failed to stop', 'error');
    }
}

async function restartBot() {
    if (!confirm('Restart the bot process? It will reload all code.')) return;
    try {
        await authFetch(`${API}/api/bot/restart`, { method: 'POST' });
        showToast('Restarting... page will reload in 5s', 'success');
        setTimeout(() => location.reload(), 5000);
    } catch (e) {
        showToast('Failed to restart', 'error');
    }
}

async function logout() {
    await fetch(`${API}/api/auth/logout`, { method: 'POST' });
    location.href = '/login';
}

// =====================
// API KEYS
// =====================
async function loadKeysStatus() {
    try {
        const res = await authFetch(`${API}/api/keys/status`);
        const data = await res.json();
        const el = document.getElementById('credsStatus');
        if (data.has_creds) {
            let msg = '\u2705 API keys configured';
            if (data.balance != null) msg += ` \u2014 Wallet: $${data.balance.toFixed(2)} USDC`;
            el.textContent = msg;
            el.style.color = 'var(--green)';
        } else {
            el.textContent = '\u26a0 No API keys \u2014 dry run only';
            el.style.color = 'var(--yellow)';
        }
    } catch (e) { /* ignore */ }
}

async function saveKeys() {
    const data = {
        POLYMARKET_PRIVATE_KEY: document.getElementById('pk').value,
        POLYMARKET_API_KEY: document.getElementById('api_key').value,
        POLYMARKET_API_SECRET: document.getElementById('api_secret').value,
        POLYMARKET_API_PASSPHRASE: document.getElementById('api_passphrase').value,
    };
    for (const k of Object.keys(data)) { if (!data[k]) delete data[k]; }
    if (!Object.keys(data).length) { showToast('Fill in at least one field', 'error'); return; }

    try {
        const res = await authFetch(`${API}/api/keys/save`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(data),
        });
        if (res.ok) { showToast('API keys saved', 'success'); loadKeysStatus(); }
        else showToast('Failed to save keys', 'error');
    } catch (e) { showToast('Network error', 'error'); }
}

async function deriveKeys() {
    const pk = document.getElementById('pk').value;
    if (!pk) { showToast('Enter private key first', 'error'); return; }
    showToast('Deriving keys...', 'info');
    try {
        const res = await authFetch(`${API}/api/keys/derive`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ POLYMARKET_PRIVATE_KEY: pk }),
        });
        if (res.ok) {
            const data = await res.json();
            document.getElementById('api_key').value = data.api_key;
            document.getElementById('api_secret').value = data.api_secret;
            document.getElementById('api_passphrase').value = data.api_passphrase;
            showToast(`Keys derived for ${data.address.slice(0, 8)}...`, 'success');
        } else {
            const err = await res.json();
            showToast(`Error: ${err.detail}`, 'error');
        }
    } catch (e) { showToast('Derivation failed', 'error'); }
}

async function checkBalance() {
    try {
        const res = await authFetch(`${API}/api/keys/status`);
        const data = await res.json();
        if (!data.has_creds) { showToast('Save API keys first', 'error'); return; }
        if (data.balance != null) showToast(`Wallet: $${data.balance.toFixed(2)} USDC`, 'success');
        else showToast('Could not fetch balance', 'error');
    } catch (e) { showToast('Balance check failed', 'error'); }
}

// =====================
// TELEGRAM
// =====================
async function saveTelegram() {
    const data = {
        TELEGRAM_TOKEN: document.getElementById('tg_token').value,
        TELEGRAM_CHAT_ID: document.getElementById('tg_chat_id').value,
    };
    for (const k of Object.keys(data)) { if (!data[k]) delete data[k]; }
    try {
        const res = await authFetch(`${API}/api/keys/telegram`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(data),
        });
        if (res.ok) showToast('Telegram config saved', 'success');
        else showToast('Failed to save', 'error');
    } catch (e) { showToast('Network error', 'error'); }
}

// =====================
// WALLET
// =====================
async function loadWalletBalances() {
    try {
        const res = await authFetch(`${API}/api/wallet/balances`);
        const d = await res.json();
        const fmt = (v) => v != null ? `$${v.toFixed(2)}` : '\u2014';

        document.getElementById('walletUsdcNative').textContent = fmt(d.eoa_usdc_native);
        document.getElementById('walletEoa').textContent = fmt(d.eoa_usdc);

        const unEl = document.getElementById('walletUnredeemed');
        if (d.unredeemed_count > 0) {
            unEl.textContent = `${d.unredeemed_count} (~$${d.unredeemed_value.toFixed(2)})`;
            unEl.style.color = 'var(--yellow)';
        } else {
            unEl.textContent = '0';
            unEl.style.color = '';
        }

        const usdtEl = document.getElementById('walletUsdt');
        if (usdtEl) {
            usdtEl.textContent = fmt(d.eoa_usdt);
            if (d.eoa_usdt > 0) usdtEl.style.color = 'var(--yellow)';
        }

        const maticEl = document.getElementById('walletMatic');
        if (d.matic != null) {
            maticEl.textContent = `Gas: ${d.matic.toFixed(4)} POL`;
            maticEl.style.color = d.matic < 0.1 ? 'var(--red)' : 'var(--text-dim)';
        }
    } catch (e) { console.error('Wallet failed:', e); }
}

async function paperFund(mode) {
    const amount = parseFloat(document.getElementById('paperAmount').value);
    if (!amount || amount <= 0) { showToast('Enter a valid amount', 'error'); return; }
    const label = mode === 'set' ? `Set bankroll to $${amount}?` : `Add $${amount} to bankroll?`;
    if (!confirm(label)) return;
    const statusEl = document.getElementById('paperStatus');
    statusEl.textContent = 'Processing...';
    try {
        const res = await authFetch(`${API}/api/paper/fund`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ amount, mode }),
        });
        const d = await res.json();
        if (d.status === 'ok') {
            showToast(`Bankroll: $${d.previous_bankroll.toFixed(2)} → $${d.new_bankroll.toFixed(2)}`, 'success');
            statusEl.textContent = `$${d.previous_bankroll.toFixed(2)} → $${d.new_bankroll.toFixed(2)}`;
        } else {
            showToast(d.detail || 'Failed', 'error');
            statusEl.textContent = d.detail || 'Error';
        }
    } catch (e) {
        showToast(e.message, 'error');
        statusEl.textContent = e.message;
    }
}

async function paperReset() {
    const amount = parseFloat(document.getElementById('paperAmount').value) || 50;
    if (!confirm(`Reset ALL paper trades and set bankroll to $${amount}? This cannot be undone.`)) return;
    const statusEl = document.getElementById('paperStatus');
    statusEl.textContent = 'Resetting...';
    try {
        const res = await authFetch(`${API}/api/paper/reset`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ bankroll: amount }),
        });
        const d = await res.json();
        if (d.status === 'ok') {
            showToast(`Reset: ${d.trades_deleted} trades deleted, bankroll=$${d.bankroll.toFixed(2)}`, 'success');
            statusEl.textContent = `Deleted ${d.trades_deleted} trades. Bankroll: $${d.bankroll.toFixed(2)}`;
        } else {
            showToast(d.detail || 'Failed', 'error');
            statusEl.textContent = d.detail || 'Error';
        }
    } catch (e) {
        showToast(e.message, 'error');
        statusEl.textContent = e.message;
    }
}

async function redeemAll() {
    if (!confirm('Redeem all winning tokens for USDC.e?')) return;
    const statusEl = document.getElementById('redeemStatus');
    statusEl.textContent = 'Redeeming...';
    statusEl.style.color = 'var(--accent)';

    try {
        const res = await authFetch(`${API}/api/wallet/redeem-all`, { method: 'POST' });
        const d = await res.json();
        if (d.redeemed === 0 && d.total === 0) {
            showToast('No tokens to redeem', 'info');
            statusEl.textContent = 'No unredeemed tokens.';
        } else {
            showToast(`Redeemed ${d.redeemed}/${d.total}`, d.redeemed > 0 ? 'success' : 'error');
            const lines = d.results.map(r =>
                `${r.condition_id} ${r.side}: ${r.success ? '\u2713' : '\u2717 ' + r.error}`
            );
            statusEl.innerHTML = lines.join('<br>');
        }
        loadWalletBalances();
    } catch (e) {
        showToast('Redeem failed', 'error');
        statusEl.textContent = 'Error: ' + e.message;
        statusEl.style.color = 'var(--red)';
    }
}

async function sendUsdc() {
    const address = document.getElementById('sendAddress').value.trim();
    const amount = parseFloat(document.getElementById('sendAmount').value);
    if (!address || !address.startsWith('0x')) { showToast('Enter valid address', 'error'); return; }
    if (!amount || amount <= 0) { showToast('Enter valid amount', 'error'); return; }
    if (!confirm(`Send $${amount.toFixed(2)} USDC.e to ${address.slice(0, 10)}...?`)) return;

    try {
        const res = await authFetch(`${API}/api/wallet/send`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ address, amount }),
        });
        if (res.ok) {
            const d = await res.json();
            showToast(`Sent $${amount.toFixed(2)} \u2014 tx: ${d.tx_hash.slice(0, 12)}...`, 'success');
            document.getElementById('sendAmount').value = '';
            loadWalletBalances();
        } else {
            const err = await res.json();
            showToast(`Send failed: ${err.detail}`, 'error');
        }
    } catch (e) { showToast('Send failed', 'error'); }
}

async function swapUsdt() {
    const usdtEl = document.getElementById('walletUsdt');
    const usdtVal = parseFloat((usdtEl?.textContent || '').replace('$', '')) || 0;
    if (usdtVal < 0.01) { showToast('No USDT balance', 'error'); return; }
    if (!confirm(`Swap $${usdtVal.toFixed(2)} USDT to USDC.e?`)) return;

    showToast('Swapping...', 'info');
    try {
        const res = await authFetch(`${API}/api/wallet/swap-usdt`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({}),
        });
        if (res.ok) {
            const d = await res.json();
            showToast(`Swap OK \u2014 tx: ${d.tx_hash.slice(0, 12)}...`, 'success');
            loadWalletBalances();
        } else {
            const err = await res.json();
            showToast(`Swap failed: ${err.detail}`, 'error');
        }
    } catch (e) { showToast('Swap failed', 'error'); }
}

// =====================
// RAG
// =====================
async function loadRagCount() {
    try {
        const res = await fetch(`${API}/api/rag/patterns?limit=10000`);
        const data = await res.json();
        const el = document.getElementById('ragPatternCount');
        if (el) el.textContent = data.length;
    } catch (e) { /* ignore */ }
}

async function purgeRag() {
    if (!confirm('Delete ALL RAG patterns? Cannot be undone.')) return;
    try {
        const res = await authFetch(`${API}/api/rag/purge`, { method: 'POST' });
        if (res.ok) { showToast('RAG patterns purged', 'success'); loadRagCount(); }
        else showToast('Purge failed', 'error');
    } catch (e) { showToast('Purge failed', 'error'); }
}

// =====================
// TOAST
// =====================
function showToast(msg, type) {
    const container = document.getElementById('toastContainer') || document.body;
    const toast = document.createElement('div');
    toast.className = `toast ${type || 'info'}`;
    const icon = type === 'success' ? '\u2713' : type === 'error' ? '\u2717' : '\u2139';
    toast.innerHTML = `<span>${icon}</span> ${msg}`;
    container.appendChild(toast);
    setTimeout(() => {
        toast.classList.add('toast-exit');
        toast.addEventListener('animationend', () => toast.remove());
    }, 3000);
}
