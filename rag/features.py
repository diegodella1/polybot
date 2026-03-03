"""Convert TA snapshot to feature vector for RAG pattern matching."""

import math
import numpy as np
from datetime import datetime, timezone


def snapshot_to_features(snapshot: dict, book_imbalance: float = 0.0) -> np.ndarray:
    """Convert a TA snapshot to a normalized feature vector.

    Features (8 dims):
        0: ret_1m
        1: ret_5m
        2: ret_15m
        3: vol_ratio (ATR5/ATR20)
        4: rsi_14 (scaled to 0-1)
        5: hour_sin (cyclic hour encoding)
        6: hour_cos
        7: book_imbalance
    """
    now = datetime.now(timezone.utc)
    hour = now.hour + now.minute / 60.0

    features = np.array([
        snapshot.get("ret_1m") or 0.0,
        snapshot.get("ret_5m") or 0.0,
        snapshot.get("ret_15m") or 0.0,
        snapshot.get("vol_ratio") or 1.0,
        (snapshot.get("rsi_14") or 50.0) / 100.0,
        math.sin(2 * math.pi * hour / 24.0),
        math.cos(2 * math.pi * hour / 24.0),
        book_imbalance,
    ], dtype=np.float32)

    return features


def normalize_features(features: np.ndarray, mean: np.ndarray, std: np.ndarray) -> np.ndarray:
    """Z-score normalization."""
    safe_std = np.where(std == 0, 1.0, std)
    return (features - mean) / safe_std
