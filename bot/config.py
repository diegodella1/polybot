"""Config loader with hot-reload from config.yaml."""

import logging
import os
import yaml

CONFIG_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "config.yaml")

_config: dict = {}
_mtime: float = 0.0

logger = logging.getLogger(__name__)

# Validation rules: key -> (min, max)
_VALIDATORS = {
    "kelly_fraction": (0.01, 1.0),
    "max_trade_pct": (0.01, 0.20),
    "min_trade_usd": (0.5, 50.0),
    "max_exposure_usd": (1.0, 1000.0),
    "daily_loss_limit_pct": (0.01, 0.50),
    "min_drawdown_multiplier": (0.1, 1.0),
    "bankroll_floor_usd": (2.0, 50.0),
    "trade_threshold": (0.01, 0.90),
    "max_spread_cents": (1, 20),
    "min_time_remaining_sec": (10, 300),
    "max_consecutive_losses": (1, 20),
    "cooldown_rounds": (1, 50),
    "min_entry_price": (0.01, 0.50),
    "max_entry_price": (0.50, 0.99),
    "stop_loss_pct": (0.05, 0.90),
    "take_profit_pct": (0.05, 2.00),
    "min_win_rate": (0, 0.60),
    "min_estimated_winrate": (0.50, 0.70),
    "max_estimated_winrate": (0.55, 0.80),
    "trade_cooldown_seconds": (0, 3600),
}


def _validate_config(cfg: dict) -> dict:
    """Clamp config values to safe ranges. Logs warnings for out-of-range values."""
    for key, (lo, hi) in _VALIDATORS.items():
        if key in cfg:
            val = cfg[key]
            if isinstance(val, (int, float)):
                clamped = max(lo, min(hi, val))
                if clamped != val:
                    logger.warning(
                        "Config '%s' = %s out of range [%s, %s], clamped to %s",
                        key, val, lo, hi, clamped,
                    )
                    cfg[key] = clamped

    # Validate weight values
    weights = cfg.get("weights", {})
    if isinstance(weights, dict):
        for wk, wv in weights.items():
            if isinstance(wv, (int, float)):
                clamped = max(0.0, min(1.0, wv))
                if clamped != wv:
                    logger.warning("Weight '%s' = %s clamped to [0, 1]", wk, wv)
                    weights[wk] = clamped

    return cfg


def load_config() -> dict:
    global _config, _mtime
    try:
        current_mtime = os.path.getmtime(CONFIG_PATH)
    except OSError:
        return _config

    if current_mtime != _mtime:
        with open(CONFIG_PATH) as f:
            raw = yaml.safe_load(f)
        _config = _validate_config(raw) if raw else {}
        _mtime = current_mtime
    return _config


def save_config(data: dict):
    global _config, _mtime
    data = _validate_config(data)
    with open(CONFIG_PATH, "w") as f:
        yaml.dump(data, f, default_flow_style=False, sort_keys=False)
    _config = data
    _mtime = os.path.getmtime(CONFIG_PATH)


def get(key: str, default=None):
    cfg = load_config()
    keys = key.split(".")
    val = cfg
    for k in keys:
        if isinstance(val, dict):
            val = val.get(k)
        else:
            return default
        if val is None:
            return default
    return val
