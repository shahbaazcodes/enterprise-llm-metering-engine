"""Real-time anomaly detection with scikit-learn's ``IsolationForest``.

What it protects against
------------------------
``runaway_loop``
    An agent that keeps calling tools/pivoting without converging - visible
    as an exploding step count or tokens-per-step ratio.
``ddos_spike``
    A burst of requests from one tenant - visible as a request rate far
    above the learned baseline.
``abnormal_token_consumption``
    A single request that consumes an extreme number of tokens (or a tenant
    whose cost per request suddenly explodes) - visible as a token/cost
    z-score far above the baseline.

Design notes
------------
* **Two models.** A per-tenant model (accurate, personalised) with a
  **global** model as fallback (covers cold-start tenants).  Both are
  ``IsolationForest`` instances trained on a rolling window of feature
  vectors.
* **Hard rules first.** Deterministic guards (token flood, step explosion)
  fire even before the model has enough samples, so a cold start is never a
  free pass for an attacker.
* **Never blocks the ledger.** The guard *annotates* the usage event
  (``is_anomaly`` / ``anomaly_type``) and raises a Prometheus counter.  It
  never silently drops billable usage - money owed is always recorded.
"""

from __future__ import annotations

import asyncio
from collections import deque
from dataclasses import dataclass
import logging
import math

import numpy as np
from sklearn.ensemble import IsolationForest

from app import metrics
from app.settings import Settings, get_settings

logger = logging.getLogger("llm.metering.ml_guard")

FEATURE_NAMES: tuple[str, ...] = (
    "total_tokens",
    "input_tokens",
    "output_tokens",
    "reasoning_tokens",
    "cached_ratio",
    "cost_microcents",
    "steps",
    "tokens_per_step",
    "window_request_count",
    "cost_zscore",
)


@dataclass(frozen=True, slots=True)
class UsageSample:
    """A single observation fed to the anomaly detector."""

    tenant_id: str
    model: str
    total_tokens: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    reasoning_tokens: int = 0
    cached_input_tokens: int = 0
    cost_microcents: int = 0
    steps: int = 1
    window_request_count: int = 1
    window_cost_microcents: int = 0
    latency_ms: int = 0

    def cached_ratio(self) -> float:
        denominator = self.input_tokens + self.cached_input_tokens
        if denominator <= 0:
            return 0.0
        return self.cached_input_tokens / denominator


@dataclass(frozen=True, slots=True)
class AnomalyResult:
    """Verdict of the anomaly guard."""

    is_anomaly: bool
    score: float
    anomaly_type: str | None = None
    reason: str = "normal"
    threshold: float = 0.0
    model: str = "none"

    def to_dict(self) -> dict[str, object]:
        return {
            "is_anomaly": self.is_anomaly,
            "score": round(self.score, 6),
            "anomaly_type": self.anomaly_type,
            "reason": self.reason,
            "model": self.model,
        }


def _zscore(value: float, mean: float, std: float) -> float:
    """Robust z-score (std floor avoids a division by zero)."""
    if std <= 1e-9:
        return 0.0
    return (value - mean) / std


class AnomalyGuard:
    """Rolling-window ``IsolationForest`` anomaly detector."""

    def __init__(self, settings: Settings | None = None) -> None:
        self._settings = settings or get_settings()
        self._enabled = self._settings.ml_guard_enabled
        self._min_samples = max(4, self._settings.ml_min_samples)
        self._refit_interval = max(1, self._settings.ml_refit_interval)
        self._threshold = self._settings.ml_score_threshold
        self._max_tokens = self._settings.ml_max_tokens_per_request
        self._max_steps = self._settings.ml_max_steps_per_request
        self._window = self._settings.ml_window_size

        self._global_buffer: deque[list[float]] = deque(maxlen=self._window)
        self._tenant_buffers: dict[str, deque[list[float]]] = {}
        self._global_model: IsolationForest | None = None
        self._tenant_models: dict[str, IsolationForest] = {}
        self._global_fit_size = 0
        self._tenant_fit_size: dict[str, int] = {}
        self._evaluated = 0
        self._anomalies = 0

    # -- feature engineering ------------------------------------------------
    def _features(self, sample: UsageSample, buffer: deque[list[float]]) -> list[float]:
        """Build the feature vector, including the rolling cost z-score."""
        costs = [row[5] for row in buffer]
        if len(costs) >= 2:
            mean = float(np.mean(costs))
            std = float(np.std(costs))
        else:
            mean, std = float(sample.cost_microcents), 0.0
        zscore = _zscore(float(sample.cost_microcents), mean, std)
        tokens_per_step = sample.total_tokens / max(1, sample.steps)
        return [
            float(sample.total_tokens),
            float(sample.input_tokens),
            float(sample.output_tokens),
            float(sample.reasoning_tokens),
            sample.cached_ratio(),
            float(sample.cost_microcents),
            float(sample.steps),
            float(tokens_per_step),
            float(sample.window_request_count),
            zscore,
        ]

    def _buffer_for(self, tenant_id: str) -> deque[list[float]]:
        buffer = self._tenant_buffers.get(tenant_id)
        if buffer is None:
            buffer = deque(maxlen=self._window)
            self._tenant_buffers[tenant_id] = buffer
        return buffer

    # -- model lifecycle ----------------------------------------------------
    def _new_model(self) -> IsolationForest:
        return IsolationForest(
            n_estimators=100,
            contamination=self._settings.ml_contamination,
            max_samples="auto",
            random_state=42,
            n_jobs=1,
        )

    def _fit(self, rows: list[list[float]]) -> IsolationForest | None:
        if len(rows) < self._min_samples:
            return None
        model = self._new_model()
        model.fit(np.asarray(rows, dtype=np.float64))
        return model

    async def _fit_async(self, buffer: deque[list[float]]) -> IsolationForest | None:
        """Fit off the event loop - scikit-learn is CPU bound and blocking."""
        snapshot = list(buffer)
        return await asyncio.to_thread(self._fit, snapshot)

    async def _maybe_refit(self, tenant_id: str, features: list[float]) -> None:
        """Refit the per-tenant and global models when the window grew enough."""
        tenant_buffer = self._buffer_for(tenant_id)
        tenant_buffer.append(features)
        self._global_buffer.append(features)

        if len(tenant_buffer) - self._tenant_fit_size.get(tenant_id, 0) >= self._refit_interval:
            model = await self._fit_async(tenant_buffer)
            if model is not None:
                self._tenant_models[tenant_id] = model
                self._tenant_fit_size[tenant_id] = len(tenant_buffer)

        if len(self._global_buffer) - self._global_fit_size >= self._refit_interval:
            model = await self._fit_async(self._global_buffer)
            if model is not None:
                self._global_model = model
                self._global_fit_size = len(self._global_buffer)

    # -- evaluation ---------------------------------------------------------
    async def evaluate(self, sample: UsageSample) -> AnomalyResult:
        """Score a request and decide whether it is anomalous.

        The sample is appended to the rolling window *before* scoring, so the
        detector always learns from live traffic.
        """
        self._evaluated += 1
        if not self._enabled:
            return AnomalyResult(is_anomaly=False, score=0.0, reason="guard_disabled")

        # --- deterministic hard rules (always active) ----------------------
        if sample.total_tokens > self._max_tokens:
            return self._flag(
                "token_flood",
                f"total_tokens={sample.total_tokens} exceeds the hard cap {self._max_tokens}",
                score=0.0,
            )
        if sample.steps > self._max_steps:
            return self._flag(
                "runaway_loop",
                f"steps={sample.steps} exceeds the hard cap {self._max_steps}",
                score=0.0,
            )

        tenant_buffer = self._buffer_for(sample.tenant_id)
        features = self._features(sample, tenant_buffer)
        await self._maybe_refit(sample.tenant_id, features)

        model = self._tenant_models.get(sample.tenant_id) or self._global_model
        if model is None:
            return AnomalyResult(is_anomaly=False, score=0.0, reason="warming_up", model="none")

        score = float(model.decision_function(np.asarray([features], dtype=np.float64))[0])
        model_name = "tenant" if sample.tenant_id in self._tenant_models else "global"
        if score >= self._threshold:
            return AnomalyResult(
                is_anomaly=False,
                score=score,
                reason="within_baseline",
                model=model_name,
            )

        anomaly_type = self._classify(sample, features)
        return self._flag(anomaly_type, f"isolation_forest score={score:.4f}", score=score)

    def _classify(self, sample: UsageSample, features: list[float]) -> str:
        """Map a flagged observation onto a human-readable anomaly type."""
        tokens_per_step = features[7]
        window_requests = features[8]
        cost_zscore = features[9]
        if sample.steps > self._max_steps or tokens_per_step > 20_000:
            return "runaway_loop"
        if window_requests > 100 and cost_zscore > 2.0:
            return "ddos_spike"
        if cost_zscore > 1.0:
            return "abnormal_token_consumption"
        return "statistical_outlier"

    def _flag(self, anomaly_type: str, reason: str, score: float) -> AnomalyResult:
        self._anomalies += 1
        metrics.ML_ANOMALIES_DETECTED_TOTAL.labels(anomaly_type=anomaly_type).inc()
        logger.warning(
            "ml anomaly detected", extra={"anomaly_type": anomaly_type, "reason": reason}
        )
        return AnomalyResult(
            is_anomaly=True,
            score=score,
            anomaly_type=anomaly_type,
            reason=reason,
            threshold=self._threshold,
        )

    # -- introspection ------------------------------------------------------
    def stats(self) -> dict[str, object]:
        """Operational snapshot of the guard."""
        return {
            "enabled": self._enabled,
            "evaluated": self._evaluated,
            "anomalies": self._anomalies,
            "global_samples": len(self._global_buffer),
            "global_model_trained": self._global_model is not None,
            "tenant_models": len(self._tenant_models),
            "feature_names": list(FEATURE_NAMES),
            "threshold": self._threshold,
        }

    @property
    def anomaly_rate(self) -> float:
        if self._evaluated == 0:
            return 0.0
        return self._anomalies / self._evaluated

    def _is_finite(self, value: float) -> bool:
        return not math.isnan(value) and not math.isinf(value)
