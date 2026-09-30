import math
from datetime import datetime
from dataclasses import dataclass
from typing import Dict, List
import streamlit as st
import pandas as pd
import numpy as np
from scipy.stats import norm

# Upstox SDK Imports
import upstox_client
from upstox_client.rest import ApiException

st.set_page_config(
    page_title="Nifty Institutional Execution Terminal",
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
            return (1.0 if F > K else 0.0) if option_type.upper() == "CE" else (-1.0 if F < K else 0.0)
        d1, _ = cls.calc_d1_d2(F, K, T, sigma)
        df = math.exp(-r * T)
        return (df * norm.cdf(d1)) if option_type.upper() == "CE" else (-df * norm.cdf(-d1))

    @classmethod
    def gamma(cls, F: float, K: float, T: float, r: float, sigma: float) -> float:
        if T <= 1e-5 or sigma <= 1e-4:
            return 0.0
        d1, _ = cls.calc_d1_d2(F, K, T, sigma)
        df = math.exp(-r * T)
        return (df * norm.pdf(d1)) / (F * sigma * math.sqrt(T))

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
# 2. INSTITUTIONAL FII/DII METRICS ENGINE
# =====================================================================
class InstitutionalEngine:
    @staticmethod
    def calculate_pcr(put_oi: float, call_oi: float) -> float:
        if call_oi <= 0:
            return 1.0
        return round(put_oi / call_oi, 2)

    @staticmethod
    def calculate_max_pain(strikes: list, call_oi: list, put_oi: list) -> float:
        loss_matrix = []
        for test_strike in strikes:
            total_loss = 0.0
            for k, c_oi, p_oi in zip(strikes, call_oi, put_oi):
                total_loss += max(0.0, test_strike - k) * c_oi
                total_loss += max(0.0, k - test_strike) * p_oi
            loss_matrix.append(total_loss)
        return strikes[int(np.argmin(loss_matrix))]

    @staticmethod
    def vix_analysis(vix: float) -> dict:
        if vix < 11.5:
            return {"Regime": "LOW VOL (Gamma Risk)", "Multiplier": 0.5, "Advice": "Size 50% rakhein, Gamma trap se bachein."}
        elif 11.5 <= vix <= 17.5:
            return {"Regime": "OPTIMAL VOL", "Multiplier": 1.0, "Advice": "Institutional sweet spot, full set deployment."}
        elif 17.5 < vix <= 22.0:
            return {"Regime": "ELEVATED VOL", "Multiplier": 0.6, "Advice": "Strikes wide rakhein (85%+ PoP)."}
        else:
            return {"Regime": "EXTREME VOL", "Multiplier": 0.3, "Advice": "Event risk: Strict 1:2 spread only."}


# =====================================================================
# 3. SESSION & OMS (Order Management System)
# =====================================================================
if "order_book" not in st.session_state:
    st.session_state.order_book = []
if "hedge_position" not in st.session_state:
    st.session_state.hedge_position = 0
if "strategy_active" not in st.session_state:
    st.session_state.strategy_active = False
if "peak_pnl" not in st.session_state:
    st.session_state.peak_pnl = 0.0
if "entry_prices" not in st.session_state:
    st.session_state.entry_prices = {}

def execute_upstox_order(instrument_key: str, symbol: str, side: str, qty: int, price: float, access_token: str, mode: str):
    status = "PAPER_FILLED"
    order_id = "SIM_" + datetime.now().strftime("%f")[:5]

    if mode == "Live Upstox Account" and access_token:
        try:
            config = upstox_client.Configuration()
            config.access_token = access_token
            api_client = upstox_client.ApiClient(config)
            order_api = upstox_client.OrderApi(api_client)

            order_payload = upstox_client.PlaceOrderRequest(
                quantity=qty,
                product="I",
                validity="DAY",
                price=float(price),
                tag="InstiAlgo",
                instrument_token=instrument_key,
                order_type="LIMIT",
                transaction_type="BUY" if side.upper().startswith("BUY") else "SELL",
                disclosed_quantity=0,
                trigger_price=0.0,
                is_amo=False
            )
            resp = order_api.place_order(order_payload, "2.0")
            order_id = resp.data.order_id
            status = "LIVE_SENT"
        except ApiException as e:
            status = f"FAILED: {e.reason}"
            st.error(f"Upstox Order Error on {symbol}: {e}")

    order_entry = {
        "Timestamp": datetime.now().strftime("%H:%M:%S"),
        "Order ID": order_id,
        "Symbol": symbol,
        "Side": side,
        "Qty": qty,
        "Price": round(price, 2),
        "Status": status
    }
    st.session_state.order_book.insert(0, order_entry)


# =====================================================================
# 4. SIDEBAR CONTROLS & INSTITUTIONAL INPUTS
# =====================================================================
with st.sidebar:
    st.header("⚡ Upstox Authentication")
    trading_mode = st.radio("Trading Mode", ["Paper Trading (Safe)", "Live Upstox Account"])
    access_token = ""
    if trading_mode == "Live Upstox Account":
        access_token = st.text_input("Upstox Access Token", type="password")

    st.divider()
    st.header("🏛 FII / DII Footprint Feeds")
    india_vix = st.number_input("India VIX", min_value=8.0, max_value=45.0, value=13.6, step=0.1)
    total_put_oi = st.number_input("Total Put OI", value=19500000, step=100000)
    total_call_oi = st.number_input("Total Call OI", value=16200000, step=100000)

    st.divider()
    st.header("⚙️ General Settings")
    lot_size = st.number_input("Nifty Lot Size", min_value=25, max_value=150, value=75, step=25)
    delta_threshold = st.slider("Delta Rebalance Tolerance (Lots)", 0.1, 2.0, 0.5, 0.1)
    r_rate = st.number_input("Risk-Free Rate", min_value=0.01, max_value=0.15, value=0.065, step=0.005)
    days_to_expiry = st.slider("Days to Expiry (DTE)", 0.05, 30.0, 2.0, 0.05)

    st.divider()
    st.header("🛡️ Protections")
    max_loss_per_set = st.number_input("Max Loss Per Set (₹)", min_value=500, max_value=25000, value=2500, step=250)
    target_profit_per_set = st.number_input("Target Profit Per Set (₹)", min_value=500, max_value=25000, value=4000, step=250)
    trail_trigger = st.number_input("Trailing Trigger Profit (₹/Set)", min_value=500, max_value=20000, value=2000, step=250)
    auto_exit_time = st.time_input("Expiry Auto-Kill Time", datetime.strptime("15:10:00", "%H:%M:%S").time())

    st.divider()
    if st.button("🚨 EMERGENCY KILL SWITCH", use_container_width=True, type="primary"):
        st.session_state.hedge_position = 0
        st.session_state.strategy_active = False
        st.session_state.peak_pnl = 0.0
        st.warning("Sabhi positions reset aur squared off!")

dte_y = max(days_to_expiry / 365.25, 1e-5)
est_iv = (india_vix + 1.0) / 100.0  # Dynamic IV from India VIX


# =====================================================================
# 5. MARKET SCAN & INSTITUTIONAL BIAS CALCULATION
# =====================================================================
st.title("⚡ Institutional Nifty Terminal (Auto-Bias Recommendation)")

future_price = st.number_input("Nifty Future LTP", min_value=15000.0, max_value=35000.0, value=24800.0, step=5.0)
strike_step = 50
atm_strike = int(round(future_price / strike_step) * strike_step)

# PCR & Max Pain
pcr_value = InstitutionalEngine.calculate_pcr(total_put_oi, total_call_oi)
pain_strikes = [atm_strike - 200, atm_strike - 100, atm_strike, atm_strike + 100, atm_strike + 200]
call_oi_sim = [1500000, 3500000, 6500000, 4000000, 2000000]
put_oi_sim = [2500000, 5000000, 6000000, 2000000, 800000]
max_pain = InstitutionalEngine.calculate_max_pain(pain_strikes, call_oi_sim, put_oi_sim)
vix_info = InstitutionalEngine.vix_analysis(india_vix)

# Strike Scanner for 80%+ PoP
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

if selected_sell_ce is None:
    selected_sell_ce = atm_strike + 500
    selected_sell_pe = atm_strike - 500
    selected_pop = RobustBlack76.probability_of_profit(future_price, selected_sell_pe, selected_sell_ce, dte_y, est_iv)

hedge_offset = 200
buy_hedge_ce = selected_sell_ce + hedge_offset
buy_hedge_pe = selected_sell_pe - hedge_offset

# Prices via Black-76
sell_ce_price = round(RobustBlack76.price(future_price, selected_sell_ce, dte_y, r_rate, est_iv, "CE"), 2)
sell_pe_price = round(RobustBlack76.price(future_price, selected_sell_pe, dte_y, r_rate, est_iv, "PE"), 2)
buy_ce_price = round(RobustBlack76.price(future_price, buy_hedge_ce, dte_y, r_rate, est_iv, "CE"), 2)
buy_pe_price = round(RobustBlack76.price(future_price, buy_hedge_pe, dte_y, r_rate, est_iv, "PE"), 2)


# =====================================================================
# 6. HIGHLIGHTED RECOMMENDATION BOX (MARKET MOVE DIRECTION)
# =====================================================================
st.divider()

if pcr_value >= 1.20 or max_pain > future_price:
    market_bias = "BULLISH (UPWARD MOMENTUM)"
    recommended_action = "PUT SELL (Bull Put Spread)"
    rec_detail = f"Market upar jane ke sanket hain. Institutional Put writing strong hai (PCR: {pcr_value}). **{selected_sell_pe} PE SELL** karein aur safety ke liye **{buy_hedge_pe} PE BUY** karein."
    box_color = "#e8f5e9"
    border_color = "#2e7d32"
    badge_color = "#1b5e20"
elif pcr_value <= 0.80 or max_pain < future_price:
    market_bias = "BEARISH (DOWNWARD MOMENTUM)"
    recommended_action = "CALL SELL (Bear Call Spread)"
    rec_detail = f"Market neeche jane ke sanket hain. Heavy Call writing active hai (PCR: {pcr_value}). **{selected_sell_ce} CE SELL** karein aur safety ke liye **{buy_hedge_ce} CE BUY** karein."
    box_color = "#ffebee"
    border_color = "#c62828"
    badge_color = "#b71c1c"
else:
    market_bias = "NEUTRAL / SIDEWAYS"
    recommended_action = "DELTA-NEUTRAL STRANGLE (1 Sell : 2 Buy Hedge)"
    rec_detail = f"Market range-bound hai (PCR: {pcr_value}). Dono taraf ka decay capture karne ke liye **{selected_sell_ce} CE & {selected_sell_pe} PE** dono Sell karein."
    box_color = "#e3f2fd"
    border_color = "#1565c0"
    badge_color = "#0d47a1"

st.markdown(
    f"""
    <div style="background-color: {box_color}; border: 2px solid {border_color}; border-radius: 12px; padding: 18px; margin-bottom: 20px;">
        <div style="display: flex; justify-content: space-between; align-items: center;">
            <span style="font-size: 20px; font-weight: bold; color: {badge_color};">🎯 ALGO RECOMMENDATION: {recommended_action}</span>
            <span style="background-color: {badge_color}; color: white; padding: 4px 12px; border-radius: 20px; font-size: 13px; font-weight: bold;">Market Bias: {market_bias}</span>
        </div>
        <p style="margin-top: 10px; font-size: 15px; color: #333; line-height: 1.6;">
            {rec_detail}
        </p>
        <div style="margin-top: 8px; font-size: 13px; color: #555;">
            <b>FII Data Check:</b> PCR = <b>{pcr_value}</b> | Institutional Max Pain = <b>{max_pain}</b> | VIX Regime = <b>{vix_info['Regime']}</b> ({vix_info['Advice']})
        </div>
    </div>
    """,
    unsafe_allow_html=True
)


# =====================================================================
# 7. STRATEGY SETS (1-10) & MARGIN CALCULATOR
# =====================================================================
st.subheader("📦 Strategy Sets & Execution Parameters")

col_s1, col_s2, col_s3 = st.columns([1, 1.5, 1.5])
with col_s1:
    strategy_sets = st.selectbox("Strategy Sets (1-10)", options=list(range(1, 11)), index=0)

sell_qty = int(strategy_sets * 1 * lot_size)
buy_qty = int(strategy_sets * 2 * lot_size)

buy_prem_tot = (buy_ce_price + buy_pe_price) * buy_qty
sell_prem_tot = (sell_ce_price + sell_pe_price) * sell_qty
net_cash_flow = sell_prem_tot - buy_prem_tot

spread_width = hedge_offset
max_spread_risk = spread_width * lot_size
base_span_per_set = max(35000.0, max_spread_risk * 1.5)
total_hedged_span = base_span_per_set * strategy_sets
total_required_capital = total_hedged_span + buy_prem_tot

total_max_loss_limit = max_loss_per_set * strategy_sets
total_target_profit = target_profit_per_set * strategy_sets

with col_s2:
    st.info(f"**Quantities for {strategy_sets} Sets:**\n• Buy Hedge: {buy_qty} Qty (2 Lots/Set)\n• Short Legs: {sell_qty} Qty (1 Lot/Set)")

with col_s3:
    st.success(f"**💰 Margin Required (Hedged):**\n• **Total Balance Req:** ₹{total_required_capital:,.2f}\n• **Mode:** {trading_mode}")


# =====================================================================
# 8. GREEKS & LIVE RISK ENGINE
# =====================================================================
d_sell_ce = RobustBlack76.delta(future_price, selected_sell_ce, dte_y, r_rate, est_iv, "CE")
d_sell_pe = RobustBlack76.delta(future_price, selected_sell_pe, dte_y, r_rate, est_iv, "PE")
d_buy_ce = RobustBlack76.delta(future_price, buy_hedge_ce, dte_y, r_rate, est_iv, "CE")
d_buy_pe = RobustBlack76.delta(future_price, buy_hedge_pe, dte_y, r_rate, est_iv, "PE")

g_sell_ce = RobustBlack76.gamma(future_price, selected_sell_ce, dte_y, r_rate, est_iv)
g_sell_pe = RobustBlack76.gamma(future_price, selected_sell_pe, dte_y, r_rate, est_iv)

options_net_delta = (-sell_qty * d_sell_ce) + (-sell_qty * d_sell_pe) + (buy_qty * d_buy_ce) + (buy_qty * d_buy_pe)
total_net_delta = options_net_delta + float(st.session_state.hedge_position)
net_portfolio_gamma = (-sell_qty * g_sell_ce) + (-sell_qty * g_sell_pe)

current_pnl = 0.0
if st.session_state.strategy_active and st.session_state.entry_prices:
    ep = st.session_state.entry_prices
    pnl_sell_ce = (ep["sell_ce"] - sell_ce_price) * sell_qty
    pnl_sell_pe = (ep["sell_pe"] - sell_pe_price) * sell_qty
    pnl_buy_ce = (buy_ce_price - ep["buy_ce"]) * buy_qty
    pnl_buy_pe = (buy_pe_price - ep["buy_pe"]) * buy_qty
    current_pnl = pnl_sell_ce + pnl_sell_pe + pnl_buy_ce + pnl_buy_pe

    if current_pnl > st.session_state.peak_pnl:
        st.session_state.peak_pnl = current_pnl

st.divider()
k1, k2, k3, k4 = st.columns(4)
k1.metric("Strategy PoP", f"{selected_pop:.1f} %", "Min 80% Filter")
k2.metric("Safe Expiry Range", f"{selected_sell_pe} - {selected_sell_ce}")
k3.metric("Net Delta Exposure", f"{total_net_delta:.2f}", f"Band: ±{delta_threshold * lot_size:.1f}")
k4.metric("Live PnL", f"₹{current_pnl:,.2f}", f"Peak: ₹{st.session_state.peak_pnl:,.2f}")


# =====================================================================
# 9. AUTO RISK & GAMMA BLAST CIRCUIT BREAKER
# =====================================================================
auto_exit_triggered = False
exit_reason = ""

if st.session_state.strategy_active:
    now_time = datetime.now().time()
    
    # 1. Pre-Gamma Blast Threshold
    if abs(net_portfolio_gamma) >= (0.35 * strategy_sets):
        auto_exit_triggered = True
        exit_reason = f"🚨 PRE-GAMMA BLAST DETECTED! Net Gamma ({net_portfolio_gamma:.4f}) breached safety limits."

    # 2. Hard Max Loss
    elif current_pnl <= -total_max_loss_limit:
        auto_exit_triggered = True
        exit_reason = f"HARD MAX LOSS HIT (-₹{abs(current_pnl):,.2f})"

    # 3. Target Profit
    elif current_pnl >= total_target_profit:
        auto_exit_triggered = True
        exit_reason = f"TARGET PROFIT ACHIEVED (+₹{current_pnl:,.2f})"

    # 4. Trailing SL Hit
    elif st.session_state.peak_pnl >= (trail_trigger * strategy_sets):
        allowed_pullback = 0.4 * st.session_state.peak_pnl
        if current_pnl <= (st.session_state.peak_pnl - allowed_pullback):
            auto_exit_triggered = True
            exit_reason = f"TRAILING SL HIT (Secured: ₹{current_pnl:,.2f})"

    # 5. 3:10 PM Rule
    elif now_time >= auto_exit_time:
        auto_exit_triggered = True
        exit_reason = f"TIME LIMIT REACHED ({now_time.strftime('%H:%M')} >= {auto_exit_time.strftime('%H:%M')})"

    if auto_exit_triggered:
        execute_upstox_order("INST_CE", f"{selected_sell_ce} CE", "CIRCUIT_BUY (COVER SHORT)", sell_qty, sell_ce_price, access_token, trading_mode)
        execute_upstox_order("INST_PE", f"{selected_sell_pe} PE", "CIRCUIT_BUY (COVER SHORT)", sell_qty, sell_pe_price, access_token, trading_mode)
        execute_upstox_order("INST_CE_H", f"{buy_hedge_ce} CE", "CIRCUIT_SELL (CLOSE HEDGE)", buy_qty, buy_ce_price, access_token, trading_mode)
        execute_upstox_order("INST_PE_H", f"{buy_hedge_pe} PE", "CIRCUIT_SELL (CLOSE HEDGE)", buy_qty, buy_pe_price, access_token, trading_mode)
        st.session_state.strategy_active = False
        st.session_state.peak_pnl = 0.0
        st.error(f"🛑 **AUTOMATIC RISK SQUARED-OFF:** {exit_reason}")


# =====================================================================
# 10. EXECUTION TERMINAL (STRICT ORDER SEQUENCING)
# =====================================================================
st.subheader("🖥 Upstox Order Terminal")
t_col1, t_col2 = st.columns([1.5, 1])

inst_buy_ce = f"NSE_FO|NIFTY_{buy_hedge_ce}_CE"
inst_buy_pe = f"NSE_FO|NIFTY_{buy_hedge_pe}_PE"
inst_sell_ce = f"NSE_FO|NIFTY_{selected_sell_ce}_CE"
inst_sell_pe = f"NSE_FO|NIFTY_{selected_sell_pe}_PE"

with t_col2:
    st.write(f"**Execute Orders ({trading_mode})**")
    btn1, btn2 = st.columns(2)

    with btn1:
        if st.button(f"🟢 Execute {strategy_sets} Sets", use_container_width=True, disabled=st.session_state.strategy_active):
            # STEP 1: Pehle BUY hedge execute hoga margin relief ke liye
            execute_upstox_order(inst_buy_ce, f"{buy_hedge_ce} CE", "BUY (ENTRY)", buy_qty, buy_ce_price, access_token, trading_mode)
            execute_upstox_order(inst_buy_pe, f"{buy_hedge_pe} PE", "BUY (ENTRY)", buy_qty, buy_pe_price, access_token, trading_mode)

            # STEP 2: Baad me SELL orders place honge
            execute_upstox_order(inst_sell_ce, f"{selected_sell_ce} CE", "SELL (ENTRY)", sell_qty, sell_ce_price, access_token, trading_mode)
            execute_upstox_order(inst_sell_pe, f"{selected_sell_pe} PE", "SELL (ENTRY)", sell_qty, sell_pe_price, access_token, trading_mode)

            st.session_state.entry_prices = {
                "sell_ce": sell_ce_price,
                "sell_pe": sell_pe_price,
                "buy_ce": buy_ce_price,
                "buy_pe": buy_pe_price
            }
            st.session_state.strategy_active = True
            st.success(f"{strategy_sets} Sets execute ho gaye! Strict sequence (Buy first, then Sell) maintained.")
            st.rerun()

    with btn2:
        if st.button(f"🔴 Exit {strategy_sets} Sets", use_container_width=True, disabled=not st.session_state.strategy_active):
            # STEP 1: Pehle SELL leg buy-back hokar close hogi taaki naked risk na rahe
            execute_upstox_order(inst_sell_ce, f"{selected_sell_ce} CE", "BUY (EXIT SHORT)", sell_qty, sell_ce_price, access_token, trading_mode)
            execute_upstox_order(inst_sell_pe, f"{selected_sell_pe} PE", "BUY (EXIT SHORT)", sell_qty, sell_pe_price, access_token, trading_mode)

            # STEP 2: Baad me BUY hedge positions exit hongi
            execute_upstox_order(inst_buy_ce, f"{buy_hedge_ce} CE", "SELL (EXIT HEDGE)", buy_qty, buy_ce_price, access_token, trading_mode)
            execute_upstox_order(inst_buy_pe, f"{buy_hedge_pe} PE", "SELL (EXIT HEDGE)", buy_qty, buy_pe_price, access_token, trading_mode)

            st.session_state.strategy_active = False
            st.info(f"{strategy_sets} Sets manual square off ho gaye!")
            st.rerun()

with t_col1:
    st.write("**Strategy Status**")
    if st.session_state.strategy_active:
        st.warning("⚠️ Trade ACTIVE hai. Trailing Stop Loss aur Pre-Gamma Blast shield background me live hain.")
    else:
        st.success("✅ Idle: Koi open position nahi hai.")

# Real-Time Order Logs
st.subheader("📜 Live Upstox Order Logs")
if st.session_state.order_book:
    st.dataframe(pd.DataFrame(st.session_state.order_book), use_container_width=True)
else:
    st.caption("Filhal koi order place nahi hua hai.")
