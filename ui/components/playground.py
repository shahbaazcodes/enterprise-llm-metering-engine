"""Tab 1: Live Metered Generation Playground.

All monetary values are micro-cents unless explicitly noted.  1 USD = 1_000_000 µ¢.
"""

from __future__ import annotations

import streamlit as st

from ui.utils import (
    format_microcents,
    format_tokens,
    microcents_to_usd_str,
    usage_to_usd,
)

MODELS = [
    "gpt-4o",
    "gpt-4o-mini",
    "claude-3-5-sonnet",
    "o3-mini",
    "claude-sonnet-4",
    "gemini-1.5-pro",
]


def render(api_client) -> None:
    """Render the playground tab content."""
    st.subheader("\u2753 Live Metered Generation")

    col_a, col_b = st.columns([3, 1])
    with col_a:
        prompt = st.text_area(
            "Prompt",
            height=120,
            placeholder="Enter your LLM prompt here...",
        )
    with col_b:
        model = st.selectbox("Model", MODELS, index=0)
        max_output = st.number_input(
            "Max Output Tokens", min_value=1, max_value=4096, value=256, step=1
        )
        temperature = st.slider("Temperature", min_value=0.0, max_value=2.0, value=0.7, step=0.1)
        idempotency_key = st.text_input(
            "Idempotency-Key",
            value="",
            help="Leave blank for new request; reuse to test exactly-once billing.",
        )

    if st.button("Generate \u25b6", type="primary", use_container_width=True):
        if not prompt.strip():
            st.error("Please enter a prompt.")
            return

        with st.spinner("Generating..."):
            try:
                result = api_client.generate_completion(
                    tenant_id=st.session_state.get("selected_tenant", "default-tenant"),
                    model=model,
                    prompt=prompt,
                    idempotency_key=idempotency_key or None,
                    max_output_tokens=max_output,
                    temperature=temperature,
                )

                output = result.get("output", "")
                usage = result.get("usage", {})
                cost = result.get("cost", {})
                quota = result.get("quota", {})
                anomaly = result.get("anomaly", {})

                with st.container(border=True):
                    st.markdown("### Response")
                    if output:
                        st.success(output)
                    else:
                        st.info("_No output returned_")

                total_microcents = cost.get("total_microcents", 0)
                input_microcents = cost.get("input_microcents", 0)
                output_microcents = cost.get("output_microcents", 0)

                col1, col2, col3 = st.columns(3)
                with col1:
                    st.metric(label="Input Cost", value=microcents_to_usd_str(input_microcents))
                with col2:
                    st.metric(label="Output Cost", value=microcents_to_usd_str(output_microcents))
                with col3:
                    st.metric(
                        label="Total Cost",
                        value=microcents_to_usd_str(total_microcents),
                        delta=None,
                    )

                col_a, col_b, col_c = st.columns(3)
                with col_a:
                    st.write("Input tokens:", format_tokens(usage.get("input_tokens", 0)))
                with col_b:
                    st.write("Output tokens:", format_tokens(usage.get("output_tokens", 0)))
                with col_c:
                    st.write("Total tokens:", format_tokens(usage.get("total_tokens", 0)))

                st.divider()
                st.markdown("### Monthly Spend Quota")
                limit_microcents = quota.get("limit_microcents", 0)
                used_microcents = quota.get("used_microcents", 0)
                remaining_microcents = quota.get("remaining_microcents", 0)

                if limit_microcents > 0:
                    pct_used = (used_microcents / limit_microcents) * 100
                    st.progress(max(0, min(100, pct_used / 100)))
                    col_a, col_b = st.columns(2)
                    with col_a:
                        st.caption(
                            f"Used: {format_microcents(used_microcents)} ({usage_to_usd(used_microcents)})"
                        )
                    with col_b:
                        st.caption(
                            f"Remaining: {format_microcents(remaining_microcents)} ({usage_to_usd(remaining_microcents)})"
                        )
                    st.caption(
                        f"Plan limit: {format_microcents(limit_microcents)} ({usage_to_usd(limit_microcents)})"
                    )
                else:
                    st.caption("No spending limit configured (unlimited quota).")

                if anomaly and anomaly.get("is_anomaly"):
                    st.warning(
                        f"\u26a0\ufe0f Anomaly detected: **{anomaly.get('anomaly_type', 'unknown')}** "
                        f"(score: {anomaly.get('score', 0):.2f}) \u2014 {anomaly.get('reason', '')}"
                    )

            except Exception as exc:
                st.error(f"Unexpected error: {exc}")

    with st.expander("Change tenant"):
        new_tenant = st.text_input(
            "Tenant slug", value=st.session_state.get("selected_tenant", "default-tenant")
        )
        if st.button("Update tenant", use_container_width=True):
            st.session_state["selected_tenant"] = new_tenant
            st.rerun()
