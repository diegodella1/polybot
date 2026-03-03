"""k-NN pattern matching using cosine similarity with NumPy."""

import logging
import numpy as np

from db import get_db

logger = logging.getLogger(__name__)

MIN_PATTERNS_FOR_SIGNAL = 20
TOP_K = 10


class PatternStore:
    def __init__(self):
        self._matrix: np.ndarray | None = None  # (N, 8) features
        self._outcomes: list[str] = []  # 'win' or 'loss'
        self._norms: np.ndarray | None = None
        self._loaded = False

    async def load(self):
        """Load all patterns from DB into memory."""
        db = await get_db()
        try:
            cursor = await db.execute(
                "SELECT features, outcome FROM patterns ORDER BY id"
            )
            rows = await cursor.fetchall()

            if not rows:
                self._loaded = True
                return

            features_list = []
            outcomes = []
            for row in rows:
                vec = np.frombuffer(row["features"], dtype=np.float32)
                if len(vec) == 8:
                    features_list.append(vec)
                    outcomes.append(row["outcome"])

            if features_list:
                self._matrix = np.stack(features_list)
                self._outcomes = outcomes
                self._norms = np.linalg.norm(self._matrix, axis=1)

            self._loaded = True
            logger.info("Loaded %d patterns", len(self._outcomes))
        finally:
            await db.close()

    async def store(self, trade_id: int, features: np.ndarray, outcome: str):
        """Store a new pattern after trade resolution."""
        db = await get_db()
        try:
            await db.execute(
                "INSERT INTO patterns (trade_id, features, outcome) VALUES (?, ?, ?)",
                (trade_id, features.tobytes(), outcome),
            )
            await db.commit()
        finally:
            await db.close()

        # Update in-memory cache
        if self._matrix is not None:
            self._matrix = np.vstack([self._matrix, features.reshape(1, -1)])
            self._outcomes.append(outcome)
            norm = np.linalg.norm(features)
            self._norms = np.append(self._norms, norm)
        else:
            self._matrix = features.reshape(1, -1)
            self._outcomes = [outcome]
            self._norms = np.array([np.linalg.norm(features)])

    def query(self, current_features: np.ndarray) -> float:
        """Find top-K similar patterns and return signal.

        Returns signal in [-1.0, 1.0]:
            positive = historical bias toward Up
            negative = historical bias toward Down

        Returns 0.0 if insufficient data.
        """
        if (
            self._matrix is None
            or len(self._outcomes) < MIN_PATTERNS_FOR_SIGNAL
        ):
            return 0.0

        # Cosine similarity: (M @ v) / (norms * norm_v)
        current_norm = np.linalg.norm(current_features)
        if current_norm == 0:
            return 0.0

        similarities = (self._matrix @ current_features) / (
            self._norms * current_norm + 1e-8
        )

        # Top K most similar
        top_k_idx = np.argsort(similarities)[-TOP_K:]
        top_outcomes = [self._outcomes[i] for i in top_k_idx]

        win_count = sum(1 for o in top_outcomes if o == "win")
        win_ratio = win_count / len(top_outcomes)

        # Convert to signal: 0.5 → 0, 0.7 → +0.4, 0.3 → -0.4
        signal = (win_ratio - 0.5) * 2.0
        return max(-1.0, min(1.0, signal))

    @property
    def pattern_count(self) -> int:
        return len(self._outcomes)
