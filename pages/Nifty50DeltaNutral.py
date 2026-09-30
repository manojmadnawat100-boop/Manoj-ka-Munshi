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
# 2. SESSION & ORDER MANAGEMENT SYSTEM (OMS)
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
# 3. SIDEBAR CONTROLS
# =====================================================================
with st.sidebar:
    st.header("⚙️ Strategy & Risk Settings")
    lot_size = st.number_input("Nifty Lot Size", min_value=25, max_value=150, value=75, step=25)
    
    # 1 se 10 lots chune ka vikalp
    lot_multiplier = st.slider("Select Lots Multiplier (1 to 10)", min_value=1, max_value=10, value=1, step=1)
    
    delta_threshold = st.slider("Delta Rebalance Tolerance (Lots)", 0.1, 2.0, 0.5, 0.1)
    r_rate = st.number_input("Risk-Free Rate", min_value=0.01, max_value=0.15, value=0.065, step=0.005)
    days_to_expiry = st.slider("Days to Expiry (DTE)", 0.1, 30.0, 3.5, 0.1)
    est_iv = st.slider("Implied Volatility (IV %)", 5.0, 40.0, 15.0, 0.5) / 100.0
    
    st.divider()
    if st.button("🚨 EMERGENCY KILL SWITCH", use_container_width=True, type="primary"):
        st.session_state.hedge_position = 0
        log_order("ALL_POSITIONS", "SQUARE_OFF", 0, 0.0, "MARKET_KILL")
        st.warning("Sabhi positions square off ho gayi hain!")

# Quantities base calculations: 1 Lot Sell, 2 Lot Buy per multiplier
sell_qty = int(lot_multiplier * 1 * lot_size)
buy_qty = int(lot_multiplier * 2 * lot_size)

# =====================================================================
# 4. AUTO STRIKE SCANNER (PoP >= 80%)
# =====================================================================
st.title("⚡ Institutional Options Terminal (PoP ≥ 80% Filter)")

col_spot, col_step = st.columns(2)
with col_spot:
    future_price = st.number_input("Nifty Future LTP", min_value=15000.0, max_value=35000.0, value=24800.0, step=5.0)
with col_step:
    strike_step = 50

# Nearest ATM Strike
atm_strike = int(round(future_price / strike_step) * strike_step)
dte_y = max(days_to_expiry / 365.25, 1e-5)

# Scanner loop for finding strikes with PoP >= 80%
scanner_results = []
for distance in range(100, 1200, strike_step):
    call_strike = atm_strike + distance
    put_strike = atm_strike - distance
    
    # Range is put_strike to call_strike
    pop = RobustBlack76.probability_of_profit(future_price, put_strike, call_strike, dte_y, est_iv)
    if pop >= 80.0:
        c_p = RobustBlack76.price(future_price, call_strike, dte_y, r_rate, est_iv, "CE")
        p_p = RobustBlack76.price(future_price, put_strike, dte_y, r_rate, est_iv, "PE")
        scanner_results.append({
            "Distance": distance,
            "CE Strike": call_strike,
            "PE Strike": put_strike,
            "PoP (%)": round(pop, 2),
            "Estimated CE Price": round(c_p, 2),
            "Estimated PE Price": round(p_p, 2)
        })

df_scanner = pd.DataFrame(scanner_results)

st.subheader("🎯 Auto-Selected Safe Range Strikes (PoP ≥ 80%)")
if not df_scanner.empty:
    st.dataframe(df_scanner.head(8), use_container_width=True)
    best_dist = int(df_scanner.iloc[0]["Distance"])
else:
    best_dist = 400
    st.warning("80% PoP ke liye market range badi choose karni hogi ya DTE kam karna hoga.")

# =====================================================================
# 5. STRATEGY LEGS SETUP (1 Lot Sell ATM : 2 Lot Buy OTM Hedge)
# =====================================================================
st.subheader("📊 1 Sell : 2 Buy Hedged Straddle Config")

col_atm, col_otm = st.columns(2)
with col_atm:
    st.markdown(f"**ATM Sell Leg (1 Lot = {sell_qty} Qty)**")
    atm_ce_price = st.number_input("ATM CE Sell Price", value=125.0, step=0.5)
    atm_pe_price = st.number_input("ATM PE Sell Price", value=120.0, step=0.5)

with col_otm:
    st.markdown(f"**OTM Buy Hedge Leg (2 Lots = {buy_qty} Qty)**")
    hedge_dist = st.number_input("Hedge Strike Distance (from ATM)", value=best_dist, step=strike_step)
    hedge_ce_strike = atm_strike + hedge_dist
    hedge_pe_strike = atm_strike - hedge_dist
    hedge_ce_price = st.number_input(f"Buy Hedge {hedge_ce_strike} CE Price", value=22.0, step=0.5)
    hedge_pe_price = st.number_input(f"Buy Hedge {hedge_pe_strike} PE Price", value=18.0, step=0.5)

# Greeks calculations
d_atm_ce = RobustBlack76.delta(future_price, atm_strike, dte_y, r_rate, est_iv, "CE")
d_atm_pe = RobustBlack76.delta(future_price, atm_strike, dte_y, r_rate, est_iv, "PE")
d_hdg_ce = RobustBlack76.delta(future_price, hedge_ce_strike, dte_y, r_rate, est_iv, "CE")
d_hdg_pe = RobustBlack76.delta(future_price, hedge_pe_strike, dte_y, r_rate, est_iv, "PE")

# Net Delta: Sell legs are negative qty, Buy hedges are positive qty
net_options_delta = (
    (-sell_qty * d_atm_ce) +
    (-sell_qty * d_atm_pe) +
    (buy_qty * d_hdg_ce) +
    (buy_qty * d_hdg_pe)
)
total_delta = net_options_delta + float(st.session_state.hedge_position)

# Net Margin/Cost benefit calculation
net_credit_debit = (sell_qty * (atm_ce_price + atm_pe_price)) - (buy_qty * (hedge_ce_price + hedge_pe_price))

# Breakevens and Strategy PoP
lower_be = atm_strike - (atm_ce_price + atm_pe_price)
upper_be = atm_strike + (atm_ce_price + atm_pe_price)
strategy_pop = RobustBlack76.probability_of_profit(future_price, lower_be, upper_be, dte_y, est_iv)

# =====================================================================
# 6. KPI DASHBOARD & EXECUTION BUTTONS
# =====================================================================
st.divider()
k1, k2, k3, k4 = st.columns(4)
k1.metric("Selected Strategy PoP", f"{strategy_pop:.1f} %", "Min Target: 80%")
k2.metric("Breakeven Range", f"{lower_be:.0f} - {upper_be:.0f}")
k3.metric("Net Delta Exposure", f"{total_delta:.2f}", f"Tolerance: ±{delta_threshold * lot_size:.1f}")
k4.metric("Net Inflow / Outflow", f"₹{net_credit_debit:,.2f}", "Margin Protected")

# Positions Breakdown Table
legs_breakdown = [
    {"Leg": f"{atm_strike} CE (ATM)", "Type": "SELL", "Qty": sell_qty, "Price": atm_ce_price, "Delta": round(-sell_qty * d_atm_ce, 2)},
    {"Leg": f"{atm_strike} PE (ATM)", "Type": "SELL", "Qty": sell_qty, "Price": atm_pe_price, "Delta": round(-sell_qty * d_atm_pe, 2)},
    {"Leg": f"{hedge_ce_strike} CE (Hedge)", "Type": "BUY", "Qty": buy_qty, "Price": hedge_ce_price, "Delta": round(buy_qty * d_hdg_ce, 2)},
    {"Leg": f"{hedge_pe_strike} PE (Hedge)", "Type": "BUY", "Qty": buy_qty, "Price": hedge_pe_price, "Delta": round(buy_qty * d_hdg_pe, 2)},
    {"Leg": "Futures Delta Hedge", "Type": "FUT", "Qty": st.session_state.hedge_position, "Price": future_price, "Delta": round(st.session_state.hedge_position, 2)}
]
st.dataframe(pd.DataFrame(legs_breakdown), use_container_width=True)

# Order Placement Section
st.subheader("🖥️ Execution Terminal")
t_col1, t_col2 = st.columns([2, 1])

with t_col1:
    rebal_qty = -int(round(total_delta / lot_size) * lot_size)
    if abs(total_delta) > (delta_threshold * lot_size):
        action = "BUY" if rebal_qty > 0 else "SELL"
        st.error(f"🚨 **DELTA DRIFT BREACHED:** Total Delta ({total_delta:.2f})")
        if st.button(f"⚡ EXECUTE HEDGE: {action} {abs(rebal_qty)} QTY FUTURES @ {future_price}", type="primary"):
            st.session_state.hedge_position += rebal_qty
            log_order("NIFTY_FUT", action, abs(rebal_qty), future_price, "IOC_LIMIT")
            st.rerun()
    else:
        st.success("✅ **DELTA NEUTRAL:** Portfolio safely balanced.")

with t_col2:
    st.write("**One-Click Order Actions**")
    btn1, btn2 = st.columns(2)
    with btn1:
        if st.button("🟢 Execute Strategy", use_container_width=True):
            # 1 Lot Sell
            log_order(f"{atm_strike}CE", "SELL", sell_qty, atm_ce_price)
            log_order(f"{atm_strike}PE", "SELL", sell_qty, atm_pe_price)
            # 2 Lot Buy Hedge (Kam rupye lagne ke liye pehle buy trigger hoga)
            log_order(f"{hedge_ce_strike}CE", "BUY", buy_qty, hedge_ce_price)
            log_order(f"{hedge_pe_strike}PE", "BUY", buy_qty, hedge_pe_price)
            st.success("Hedged Strategy Orders Placed!")
            st.rerun()
    with btn2:
        if st.button("🔴 Exit Strategy", use_container_width=True):
            log_order(f"{atm_strike}CE", "BUY (EXIT)", sell_qty, atm_ce_price)
            log_order(f"{atm_strike}PE", "BUY (EXIT)", sell_qty, atm_pe_price)
            log_order(f"{hedge_ce_strike}CE", "SELL (EXIT)", buy_qty, hedge_ce_price)
            log_order(f"{hedge_pe_strike}PE", "SELL (EXIT)", buy_qty, hedge_pe_price)
            st.info("Strategy Fully Exited!")
            st.rerun()

# Real-time Order Log
st.subheader("📜 Order Logs")
if st.session_state.order_book:
    st.dataframe(pd.DataFrame(st.session_state.order_book), use_container_width=True)
else:
    st.caption("Abhi koi active order execute nahi hua hai.")
    
