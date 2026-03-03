// Polybot Admin — v2

const API = '';

const FIELDS = [
    'max_trade_pct', 'min_trade_usd', 'max_exposure_usd', 'daily_loss_limit_pct',
    'trade_threshold', 'max_spread_cents', 'min_time_remaining_sec',
    'max_consecutive_losses', 'cooldown_rounds', 'kelly_fraction',
    'min_entry_price', 'max_entry_price', 'dry_run'
];

const WEIGHTS = ['momentum', 'book_skew', 'fair_value', 'vol_regime', 'rag_pattern', 'sentiment'];

// --- Auth fetch ---
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
    loadStats();
    loadKeysStatus();
    loadBotStatus();
    loadWalletBalances();

    // Wire up weight sliders
    for (const w of WEIGHTS) {
        const slider = document.getElementById(`w_${w}`);
        const val = document.getElementById(`wv_${w}`);
        if (slider && val) {
            slider.addEventListener('input', () => {
                val.textContent = parseFloat(slider.value).toFixed(2);
                updateWeightSum();
            });
        }
    }
});

// --- Bot Status ---
async function loadBotStatus() {
    try {
        const res = await fetch(`${API}/api/status`);
        const data = await res.json();
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
    } catch (e) {
        // silently fail
    }
}

// --- Collapsible sections ---
function toggleSection(header) {
    const body = header.nextElementSibling;
    const isOpen = header.classList.contains('open');

    if (isOpen) {
        header.classList.remove('open');
        body.classList.remove('open');
    } else {
        header.classList.add('open');
        body.classList.add('open');
    }
}

// --- Weight sum ---
function updateWeightSum() {
    let sum = 0;
    for (const w of WEIGHTS) {
        const slider = document.getElementById(`w_${w}`);
        if (slider) sum += parseFloat(slider.value);
    }

    const fill = document.getElementById('weightSumFill');
    const label = document.getElementById('weightSumLabel');
    if (!fill || !label) return;

    const pct = Math.min(sum * 100, 100);
    fill.style.width = pct + '%';

    const diff = Math.abs(sum - 1.0);
    if (diff < 0.01) {
        fill.className = 'weight-sum-fill';
        label.className = 'weight-sum-label valid';
        label.textContent = `Sum: ${sum.toFixed(2)} \u2714`;
    } else if (sum > 1.0) {
        fill.className = 'weight-sum-fill over';
        label.className = 'weight-sum-label invalid';
        label.textContent = `Sum: ${sum.toFixed(2)} (over!)`;
    } else {
        fill.className = 'weight-sum-fill under';
        label.className = 'weight-sum-label invalid';
        label.textContent = `Sum: ${sum.toFixed(2)} (under)`;
    }
}

// --- Config ---
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

        if (cfg.weights) {
            for (const w of WEIGHTS) {
                const slider = document.getElementById(`w_${w}`);
                const val = document.getElementById(`wv_${w}`);
                if (slider && cfg.weights[w] != null) {
                    slider.value = cfg.weights[w];
                    val.textContent = parseFloat(cfg.weights[w]).toFixed(2);
                }
            }
        }

        updateWeightSum();
    } catch (e) {
        console.error('Failed to load config:', e);
    }
}

async function saveConfig() {
    const cfg = {};
    for (const f of FIELDS) {
        const el = document.getElementById(f);
        if (!el) continue;
        if (f === 'dry_run') {
            cfg[f] = el.value === 'true';
        } else if (['max_consecutive_losses', 'cooldown_rounds', 'max_spread_cents', 'min_time_remaining_sec'].includes(f)) {
            cfg[f] = parseInt(el.value);
        } else {
            cfg[f] = parseFloat(el.value);
        }
    }

    cfg.weights = {};
    for (const w of WEIGHTS) {
        const slider = document.getElementById(`w_${w}`);
        if (slider) cfg.weights[w] = parseFloat(slider.value);
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
        if (res.ok) {
            showToast('Config saved', 'success');
        } else {
            showToast('Failed to save', 'error');
        }
    } catch (e) {
        showToast('Network error', 'error');
    }
}

// --- Stats ---
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
        console.error('Failed to load stats:', e);
    }
}

// --- Bot Control ---
async function startBot() {
    try {
        await authFetch(`${API}/api/bot/start`, { method: 'POST' });
        showToast('Bot started', 'success');
        setTimeout(loadBotStatus, 1000);
    } catch (e) {
        showToast('Failed to start bot', 'error');
    }
}

async function stopBot() {
    try {
        await authFetch(`${API}/api/bot/stop`, { method: 'POST' });
        showToast('Bot stopped', 'success');
        setTimeout(loadBotStatus, 1000);
    } catch (e) {
        showToast('Failed to stop bot', 'error');
    }
}

async function logout() {
    await fetch(`${API}/api/auth/logout`, { method: 'POST' });
    location.href = '/login';
}

// --- API Keys ---
async function loadKeysStatus() {
    try {
        const res = await authFetch(`${API}/api/keys/status`);
        const data = await res.json();
        const el = document.getElementById('credsStatus');
        if (data.has_creds) {
            let msg = '\u2705 API keys configured';
            if (data.balance != null) {
                msg += ` \u2014 Wallet: $${data.balance.toFixed(2)} USDC`;
            }
            el.textContent = msg;
            el.style.color = 'var(--green)';
        } else {
            el.textContent = '\u26a0 No API keys \u2014 running in dry run mode';
            el.style.color = 'var(--yellow)';
        }
    } catch (e) {
        console.error('Keys status failed:', e);
    }
}

async function saveKeys() {
    const data = {
        POLYMARKET_PRIVATE_KEY: document.getElementById('pk').value,
        POLYMARKET_API_KEY: document.getElementById('api_key').value,
        POLYMARKET_API_SECRET: document.getElementById('api_secret').value,
        POLYMARKET_API_PASSPHRASE: document.getElementById('api_passphrase').value,
    };
    for (const k of Object.keys(data)) {
        if (!data[k]) delete data[k];
    }
    if (Object.keys(data).length === 0) {
        showToast('Fill in at least one field', 'error');
        return;
    }
    try {
        const res = await authFetch(`${API}/api/keys/save`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(data),
        });
        if (res.ok) {
            showToast('API keys saved \u2014 restart bot to apply', 'success');
            loadKeysStatus();
        } else {
            showToast('Failed to save keys', 'error');
        }
    } catch (e) {
        showToast('Network error', 'error');
    }
}

async function deriveKeys() {
    const pk = document.getElementById('pk').value;
    if (!pk) {
        showToast('Enter private key first', 'error');
        return;
    }
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
    } catch (e) {
        showToast('Derivation failed', 'error');
    }
}

async function checkBalance() {
    try {
        const res = await authFetch(`${API}/api/keys/status`);
        const data = await res.json();
        if (!data.has_creds) {
            showToast('Save API keys first', 'error');
            return;
        }
        if (data.balance != null) {
            showToast(`Wallet: $${data.balance.toFixed(2)} USDC`, 'success');
        } else {
            showToast('Could not fetch balance', 'error');
        }
    } catch (e) {
        showToast('Balance check failed', 'error');
    }
}

async function saveTelegram() {
    const data = {
        TELEGRAM_TOKEN: document.getElementById('tg_token').value,
        TELEGRAM_CHAT_ID: document.getElementById('tg_chat_id').value,
    };
    for (const k of Object.keys(data)) {
        if (!data[k]) delete data[k];
    }
    try {
        const res = await authFetch(`${API}/api/keys/telegram`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(data),
        });
        if (res.ok) showToast('Telegram config saved', 'success');
        else showToast('Failed to save', 'error');
    } catch (e) {
        showToast('Network error', 'error');
    }
}

// --- Wallet Management ---
async function loadWalletBalances() {
    try {
        const res = await authFetch(`${API}/api/wallet/balances`);
        const d = await res.json();

        const fmt = (v) => v != null ? `$${v.toFixed(2)}` : '\u2014';

        document.getElementById('walletExchange').textContent = fmt(d.exchange_usdc);
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
    } catch (e) {
        console.error('Wallet balances failed:', e);
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
            statusEl.textContent = 'No unredeemed tokens found.';
        } else {
            showToast(`Redeemed ${d.redeemed}/${d.total} positions`, d.redeemed > 0 ? 'success' : 'error');
            const lines = d.results.map(r =>
                `${r.condition_id} ${r.side}: ${r.success ? '\u2713' : '\u2717 ' + r.error}`
            );
            statusEl.innerHTML = lines.join('<br>');
            statusEl.style.color = 'var(--text-dim)';
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

    if (!address || !address.startsWith('0x')) {
        showToast('Enter a valid address', 'error');
        return;
    }
    if (!amount || amount <= 0) {
        showToast('Enter a valid amount', 'error');
        return;
    }
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
    } catch (e) {
        showToast('Send failed: network error', 'error');
    }
}

async function swapUsdt() {
    const usdtEl = document.getElementById('walletUsdt');
    const usdtText = usdtEl ? usdtEl.textContent : '';
    const usdtVal = parseFloat(usdtText.replace('$', '')) || 0;

    if (usdtVal < 0.01) {
        showToast('No USDT balance to swap', 'error');
        return;
    }

    if (!confirm(`Swap $${usdtVal.toFixed(2)} USDT to USDC.e via Uniswap V3?`)) return;

    showToast('Swapping USDT \u2192 USDC.e...', 'info');

    try {
        const res = await authFetch(`${API}/api/wallet/swap-usdt`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({}),
        });
        if (res.ok) {
            const d = await res.json();
            const newBal = d.details?.usdc_balance;
            const msg = newBal != null
                ? `Swapped! New USDC.e: $${newBal.toFixed(2)} \u2014 tx: ${d.tx_hash.slice(0, 12)}...`
                : `Swap OK \u2014 tx: ${d.tx_hash.slice(0, 12)}...`;
            showToast(msg, 'success');
            loadWalletBalances();
        } else {
            const err = await res.json();
            showToast(`Swap failed: ${err.detail}`, 'error');
        }
    } catch (e) {
        showToast('Swap failed: network error', 'error');
    }
}

// --- Toast ---
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
