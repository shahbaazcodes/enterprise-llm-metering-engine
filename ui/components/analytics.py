"""Tab 2: Tenant Usage & Cost Analytics.

Per-model token and cost breakdown using Plotly bar charts.
"""

from __future__ import annotations

import plotly.graph_objects as go
import streamlit as st

from ui.utils import format_tokens, usage_to_usd


def render(api_client) -> None:
    """Render the analytics tab with usage and cost graphs."""
    st.subheader("⭕ Tenant Usage & Cost Analytics")

    # ---- Tenant selector ----
    with st.container(border=True):
        col_a, col_b = st.columns([3, 1])
        with col_a:
            tenant = col_a.text_input(
                "Tenant slug",
                value=st.session_state.get("selected_tenant", "default-tenant"),
                key="analytics_tenant_input",
            )
        with col_b:
            if col_b.button(
                "Load",
                type="primary",
                use_container_width=True,
                key="analytics_load_btn",
            ):
                st.session_state["selected_tenant"] = tenant
                st.rerun()

    if "selected_tenant" not in st.session_state:
        st.info("Please enter a tenant slug to view analytics.")
        return

    # ---- Fetch data ----
    with st.spinner("Loading analytics..."):
        try:
            data = api_client.get_tenant_usage(st.session_state["selected_tenant"])
        except Exception as exc:
            st.error(f"Failed to load usage data: {exc}")
            return

    # ---- Summary metric cards ----
    col1, col2, col3 = st.columns(3)
    with col1:
        st.metric("Total Spend", usage_to_usd(data.get("total_cost_microcents", 0)))
    with col2:
        st.metric("Total Tokens", format_tokens(data.get("total_tokens", 0)))
    with col3:
        remaining = data.get("remaining_microcents", 0)
        limit = data.get("plan_limit_microcents", 0)
        st.metric("Remaining Quota", usage_to_usd(remaining) if limit else "∞")

    # ---- Per-model breakdown ----
    breakdown = data.get("by_model", [])
    if not breakdown:
        st.info("No usage data available for this tenant.")
        return

    # Prepare tables and charts
    st.divider()
    st.markdown("### Per-Model Breakdown")

    # ---- Bar chart: tokens by model ----
    models = [b.get("model", "") for b in breakdown]
    tokens = [b.get("total_tokens", 0) for b in breakdown]
    costs = [b.get("cost_microcents", 0) for b in breakdown]
    requests = [b.get("requests", 0) for b in breakdown]

    # ---- Tokens per model chart ----
    fig_tokens = go.Figure(
        data=go.Bar(
            x=models,
            y=tokens,
            text=[format_tokens(t) for t in tokens],
            textposition="auto",
            marker_color="#1f77b4",
        )
    )
    fig_tokens.update_layout(
        title="Tokens Generated per Model",
        xaxis_title="Model",
        yaxis_title="Tokens",
        template="plotly_dark",
        height=400,
    )
    st.plotly_chart(fig_tokens, use_container_width=True)

    # ---- Cost per model chart ----
    fig_cost = go.Figure(
        data=go.Bar(
            x=models,
            y=costs,
            text=[usage_to_usd(c) for c in costs],
            textposition="auto",
            marker_color="#ff7f0e",
        )
    )
    fig_cost.update_layout(
        title="Cost (USD) per Model",
        xaxis_title="Model",
        yaxis_title="Cost USD",
        template="plotly_dark",
        height=400,
    )
    st.plotly_chart(fig_cost, use_container_width=True)

    # ---- Requests per model chart ----
    fig_reqs = go.Figure(
        data=go.Bar(
            x=models,
            y=requests,
            text=requests,
            textposition="auto",
            marker_color="#2ca02c",
        )
    )
    fig_reqs.update_layout(
        title="API Requests per Model",
        xaxis_title="Model",
        yaxis_title="Requests",
        template="plotly_dark",
        height=400,
    )
    st.plotly_chart(fig_reqs, use_container_width=True)

    # ---- Data table ----
    with st.expander("Detailed breakdown table"):
        import pandas as pd

        df = pd.DataFrame(
            [
                {
                    "Model": b.get("model", ""),
                    "Requests": b.get("requests", 0),
                    "Input Tokens": b.get("input_tokens", 0),
                    "Output Tokens": b.get("output_tokens", 0),
                    "Total Tokens": b.get("total_tokens", 0),
                    "Cost (µ¢)": b.get("cost_microcents", 0),
                    "Cost (USD)": usage_to_usd(b.get("cost_microcents", 0)),
                }
                for b in breakdown
            ]
        )
        st.dataframe(df, use_container_width=True, hide_index=True)

    # ---- Plan information ----
    st.divider()
    st.markdown("### Plan Information")
    plan = data.get("plan", "free")
    st.caption(f"Plan: **{plan}**")
    st.caption(f"Billing period: {data.get('period', 'N/A')}")
    st.caption(f"Period since: {data.get('since', 'N/A')}")
    st.caption(f"Period until: {data.get('until', 'N/A')}")

    # ---- Raw API response (debug) ----
    with st.expander("Raw API response"):
        st.json(data)