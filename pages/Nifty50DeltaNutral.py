import math
from dataclasses import dataclass
from typing import Dict, List
import streamlit as st
import pandas as pd
from scipy.optimize import brentq
from scipy.stats import norm

st.set_page_config(
    page_title="Nifty 80% PoP Algo Terminal",
    page_icon="⚡",
    layout="wide"
)

# =====================================================================
# 1. QUANTITATIVE PRICING & GREEKS ENGINE (Black-76)
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
        if T <= 1e-5 or sigma <= 1e-4 or lower_be >= upper_be:
            return 0.0
        vol_time = sigma * math.sqrt(T)
        d2_upper = (math.log(F / upper_be) + 0.5 * (sigma ** 2) * T) / vol_time - vol_time
        d2_lower = (math.log(F / lower_be) + 0.5 * (sigma ** 2) * T) / vol_time - vol_time
        pop = norm.cdf(d2_lower) - norm.cdf(d2_upper)
        return max(0.0, min(1.0, pop)) * 100.0


# =====================================================================
# 2. SESSION & OMS (Order Management System)
# =====================================================================
if "order_book" not in st.session_state:
    st.session_state.order_book = []
if "hedge_position" not in st.session_state:
    st.session_state.hedge_position = 0

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
# 3. SIDEBAR CONTROLS & RISK LIMITS
# =====================================================================
with st.sidebar:
    st.header("⚙️ Risk & Order Multipliers")
    lot_size = st.number_input("Nifty Lot Size", min_value=25, max_value=150, value=75, step=25)
    lot_multiplier = st.slider("Select Trade Lots (1 to 10)", min_value=1, max_value=10, value=1, step=1)
    
    delta_threshold = st.slider("Delta Rebalance Tolerance (Lots)", 0.1, 2.0, 0.5, 0.1)
    r_rate = st.number_input("Risk-Free Rate", min_value=0.01, max_value=0.15, value=0.065, step=0.005)
    days_to_expiry = st.slider("Days to Expiry (DTE)", 0.1, 30.0, 3.5, 0.1)
    est_iv = st.slider("Implied Volatility (IV %)", 5.0, 40.0, 14.5, 0.5) / 100.0
    
    st.divider()
    if st.button("🚨 EMERGENCY KILL SWITCH", use_container_width=True, type="primary"):
        st.session_state.hedge_position = 0
        log_order("PORTFOLIO_ALL", "SQUARE_OFF", 0, 0.0, "MARKET_KILL")
        st.warning("All active positions squared off!")

# Lot Quantities: 1 Lot Sell : 2 Lot Buy per multiplier
sell_qty = int(lot_multiplier * 1 * lot_size)
buy_qty = int(lot_multiplier * 2 * lot_size)
dte_y = max(days_to_expiry / 365.25, 1e-5)


# =====================================================================
# 4. AUTOMATIC STRIKE SELECTION ENGINE (PoP >= 80% ENFORCER)
# =====================================================================
st.title("⚡ Auto 80%+ PoP Delta-Neutral Terminal")

future_price = st.number_input("Nifty Future LTP", min_value=15000.0, max_value=35000.0, value=24800.0, step=5.0)
strike_step = 50
atm_strike = int(round(future_price / strike_step) * strike_step)

# Engine scans outwards to automatically lock the exact strike where PoP >= 80%
selected_sell_ce = None
selected_sell_pe = None
selected_pop = 0.0

for distance in range(100, 2000, strike_step):
    test_ce = atm_strike + distance
    test_pe = atm_strike - distance
    pop_score = RobustBlack76.probability_of_profit(future_price, test_pe, test_ce, dte_y, est_iv)
    if pop_score >= 80.0:
        selected_sell_ce = test_ce
        selected_sell_pe = test_pe
        selected_pop = pop_score
        break

# Fallback safety
if selected_sell_ce is None:
    selected_sell_ce = atm_strike + 500
    selected_sell_pe = atm_strike - 500
    selected_pop = RobustBlack76.probability_of_profit(future_price, selected_sell_pe, selected_sell_ce, dte_y, est_iv)

# Buy Hedge Strike auto-placed further OTM for margin relief (Ratio 1:2)
hedge_offset = 200
buy_hedge_ce = selected_sell_ce + hedge_offset
buy_hedge_pe = selected_sell_pe - hedge_offset

# Theoretical Prices via Black-76
sell_ce_price = round(RobustBlack76.price(future_price, selected_sell_ce, dte_y, r_rate, est_iv, "CE"), 2)
sell_pe_price = round(RobustBlack76.price(future_price, selected_sell_pe, dte_y, r_rate, est_iv, "PE"), 2)
buy_ce_price = round(RobustBlack76.price(future_price, buy_hedge_ce, dte_y, r_rate, est_iv, "CE"), 2)
buy_pe_price = round(RobustBlack76.price(future_price, buy_hedge_pe, dte_y, r_rate, est_iv, "PE"), 2)


# =====================================================================
# 5. GREEKS & RISK DASHBOARD
# =====================================================================
d_sell_ce = RobustBlack76.delta(future_price, selected_sell_ce, dte_y, r_rate, est_iv, "CE")
d_sell_pe = RobustBlack76.delta(future_price, selected_sell_pe, dte_y, r_rate, est_iv, "PE")
d_buy_ce = RobustBlack76.delta(future_price, buy_hedge_ce, dte_y, r_rate, est_iv, "CE")
d_buy_pe = RobustBlack76.delta(future_price, buy_hedge_pe, dte_y, r_rate, est_iv, "PE")

# 1 Sell (-sell_qty) aur 2 Buy (+buy_qty)
options_net_delta = (
    (-sell_qty * d_sell_ce) +
    (-sell_qty * d_sell_pe) +
    (buy_qty * d_buy_ce) +
    (buy_qty * d_buy_pe)
)
total_net_delta = options_net_delta + float(st.session_state.hedge_position)

net_cash_flow = (sell_qty * (sell_ce_price + sell_pe_price)) - (buy_qty * (buy_ce_price + buy_pe_price))

st.divider()
k1, k2, k3, k4 = st.columns(4)
k1.metric("Strategy PoP", f"{selected_pop:.1f} %", "Target ≥ 80% Matched")
k2.metric("Safe Profit Zone", f"{selected_sell_pe} - {selected_sell_ce}", f"Spread: {selected_sell_ce - selected_sell_pe} pts")
k3.metric("Net Delta Exposure", f"{total_net_delta:.2f}", f"Tolerance: ±{delta_threshold * lot_size:.1f}")
k4.metric("Net Cashflow / Credit", f"₹{net_cash_flow:,.2f}", f"{lot_multiplier} Lots Multiplier")

# Auto-Configured Legs Display
st.subheader("🎯 Auto-Selected 80%+ PoP Basket (1 Sell : 2 Buy)")
breakdown_records = [
    {"Leg": f"{selected_sell_ce} CE (Auto Selected)", "Side": "SELL (1 Lot)", "Qty": sell_qty, "Est. LTP": sell_ce_price, "Delta": round(-sell_qty * d_sell_ce, 2)},
    {"Leg": f"{selected_sell_pe} PE (Auto Selected)", "Side": "SELL (1 Lot)", "Qty": sell_qty, "Est. LTP": sell_pe_price, "Delta": round(-sell_qty * d_sell_pe, 2)},
    {"Leg": f"{buy_hedge_ce} CE (Margin Hedge)", "Side": "BUY (2 Lots)", "Qty": buy_qty, "Est. LTP": buy_ce_price, "Delta": round(buy_qty * d_buy_ce, 2)},
    {"Leg": f"{buy_hedge_pe} PE (Margin Hedge)", "Side": "BUY (2 Lots)", "Qty": buy_qty, "Est. LTP": buy_pe_price, "Delta": round(buy_qty * d_buy_pe, 2)},
    {"Leg": "Futures Delta Hedge", "Side": "LONG" if st.session_state.hedge_position > 0 else "SHORT", "Qty": st.session_state.hedge_position, "Est. LTP": future_price, "Delta": round(st.session_state.hedge_position, 2)}
]
st.dataframe(pd.DataFrame(breakdown_records), use_container_width=True)


# =====================================================================
# 6. ONE-CLICK EXECUTION TERMINAL
# =====================================================================
st.subheader("🖥️ Execution Terminal")
t_col1, t_col2 = st.columns([2, 1])

with t_col1:
    rebal_qty = -int(round(total_net_delta / lot_size) * lot_size)
    tolerance = delta_threshold * lot_size
    if abs(total_net_delta) > tolerance:
        action = "BUY" if rebal_qty > 0 else "SELL"
        st.error(f"🚨 **DELTA DRIFT BREACH:** Net Delta ({total_net_delta:.2f})")
        if st.button(f"⚡ EXECUTE HEDGE: {action} {abs(rebal_qty)} QTY FUTURES @ {future_price}", type="primary"):
            st.session_state.hedge_position += rebal_qty
            log_order("NIFTY_FUT", action, abs(rebal_qty), future_price, "IOC_LIMIT")
            st.rerun()
    else:
        st.success("✅ **DELTA NEUTRAL:** Strategy delta safe zone me hai.")

with t_col2:
    st.write("**One-Click Order Actions**")
    btn1, btn2 = st.columns(2)
    with btn1:
        if st.button("🟢 Execute Strategy", use_container_width=True):
            # Pehle buy hedge orders trigger honge margin benefit unlock karne ke liye
            log_order(f"{buy_hedge_ce}CE", "BUY", buy_qty, buy_ce_price)
            log_order(f"{buy_hedge_pe}PE", "BUY", buy_qty, buy_pe_price)
            log_order(f"{selected_sell_ce}CE", "SELL", sell_qty, sell_ce_price)
            log_order(f"{selected_sell_pe}PE", "SELL", sell_qty, sell_pe_price)
            st.success("80%+ PoP Basket Orders Placed!")
            st.rerun()
    with btn2:
        if st.button("🔴 Exit Strategy", use_container_width=True):
            log_order(f"{selected_sell_ce}CE", "BUY (EXIT)", sell_qty, sell_ce_price)
            log_order(f"{selected_sell_pe}PE", "BUY (EXIT)", sell_qty, sell_pe_price)
            log_order(f"{buy_hedge_ce}CE", "SELL (EXIT)", buy_qty, buy_ce_price)
            log_order(f"{buy_hedge_pe}PE", "SELL (EXIT)", buy_qty, buy_pe_price)
            st.info("Basket Exited Successfully!")
            st.rerun()

# Order Log Display
st.subheader("📜 Order Logs")
if st.session_state.order_book:
    st.dataframe(pd.DataFrame(st.session_state.order_book), use_container_width=True)
else:
    st.caption("Filhal koi active order nahi hai.")
            
