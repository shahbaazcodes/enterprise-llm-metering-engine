"""Tab 3: System Health & Prometheus Metrics.

Real-time dependency status indicators (PostgreSQL, Redis, LocalStack S3)
plus raw Prometheus operational metrics exposition.
"""

from __future__ import annotations

import re

import streamlit as st

from ui.api_client import MeteringApiClient


def render(api_client: MeteringApiClient) -> None:
    """Render the telemetry tab content."""
    st.subheader("\U0001f6e0\ufe0f System Health & Prometheus Metrics")

    # ---- Dependency health status ----\
    st.markdown("### Dependency Status")

    col_a, col_b, col_c = st.columns(3)

    with col_a:
        st.markdown("**PostgreSQL**")
        try:
            health = api_client.check_health()
            db_status = health.get("database", "unknown")
            if db_status == "up":
                st.success("✅ Up")
            else:
                st.error("❌ Down")
        except Exception:
            st.error("❌ Unreachable")
        st.caption("Backend dependency health probe via `GET /health`")

    with col_b:
        st.markdown("**Redis**")
        try:
            health = api_client.check_health()
            redis_status = health.get("redis", "unknown")
            if redis_status == "up":
                st.success("✅ Up")
            else:
                st.error("❌ Down")
        except Exception:
            st.error("❌ Unreachable")
        st.caption("Idempotency locks + quota Lua scripts")

    with col_c:
        st.markdown("**LocalStack S3**")
        try:
            health = api_client.check_health()
            # S3 status is not part of /health response; derive from overall
            overall = health.get("status", "unknown")
            if overall == "ok":
                st.success("✅ Up")
            elif overall == "degraded":
                st.warning("⚠️ Degraded")
            else:
                st.error("❌ Down")
        except Exception:
            st.error("❌ Unreachable")
        st.caption("Audit archiver for immutable usage ledger")

    st.divider()

    # ---- Prometheus metrics ----\
    st.markdown("### Prometheus Metrics")

    col_d, col_e = st.columns([1, 4])
    with col_d:
        if st.button("Refresh Metrics", use_container_width=True, key="refresh_metrics"):
            st.rerun()
    with col_e:
        st.caption("Raw text exposition format from `GET /metrics`")

    try:
        metrics_text = api_client.get_metrics()

        # ---- Highlight key metrics as cards ----\
        metric_names = [
            "http_requests_total",
            "llm_tokens_total",
            "llm_cost_microcents_total",
            "llm_usage_events_persisted_total",
            "ml_anomalies_detected_total",
            "rate_limit_rejections_total",
            "payment_required_total",
            "llm_webhook_events_total",
        ]

        # Parse values for the selected metrics
        lines = metrics_text.splitlines()
        parsed: dict[str, list[float]] = {}
        current_metric = None
        for line in lines:
            if line.startswith("#") or not line.strip():
                continue
            match = re.match(r"^([a-zA-Z_:][a-zA-Z0-9_:]*)\b", line)
            if match:
                current_metric = match.group(1)
            if current_metric and current_metric in metric_names:
                # Extract the last numeric value
                val_match = re.findall(r"[\d.]+", line)
                if val_match:
                    parsed.setdefault(current_metric, []).append(float(val_match[-1]))

        if parsed:
            card_cols = st.columns(min(len(parsed), 4))
            for i, (mname, values) in enumerate(parsed.items()):
                latest = values[-1] if values else 0.0
                with card_cols[i % 4]:
                    st.metric(label=mname, value=f"{latest:,.0f}")

        st.divider()

        # ---- Raw metrics display ----\
        st.text_area(
            "Raw Prometheus Output",
            value=metrics_text,
            height=400,
            key="raw_metrics",
        )

    except Exception as exc:
        st.error(f"Failed to fetch metrics: {exc}")
