"""LLM Usage Metering & Billing Engine.

A production-grade metering and billing plane for LLM traffic:

* **Exact money.** Every monetary value is an integer count of micro-cents
  (1 USD = 1_000_000 micro-cents). No floats, no drift.
* **Exactly-once.** Redis ``SET NX EX`` locks plus a unique index on
  ``idempotency_key`` guarantee a retried request is billed exactly once.
* **Two-speed rejection.** ``402 Payment Required`` for inactive/past-due
  entitlements, ``429 Too Many Requests`` for quota/rate-limit exhaustion.
* **Anomaly defence.** A scikit-learn ``IsolationForest`` guard flags runaway
  loops, DDoS spikes and abnormal token consumption in real time.
"""

__all__ = ["__version__"]

__version__ = "1.0.0"
