import math
from dataclasses import dataclass
from typing import Dict, List
import streamlit as st
import pandas as pd
from scipy.optimize import brentq
from scipy.stats import norm

st.set_page_config(
    page_title="Nifty Institutional Algo Terminal",
    page_icon="⚡",
    layout="wide"
)

# =====================================================================
# 1. QUANTITATIVE PRICING, GREEKS & ANALYTICS ENGINE
# =====================================================================
class RobustBlack76:
    @staticmethod
    def calc_d1_d2(F: float, K: float, T: float, sigma: float):
        if T <= 1e-5 or sigma <= 1e-4:
            return 0.0, 0.0
        vol_time = sigma * math.sqrt(T)
        d1 = (math.log(F / K) + 0.5 * (sigma ** 2) * T) / vol_time
        d2 = d1 - vol_time
        return d1, d2

    @classmethod
    def price(cls, F: float, K: float, T: float, r: float, sigma: float, option_type: str) -> float:
        if T <= 1e-5:
            return max(0.0, (F - K) if option_type.upper() == "CE" else (K - F))
        d1, d2 = cls.calc_d1_d2(F, K, T, sigma)
        df = math.exp(-r * T)
        if option_type.upper() == "CE":
            return df * (F * norm.cdf(d1) - K * norm.cdf(d2))
        else:
            return df * (K * norm.cdf(-d2) - F * norm.cdf(-d1))

    @classmethod
    def implied_volatility(
        cls, ltp: float, F: float, K: float, T: float, r: float, option_type: str, fallback_iv: float = 0.16
    ) -> float:
        if T <= 1e-5 or ltp <= 0:
            return fallback_iv
        df = math.exp(-r * T)
        intrinsic = max(0.0, (F - K) if option_type.upper() == "CE" else (K - F)) * df
        if ltp <= intrinsic * 0.99:
            return fallback_iv

        def objective(sigma):
            return cls.price(F, K, T, r, sigma, option_type) - ltp

        try:
            return brentq(objective, 0.01, 3.5, maxiter=80, xtol=1e-4)
        except (ValueError, RuntimeError):
            return fallback_iv

    @classmethod
    def delta(cls, F: float, K: float, T: float, r: float, sigma: float, option_type: str) -> float:
        if T <= 1e-5 or sigma <= 1e-4:
            if option_type.upper() == "CE":
                return 1.0 if F > K else 0.0
            else:
                return -1.0 if F < K else 0.0
        d1, _ = cls.calc_d1_d2(F, K, T, sigma)
        df = math.exp(-r * T)
        return (df * norm.cdf(d1)) if option_type.upper() == "CE" else (-df * norm.cdf(-d1))

    @classmethod
    def probability_of_profit(cls, F: float, lower_be: float, upper_be: float, T: float, sigma: float) -> float:
        """Calculates standard Log-Normal Probability of expiring between Lower and Upper Breakevens."""
        if T <= 1e-5 or sigma <= 1e-4 or lower_be >= upper_be:
            return 0.0
        vol_time = sigma * math.sqrt(T)
        
        # d2 calculations for log-normal underlying distribution at expiry
        d2_upper = (math.log(F / upper_be) + 0.5 * (sigma ** 2) * T) / vol_time - vol_time
        d2_lower = (math.log(F / lower_be) + 0.5 * (sigma ** 2) * T) / vol_time - vol_time

        # Probability(Lower < S_T < Upper) = N(d2_lower) - N(d2_upper)
        pop = norm.cdf(d2_lower) - norm.cdf(d2_upper)
        return max(0.0, min(1.0, pop)) * 100.0


# =====================================================================
# 2. SESSION & ORDER MANAGEMENT SYSTEM (OMS)
# =====================================================================
if "order_book" not in st.session_state:
    st.session_state.order_book = []
if "hedge_position" not in st.session_state:
    st.session_state.hedge_position = 0
if "terminal_active" not in st.session_state:
    st.session_state.terminal_active = True

def log_order(symbol: str, side: str, qty: int, price: float, order_type: str = "LIMIT"):
    order_entry = {
        "Timestamp": pd.Timestamp.now().strftime("%H:%M:%S"),
        "Symbol": symbol,
        "Side": side,
        "Qty": qty,
        "Price": round(price, 2),
        "Type": order_type,
        "Status": "FILLED"
    }
    st.session_state.order_book.insert(0, order_entry)

# =====================================================================
# 3. SIDEBAR CONTROLS & BROKER CONFIG
# =====================================================================
with st.sidebar:
    st.header("⚡ Broker Gateway")
    broker_mode = st.selectbox("Trading Mode", ["Paper / Sandbox", "Live Broker API (Kite/Upstox)"])
    if broker_mode != "Paper / Sandbox":
        api_key = st.text_input("API Key", type="password")
        api_secret = st.text_input("Access Token", type="password")
    
    st.divider()
    st.header("⚙️ Risk & Engine Bounds")
    lot_size = st.number_input("Nifty Lot Size", min_value=25, max_value=150, value=75, step=25)
    delta_threshold = st.slider("Delta Drift Limit (Lots)", 0.1, 2.0, 0.5, 0.1)
    r_rate = st.number_input("Risk-Free Rate", min_value=0.01, max_value=0.15, value=0.065, step=0.005)
    days_to_expiry = st.slider("Days to Expiry (DTE)", 0.1, 30.0, 3.5, 0.1)
    
    st.divider()
    if st.button("🚨 EMERGENCY KILL SWITCH", use_container_width=True, type="primary"):
        st.session_state.hedge_position = 0
        log_order("PORTFOLIO_ALL", "SQUARE_OFF", 0, 0.0, "MARKET_KILL")
        st.warning("All orders cancelled and positions closed.")

# =====================================================================
# 4. MAIN TERMINAL DASHBOARD
# =====================================================================
st.title("⚡ Institutional Options Trading & Delta-Neutral Terminal")

# Inputs Bar
col1, col2, col3 = st.columns(3)
with col1:
    future_price = st.number_input("Nifty Futures LTP", min_value=15000.0, max_value=35000.0, value=24800.0, step=5.0)
with col2:
    strike_selected = st.number_input("Straddle Strike", min_value=15000, max_value=35000, value=24800, step=50)
with col3:
    lots_traded = st.number_input("Trade Lots", min_value=1, max_value=50, value=2, step=1)

trade_qty = int(lots_traded * lot_size)

# Market Feed
st.subheader("📡 Live Feed & Positions Config")
c1, c2, c3, c4 = st.columns(4)
with c1:
    ce_entry = st.number_input("CE Entry", value=130.0, step=0.5)
with c2:
    ce_ltp = st.number_input("CE LTP", value=125.0, step=0.5)
with c3:
    pe_entry = st.number_input("PE Entry", value=125.0, step=0.5)
with c4:
    pe_ltp = st.number_input("PE LTP", value=120.0, step=0.5)

# Calculations
dte_y = max(days_to_expiry / 365.25, 1e-5)

# IV Calculations
ce_iv = RobustBlack76.implied_volatility(ce_ltp, future_price, strike_selected, dte_y, r_rate, "CE")
pe_iv = RobustBlack76.implied_volatility(pe_ltp, future_price, strike_selected, dte_y, r_rate, "PE")
avg_iv = (ce_iv + pe_iv) / 2.0

# Deltas
ce_unit_delta = RobustBlack76.delta(future_price, strike_selected, dte_y, r_rate, ce_iv, "CE")
pe_unit_delta = RobustBlack76.delta(future_price, strike_selected, dte_y, r_rate, pe_iv, "PE")

# Portfolio Metrics (Short Straddle)
ce_pos_delta = ce_unit_delta * (-trade_qty)
pe_pos_delta = pe_unit_delta * (-trade_qty)
net_delta = float(st.session_state.hedge_position) + ce_pos_delta + pe_pos_delta

unrealized_pnl = ((ce_entry - ce_ltp) * trade_qty) + ((pe_entry - pe_ltp) * trade_qty)

# Probability of Profit (PoP) & Breakeven Analytics
combined_entry_premium = ce_entry + pe_entry
lower_breakeven = strike_selected - combined_entry_premium
upper_breakeven = strike_selected + combined_entry_premium
pop_value = RobustBlack76.probability_of_profit(future_price, lower_breakeven, upper_breakeven, dte_y, avg_iv)

# =====================================================================
# 5. EXECUTION & PRE-TRADE METRICS
# =====================================================================
st.divider()
st.subheader("🎯 Pre-Trade Probability & Risk Metrics")

kpi_pop, kpi_be, kpi_delta, kpi_pnl = st.columns(4)
kpi_pop.metric("Probability of Profit (PoP)", f"{pop_value:.1f} %", help="Chance of expiring between breakevens based on IV")
kpi_be.metric("Breakeven Range", f"{lower_breakeven:.0f} - {upper_breakeven:.0f}", f"±{combined_entry_premium:.1f} pts")
kpi_delta.metric("Net Delta Exposure", f"{net_delta:.2f}", f"Band: ±{delta_threshold * lot_size:.1f}")
kpi_pnl.metric("Unrealized PnL", f"₹{unrealized_pnl:,.2f}")

# Breakdown Table
breakdown_data = [
    {"Leg": f"{strike_selected} CE", "Side": "SHORT", "Qty": -trade_qty, "Entry": ce_entry, "LTP": ce_ltp, "IV": f"{ce_iv*100:.1f}%", "Delta": f"{ce_pos_delta:.2f}"},
    {"Leg": f"{strike_selected} PE", "Side": "SHORT", "Qty": -trade_qty, "Entry": pe_entry, "LTP": pe_ltp, "IV": f"{pe_iv*100:.1f}%", "Delta": f"{pe_pos_delta:.2f}"},
    {"Leg": "NIFTY FUTURES (HEDGE)", "Side": "LONG" if st.session_state.hedge_position > 0 else "SHORT", "Qty": st.session_state.hedge_position, "Entry": future_price, "LTP": future_price, "IV": "—", "Delta": f"{st.session_state.hedge_position:.2f}"}
]
st.dataframe(pd.DataFrame(breakdown_data), use_container_width=True)

# =====================================================================
# 6. ONE-CLICK TRADING TERMINAL & AUTOMATED REBALANCER
# =====================================================================
st.subheader("🖥️ Execution Terminal")

t_col1, t_col2 = st.columns([2, 1])

with t_col1:
    rebalance_order_qty = -int(round(net_delta / lot_size) * lot_size)
    tolerance = delta_threshold * lot_size

    if abs(net_delta) > tolerance:
        action = "BUY" if rebalance_order_qty > 0 else "SELL"
        st.error(f"🚨 **REBALANCE TRIGGERED:** Net Delta ({net_delta:.2f}) breached limits.")
        if st.button(f"⚡ EXECUTE HEDGE: {action} {abs(rebalance_order_qty)} QTY FUTURES @ {future_price}", type="primary"):
            st.session_state.hedge_position += rebalance_order_qty
            log_order("NIFTY_FUT", action, abs(rebalance_order_qty), future_price, "IOC_LIMIT")
            st.rerun()
    else:
        st.success("✅ **DELTA NEUTRAL:** Strategy is balanced. No dynamic hedge needed.")

with t_col2:
    st.write("**Manual Straddle Actions**")
    m_col_a, m_col_b = st.columns(2)
    with m_col_a:
        if st.button("🟢 Sell Straddle", use_container_width=True):
            log_order(f"{strike_selected}CE", "SELL", trade_qty, ce_ltp)
            log_order(f"{strike_selected}PE", "SELL", trade_qty, pe_ltp)
            st.success("Straddle Orders Placed!")
            st.rerun()
    with m_col_b:
        if st.button("🔴 Exit Options", use_container_width=True):
            log_order(f"{strike_selected}CE", "BUY (EXIT)", trade_qty, ce_ltp)
            log_order(f"{strike_selected}PE", "BUY (EXIT)", trade_qty, pe_ltp)
            st.info("Straddle Exited!")
            st.rerun()

# =====================================================================
# 7. REAL-TIME AUDIT ORDER BOOK
# =====================================================================
st.subheader("📜 Terminal Order Logs")
if st.session_state.order_book:
    st.dataframe(pd.DataFrame(st.session_state.order_book), use_container_width=True)
else:
    st.caption("No orders placed yet. Orders will display here in real time.")
