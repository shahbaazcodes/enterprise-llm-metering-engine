"""Streamlit frontend for the LLM Metering & Billing Engine.

This module provides the main application entry point with:
- Dark enterprise theme styling
- Four interactive tabs: Playground, Analytics, Telemetry, Subscriptions
- Sidebar for global configuration (API URL, tenant selection, health status)
"""

from __future__ import annotations

from pathlib import Path
import sys

import streamlit as st  # noqa: E402

# Ensure the UI package can be imported when run via ``streamlit run ui/app.py``.
ui_root = Path(__file__).parent.resolve()
if str(ui_root.parent) not in sys.path:
    sys.path.insert(0, str(ui_root.parent))  # noqa: E402

from ui.api_client import create_client  # noqa: E402
from ui.components import analytics, playground, subscriptions, telemetry  # noqa: E402

# --------------------------------------------------------------------------- #
# Streamlit app configuration
# --------------------------------------------------------------------------- #
st.set_page_config(
    page_title="LLM Metering Dashboard",
    page_icon="\U0001f4b0",
    layout="wide",
    initial_sidebar_state="expanded",
)


# --------------------------------------------------------------------------- #
# Dark theme custom CSS
# --------------------------------------------------------------------------- #
DARK_THEME_CSS = """
/* Streamlit dark theme override */
[data-testid="stAppViewContainer"] {
    background-color: #0a0a0a;
}
[data-testid="stSidebar"] {
    background-color: #1a1a1a;
}
[data-testid="stMetric"] {
    background-color: #1e1e1e;
    border-radius: 8px;
    padding: 12px;
}
.stButton > button {
    background-color: #4a90d9;
    color: white;
    border: none;
    border-radius: 6px;
    padding: 8px 16px;
}
.stButton > button:hover {
    background-color: #357ab8;
}
.stTabs [data-baseweb="tab-list"] {
    background-color: #1e1e1e;
}
"""

st.markdown(f"<style>{DARK_THEME_CSS}</style>", unsafe_allow_html=True)


# --------------------------------------------------------------------------- #
# Session state initialization
# --------------------------------------------------------------------------- #
if "api_base_url" not in st.session_state:
    st.session_state.api_base_url = "http://localhost:8000"

if "selected_tenant" not in st.session_state:
    st.session_state.selected_tenant = "default-tenant"

if "api_client" not in st.session_state:
    st.session_state.api_client = create_client(st.session_state.api_base_url)

if "health_status" not in st.session_state:
    st.session_state.health_status = "unknown"


# --------------------------------------------------------------------------- #
# Sidebar layout
# --------------------------------------------------------------------------- #
with st.sidebar:
    st.title("\U0001f4b0 LLM Metering")
    st.caption("Billing Engine Dashboard")

    # API Base URL
    st.markdown("---")
    st.markdown("**Configuration**")
    api_url = st.text_input(
        "API Base URL",
        value=st.session_state.api_base_url,
        key="api_url_input",
        help="The FastAPI backend URL",
    )
    if api_url != st.session_state.api_base_url:
        st.session_state.api_base_url = api_url
        st.session_state.api_client = create_client(api_url)

    # Tenant selection
    st.markdown("---")
    st.markdown("**Tenant**")
    tenant = st.text_input(
        "Tenant ID",
        value=st.session_state.selected_tenant,
        key="tenant_input",
        help="Select or enter a tenant slug",
    )
    if tenant != st.session_state.selected_tenant:
        st.session_state.selected_tenant = tenant

    # Health indicator
    st.markdown("---")
    st.markdown("**Status**")

    health_icon = "\u2753"
    health_color = "gray"
    health_label = "Unknown"

    try:
        client = st.session_state.get("api_client")
        if client:
            health = client.check_health()
            status = health.get("status", "unknown")
            if status == "ok":
                health_icon = "\u2705"
                health_color = "green"
                health_label = "Healthy"
            elif status == "degraded":
                health_icon = "\u26a0\ufe0f"
                health_color = "orange"
                health_label = "Degraded"
            else:
                health_icon = "\u274c"
                health_color = "red"
                health_label = "Unhealthy"
    except Exception:
        health_icon = "\u274c"
        health_color = "red"
        health_label = "Offline"

    st.markdown(
        f'<span style="font-size: 24px;">{health_icon}</span>'
        f'<span style="color: {health_color}; font-weight: bold;"> {health_label}</span>',
        unsafe_allow_html=True,
    )

    # Quick refresh buttons
    st.markdown("---")
    st.markdown("**Quick Actions**")
    if st.button("\U0001f504 Refresh", use_container_width=True, key="quick_refresh"):
        st.rerun()

    if st.button("\U0001f3e0 Go to Playground", use_container_width=True, key="go_playground"):
        st.rerun()


# --------------------------------------------------------------------------- #
# Main content layout
# --------------------------------------------------------------------------- #
st.title("\U0001f4ca LLM Usage Metering Dashboard")
st.caption("Monitor, analyze, and simulate billing for your LLM providers")

# Tab navigation
tab1, tab2, tab3, tab4 = st.tabs(
    [
        "\U0001f3ae Playground",
        "\U0001f4c8 Analytics",
        "\U0001f6f0\ufe0f Telemetry",
        "\U0001f4cb Subscriptions",
    ]
)

# Render each tab
with tab1:
    st.markdown("Interact with the metering engine directly from the playground.")
    st.divider()
    playground.render(st.session_state.api_client)

with tab2:
    st.markdown("Explore tenant usage patterns, token distributions, and cost breakdowns.")
    st.divider()
    analytics.render(st.session_state.api_client)

with tab3:
    st.markdown("View real-time system health and Prometheus metrics.")
    st.divider()
    telemetry.render(st.session_state.api_client)

with tab4:
    st.markdown("View plan details and simulate Stripe webhook events.")
    st.divider()
    subscriptions.render(st.session_state.api_client)


# --------------------------------------------------------------------------- #
# Footer
# --------------------------------------------------------------------------- #
st.markdown("---")
st.caption(
    "Enterprise LLM Metering & Billing Engine v1.0.0 | "
    "All amounts in micro-cents (1 USD = 1,000,000 \u00b5\u00a2)"
)
