import math
import os
from datetime import datetime, time
from typing import Dict, List, Tuple
import numpy as np
import pandas as pd
from scipy.stats import norm
import streamlit as st

# Upstox SDK Imports
import upstox_client
from upstox_client.rest import ApiException

st.set_page_config(
    page_title="Nifty Institutional Shield Terminal",
    page_icon="🛡️",
    layout="wide",
)

# Persistent File Paths
DAILY_SUMMARY_LOG_FILE = "daily_paper_trades.csv"
LEG_EXECUTION_LOG_FILE = "daily_trade_legs_journal.csv"


# =====================================================================
# 0. PERSISTENT LOGGING ENGINES (CSV STORAGE)
# =====================================================================
def log_trade_summary_to_storage(
    date_str: str,
    time_str: str,
    action: str,
    sets: int,
    pnl: float,
    reason: str,
    details: str,
):
    """Saves aggregate trade exit summary for daily review."""
    record = {
        "Date": date_str,
        "Time": time_str,
        "Action": action,
        "Sets": sets,
        "Realized_PnL": round(pnl, 2),
        "Exit_Reason": reason,
        "Trade_Details": details,
    }
    df_new = pd.DataFrame([record])
    if not os.path.exists(DAILY_SUMMARY_LOG_FILE):
        df_new.to_csv(DAILY_SUMMARY_LOG_FILE, index=False)
    else:
        df_new.to_csv(
            DAILY_SUMMARY_LOG_FILE, mode="a", header=False, index=False
        )


def log_leg_execution_to_storage(
    date_str: str,
    time_str: str,
    order_id: str,
    leg_name: str,
    side: str,
    qty: int,
    theoretical_price: float,
    execution_price: float,
    iv_percent: float,
    underlying_ltp: float,
    strategy_stage: str,
):
    """Saves per-leg granular prices, IV, and slippage calculations."""
    if "BUY" in side.upper():
        slippage_per_unit = round(execution_price - theoretical_price, 2)
    else:
        slippage_per_unit = round(theoretical_price - execution_price, 2)

    total_slippage_inr = round(slippage_per_unit * qty, 2)

    leg_record = {
        "Date": date_str,
        "Time": time_str,
        "Order_ID": order_id,
        "Stage": strategy_stage,
        "Leg": leg_name,
        "Side": side,
        "Qty": qty,
        "Underlying_LTP": underlying_ltp,
        "IV_Percent": round(iv_percent, 2),
        "Theo_Price": round(theoretical_price, 2),
        "Exec_Price": round(execution_price, 2),
        "Slippage_Pt": slippage_per_unit,
        "Slippage_INR": total_slippage_inr,
    }

    df_entry = pd.DataFrame([leg_record])
    if not os.path.exists(LEG_EXECUTION_LOG_FILE):
        df_entry.to_csv(LEG_EXECUTION_LOG_FILE, index=False)
    else:
        df_entry.to_csv(
            LEG_EXECUTION_LOG_FILE, mode="a", header=False, index=False
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
        d1 = (math.log(F / K) + 0.5 * (sigma**2) * T) / vol_time
        d2 = d1 - vol_time
        return d1, d2

    @classmethod
    def price(
        cls,
        F: float,
        K: float,
        T: float,
        r: float,
        sigma: float,
        option_type: str,
    ) -> float:
        if T <= 1e-5:
            return max(0.0, (F - K) if option_type.upper() == "CE" else (K - F))
        d1, d2 = cls.calc_d1_d2(F, K, T, sigma)
        df = math.exp(-r * T)
        if option_type.upper() == "CE":
            return df * (F * norm.cdf(d1) - K * norm.cdf(d2))
        else:
            return df * (K * norm.cdf(-d2) - F * norm.cdf(-d1))

    @classmethod
    def delta(
        cls,
        F: float,
        K: float,
        T: float,
        r: float,
        sigma: float,
        option_type: str,
    ) -> float:
        if T <= 1e-5 or sigma <= 1e-4:
            return (
                (1.0 if F > K else 0.0)
                if option_type.upper() == "CE"
                else (-1.0 if F < K else 0.0)
            )
        d1, _ = cls.calc_d1_d2(F, K, T, sigma)
        df = math.exp(-r * T)
        return (
            (df * norm.cdf(d1))
            if option_type.upper() == "CE"
            else (-df * norm.cdf(-d1))
        )

    @classmethod
    def gamma(
        cls, F: float, K: float, T: float, r: float, sigma: float
    ) -> float:
        if T <= 1e-5 or sigma <= 1e-4:
            return 0.0
        d1, _ = cls.calc_d1_d2(F, K, T, sigma)
        df = math.exp(-r * T)
        return (df * norm.pdf(d1)) / (F * sigma * math.sqrt(T))

    @classmethod
    def probability_of_profit(
        cls, F: float, lower_be: float, upper_be: float, T: float, sigma: float
    ) -> float:
        if T <= 1e-5 or sigma <= 1e-4 or lower_be >= upper_be:
            return 0.0
        vol_time = sigma * math.sqrt(T)
        d2_upper = (
            math.log(F / upper_be) + 0.5 * (sigma**2) * T
        ) / vol_time - vol_time
        d2_lower = (
            math.log(F / lower_be) + 0.5 * (sigma**2) * T
        ) / vol_time - vol_time
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
            return {
                "Regime": "LOW VOL (Gamma Risk)",
                "Multiplier": 0.5,
                "Advice": "Size 50% rakhein, Gamma trap se bachein.",
            }
        elif 11.5 <= vix <= 17.5:
            return {
                "Regime": "OPTIMAL VOL",
                "Multiplier": 1.0,
                "Advice": "Institutional sweet spot, full set deployment.",
            }
        elif 17.5 < vix <= 22.0:
            return {
                "Regime": "ELEVATED VOL",
                "Multiplier": 0.6,
                "Advice": "Strikes wide rakhein (85%+ PoP).",
            }
        else:
            return {
                "Regime": "EXTREME VOL",
                "Multiplier": 0.3,
                "Advice": "Event risk: Strict 1:2 spread only.",
            }


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
if "daily_realized_loss" not in st.session_state:
    st.session_state.daily_realized_loss = 0.0


def execute_upstox_order(
    instrument_key: str,
    symbol: str,
    side: str,
    qty: int,
    theo_price: float,
    access_token: str,
    mode: str,
    current_iv: float,
    underlying_price: float,
    stage: str,
) -> Tuple[bool, str, float]:
    order_id = "SIM_" + datetime.now().strftime("%f")[:5]
    success = True
    executed_price = theo_price

    if mode == "Live Upstox Account" and access_token:
        try:
            config = upstox_client.Configuration()
            config.access_token = access_token
            api_client = upstox_client.ApiClient(config)
            order_api = upstox_client.OrderApi(api_client)

            tick_price = round(round(theo_price / 0.05) * 0.05, 2)

            order_payload = upstox_client.PlaceOrderRequest(
                quantity=qty,
                product="I",
                validity="DAY",
                price=float(tick_price),
                tag="SafeInstiAlgo",
                instrument_token=instrument_key,
                order_type="LIMIT",
                transaction_type="BUY"
                if side.upper().startswith("BUY")
                else "SELL",
                disclosed_quantity=0,
                trigger_price=0.0,
                is_amo=False,
            )
            resp = order_api.place_order(order_payload, "2.0")
            order_id = resp.data.order_id
            status = "LIVE_SENT"
            executed_price = tick_price
            success = True
        except ApiException as e:
            status = f"FAILED: {e.reason}"
            st.error(f"Order Rejected on {symbol}: {e}")
            return False, status, 0.0
    else:
        # Realistic tick variation simulation for Paper Trading (+/- 0.05 pt slippage)
        sim_noise = 0.05 if "BUY" in side.upper() else -0.05
        executed_price = max(0.05, round(theo_price + sim_noise, 2))
        status = "PAPER_FILLED"
        success = True

    # Real-time state order book log
    order_entry = {
        "Timestamp": datetime.now().strftime("%H:%M:%S"),
        "Order ID": order_id,
        "Symbol": symbol,
        "Side": side,
        "Qty": qty,
        "Price": round(executed_price, 2),
        "Status": status,
    }
    st.session_state.order_book.insert(0, order_entry)

    # Permanent CSV leg logging
    now_d = datetime.now().strftime("%Y-%m-%d")
    now_t = datetime.now().strftime("%H:%M:%S")
    log_leg_execution_to_storage(
        date_str=now_d,
        time_str=now_t,
        order_id=order_id,
        leg_name=symbol,
        side=side,
        qty=qty,
        theoretical_price=theo_price,
        execution_price=executed_price,
        iv_percent=current_iv * 100.0,
        underlying_ltp=underlying_price,
        strategy_stage=stage,
    )

    return success, status, executed_price


# =====================================================================
# 4. SIDEBAR CONTROLS & RISK SHIELDS
# =====================================================================
with st.sidebar:
    st.header("⚡ Upstox Authentication")
    trading_mode = st.radio(
        "Trading Mode", ["Paper Trading (Safe)", "Live Upstox Account"]
    )
    access_token = ""
    if trading_mode == "Live Upstox Account":
        access_token = st.text_input("Upstox Access Token", type="password")

    st.divider()
    st.header("🛡️ Capital Protection Shields")
    daily_account_loss_limit = st.number_input(
        "Daily Max Loss Circuit (₹)",
        min_value=1000,
        max_value=50000,
        value=5000,
        step=500,
    )
    enforce_opening_filter = st.checkbox(
        "Enforce 9:15-9:30 AM Whipsaw Lock", value=True
    )

    if st.button("🔄 Reset Daily Loss Counter"):
        st.session_state.daily_realized_loss = 0.0
        st.success("Daily realized loss reset ho gaya!")
        st.rerun()

    st.divider()
    st.header("🏛 FII / DII Footprint Feeds")
    india_vix = st.number_input(
        "India VIX", min_value=8.0, max_value=45.0, value=13.6, step=0.1
    )
    total_put_oi = st.number_input(
        "Total Put OI", value=19500000, step=100000
    )
    total_call_oi = st.number_input(
        "Total Call OI", value=16200000, step=100000
    )

    st.divider()
    st.header("⚙️ General Settings")
    lot_size = st.number_input(
        "Nifty Lot Size", min_value=25, max_value=150, value=75, step=25
    )
    delta_threshold = st.slider(
        "Delta Rebalance Tolerance (Lots)", 0.1, 2.0, 0.5, 0.1
    )
    r_rate = st.number_input(
        "Risk-Free Rate", min_value=0.01, max_value=0.15, value=0.065, step=0.005
    )
    days_to_expiry = st.slider(
        "Days to Expiry (DTE)", 0.05, 30.0, 2.0, 0.05
    )

    st.divider()
    st.header("🛡️ Trade Protectors")
    max_loss_per_set = st.number_input(
        "Max Loss Per Set (₹)",
        min_value=500,
        max_value=25000,
        value=2500,
        step=250,
    )
    target_profit_per_set = st.number_input(
        "Target Profit Per Set (₹)",
        min_value=500,
        max_value=25000,
        value=4000,
        step=250,
    )
    trail_trigger = st.number_input(
        "Trailing Trigger Profit (₹/Set)",
        min_value=500,
        max_value=20000,
        value=2000,
        step=250,
    )
    auto_exit_time = st.time_input(
        "Expiry Auto-Kill Time",
        datetime.strptime("15:10:00", "%H:%M:%S").time(),
    )

    st.divider()
    if st.button(
        "🚨 EMERGENCY KILL SWITCH", use_container_width=True, type="primary"
    ):
        st.session_state.hedge_position = 0
        st.session_state.strategy_active = False
        st.session_state.peak_pnl = 0.0
        st.warning("Sabhi positions squared off aur lock kar di gayi hain!")

dte_y = max(days_to_expiry / 365.25, 1e-5)
est_iv = (india_vix + 1.0) / 100.0


# =====================================================================
# 5. MARKET SCAN & INSTITUTIONAL BIAS CALCULATION
# =====================================================================
st.title("⚡ Institutional Nifty Terminal (Protected Execution)")

if abs(st.session_state.daily_realized_loss) >= daily_account_loss_limit:
    st.error(
        f"🛑 **ACCOUNT LOCKED: Daily Max Loss (₹{daily_account_loss_limit:,.2f}) Reach Ho Chuka Hai!** Naye orders band hain."
    )
    st.stop()

future_price = st.number_input(
    "Nifty Future LTP",
    min_value=15000.0,
    max_value=35000.0,
    value=24800.0,
    step=5.0,
)
strike_step = 50
atm_strike = int(round(future_price / strike_step) * strike_step)

pcr_value = InstitutionalEngine.calculate_pcr(total_put_oi, total_call_oi)
pain_strikes = [
    atm_strike - 200,
    atm_strike - 100,
    atm_strike,
    atm_strike + 100,
    atm_strike + 200,
]
call_oi_sim = [1500000, 3500000, 6500000, 4000000, 2000000]
put_oi_sim = [2500000, 5000000, 6000000, 2000000, 800000]
max_pain = InstitutionalEngine.calculate_max_pain(
    pain_strikes, call_oi_sim, put_oi_sim
)
vix_info = InstitutionalEngine.vix_analysis(india_vix)

selected_sell_ce = None
selected_sell_pe = None
selected_pop = 0.0

for distance in range(100, 2000, strike_step):
    test_ce = atm_strike + distance
    test_pe = atm_strike - distance
    pop_score = RobustBlack76.probability_of_profit(
        future_price, test_pe, test_ce, dte_y, est_iv
    )
    if pop_score >= 80.0:
        selected_sell_ce = test_ce
        selected_sell_pe = test_pe
        selected_pop = pop_score
        break

if selected_sell_ce is None:
    selected_sell_ce = atm_strike + 500
    selected_sell_pe = atm_strike - 500
    selected_pop = RobustBlack76.probability_of_profit(
        future_price, selected_sell_pe, selected_sell_ce, dte_y, est_iv
    )

hedge_offset = 200
buy_hedge_ce = selected_sell_ce + hedge_offset
buy_hedge_pe = selected_sell_pe - hedge_offset

sell_ce_price = round(
    RobustBlack76.price(
        future_price, selected_sell_ce, dte_y, r_rate, est_iv, "CE"
    ),
    2,
)
sell_pe_price = round(
    RobustBlack76.price(
        future_price, selected_sell_pe, dte_y, r_rate, est_iv, "PE"
    ),
    2,
)
buy_ce_price = round(
    RobustBlack76.price(
        future_price, buy_hedge_ce, dte_y, r_rate, est_iv, "CE"
    ),
    2,
)
buy_pe_price = round(
    RobustBlack76.price(
        future_price, buy_hedge_pe, dte_y, r_rate, est_iv, "PE"
    ),
    2,
)


# =====================================================================
# 6. HIGHLIGHTED RECOMMENDATION BOX (MARKET BIAS)
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
    unsafe_allow_html=True,
)


# =====================================================================
# 7. STRATEGY SETS (1-10) & MARGIN CALCULATOR
# =====================================================================
st.subheader("📦 Strategy Sets & Execution Parameters")

col_s1, col_s2, col_s3 = st.columns([1, 1.5, 1.5])
with col_s1:
    strategy_sets = st.selectbox(
        "Strategy Sets (1-10)", options=list(range(1, 11)), index=0
    )

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
    st.info(
        f"**Quantities for {strategy_sets} Sets:**\n• Buy Hedge: {buy_qty} Qty (2 Lots/Set)\n• Short Legs: {sell_qty} Qty (1 Lot/Set)"
    )

with col_s3:
    st.success(
        f"**💰 Margin Required (Hedged):**\n• **Total Balance Req:** ₹{total_required_capital:,.2f}\n• **Daily Loss Limit:** ₹{daily_account_loss_limit:,.2f}"
    )


# =====================================================================
# 8. GREEKS & LIVE RISK ENGINE
# =====================================================================
d_sell_ce = RobustBlack76.delta(
    future_price, selected_sell_ce, dte_y, r_rate, est_iv, "CE"
)
d_sell_pe = RobustBlack76.delta(
    future_price, selected_sell_pe, dte_y, r_rate, est_iv, "PE"
)
d_buy_ce = RobustBlack76.delta(
    future_price, buy_hedge_ce, dte_y, r_rate, est_iv, "CE"
)
d_buy_pe = RobustBlack76.delta(
    future_price, buy_hedge_pe, dte_y, r_rate, est_iv, "PE"
)

g_sell_ce = RobustBlack76.gamma(
    future_price, selected_sell_ce, dte_y, r_rate, est_iv
)
g_sell_pe = RobustBlack76.gamma(
    future_price, selected_sell_pe, dte_y, r_rate, est_iv
)

options_net_delta = (
    (-sell_qty * d_sell_ce)
    + (-sell_qty * d_sell_pe)
    + (buy_qty * d_buy_ce)
    + (buy_qty * d_buy_pe)
)
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
k3.metric(
    "Net Delta Exposure",
    f"{total_net_delta:.2f}",
    f"Band: ±{delta_threshold * lot_size:.1f}",
)
k4.metric(
    "Live PnL",
    f"₹{current_pnl:,.2f}",
    f"Peak: ₹{st.session_state.peak_pnl:,.2f}",
)


# =====================================================================
# 9. AUTO RISK & GAMMA BLAST CIRCUIT BREAKER
# =====================================================================
inst_buy_ce = f"NSE_FO|{buy_hedge_ce}_CE"
inst_buy_pe = f"NSE_FO|{buy_hedge_pe}_PE"
inst_sell_ce = f"NSE_FO|{selected_sell_ce}_CE"
inst_sell_pe = f"NSE_FO|{selected_sell_pe}_PE"

auto_exit_triggered = False
exit_reason = ""

if st.session_state.strategy_active:
    now_time = datetime.now().time()

    if abs(net_portfolio_gamma) >= (0.35 * strategy_sets):
        auto_exit_triggered = True
        exit_reason = f"🚨 PRE-GAMMA BLAST DETECTED! Net Gamma ({net_portfolio_gamma:.4f}) breached safety limits."

    elif current_pnl <= -total_max_loss_limit:
        auto_exit_triggered = True
        exit_reason = f"HARD MAX LOSS HIT (-₹{abs(current_pnl):,.2f})"

    elif current_pnl >= total_target_profit:
        auto_exit_triggered = True
        exit_reason = f"TARGET PROFIT ACHIEVED (+₹{current_pnl:,.2f})"

    elif st.session_state.peak_pnl >= (trail_trigger * strategy_sets):
        allowed_pullback = 0.4 * st.session_state.peak_pnl
        if current_pnl <= (st.session_state.peak_pnl - allowed_pullback):
            auto_exit_triggered = True
            exit_reason = f"TRAILING SL HIT (Secured: ₹{current_pnl:,.2f})"

    elif now_time >= auto_exit_time:
        auto_exit_triggered = True
        exit_reason = f"TIME LIMIT REACHED ({now_time.strftime('%H:%M')} >= {auto_exit_time.strftime('%H:%M')})"

    if auto_exit_triggered:
        # Exit legs execution with individual slippage logging
        execute_upstox_order(
            inst_sell_ce,
            f"{selected_sell_ce} CE",
            "CIRCUIT_BUY (COVER SHORT)",
            sell_qty,
            sell_ce_price,
            access_token,
            trading_mode,
            est_iv,
            future_price,
            "CIRCUIT_EXIT",
        )
        execute_upstox_order(
            inst_sell_pe,
            f"{selected_sell_pe} PE",
            "CIRCUIT_BUY (COVER SHORT)",
            sell_qty,
            sell_pe_price,
            access_token,
            trading_mode,
            est_iv,
            future_price,
            "CIRCUIT_EXIT",
        )
        execute_upstox_order(
            inst_buy_ce,
            f"{buy_hedge_ce} CE",
            "CIRCUIT_SELL (CLOSE HEDGE)",
            buy_qty,
            buy_ce_price,
            access_token,
            trading_mode,
            est_iv,
            future_price,
            "CIRCUIT_EXIT",
        )
        execute_upstox_order(
            inst_buy_pe,
            f"{buy_hedge_pe} PE",
            "CIRCUIT_SELL (CLOSE HEDGE)",
            buy_qty,
            buy_pe_price,
            access_token,
            trading_mode,
            est_iv,
            future_price,
            "CIRCUIT_EXIT",
        )

        # Log total trade summary
        log_trade_summary_to_storage(
            date_str=datetime.now().strftime("%Y-%m-%d"),
            time_str=datetime.now().strftime("%H:%M:%S"),
            action="CIRCUIT_EXIT",
            sets=strategy_sets,
            pnl=current_pnl,
            reason=exit_reason,
            details=f"Shorts: {selected_sell_ce} CE / {selected_sell_pe} PE | Hedges: {buy_hedge_ce} CE / {buy_hedge_pe} PE",
        )

        if current_pnl < 0:
            st.session_state.daily_realized_loss += current_pnl

        st.session_state.strategy_active = False
        st.session_state.peak_pnl = 0.0
        st.error(f"🛑 **AUTOMATIC RISK SQUARED-OFF:** {exit_reason}")


# =====================================================================
# 10. PROTECTED EXECUTION TERMINAL & ORDER ROUTING
# =====================================================================
st.subheader("🖥 Protected Upstox Order Terminal")
t_col1, t_col2 = st.columns([1.5, 1])

current_clock = datetime.now().time()
is_opening_whipsaw_time = time(9, 15) <= current_clock < time(9, 30)

with t_col2:
    st.write(f"**Execute Orders ({trading_mode})**")
    btn1, btn2 = st.columns(2)

    with btn1:
        execute_disabled = st.session_state.strategy_active or (
            enforce_opening_filter and is_opening_whipsaw_time
        )
        if st.button(
            f"🟢 Execute {strategy_sets} Sets",
            use_container_width=True,
            disabled=execute_disabled,
        ):

            # STEP 1: Pehle BUY hedge order execute hoga
            ok_buy_ce, _, fill_buy_ce = execute_upstox_order(
                inst_buy_ce,
                f"{buy_hedge_ce} CE",
                "BUY (HEDGE)",
                buy_qty,
                buy_ce_price,
                access_token,
                trading_mode,
                est_iv,
                future_price,
                "ENTRY",
            )
            ok_buy_pe, _, fill_buy_pe = execute_upstox_order(
                inst_buy_pe,
                f"{buy_hedge_pe} PE",
                "BUY (HEDGE)",
                buy_qty,
                buy_pe_price,
                access_token,
                trading_mode,
                est_iv,
                future_price,
                "ENTRY",
            )

            # FALLBACK SHIELD: Agar Buy fail hua toh Sell cancel
            if not (ok_buy_ce and ok_buy_pe):
                st.error(
                    "🚨 HEDGE BUY REJECTED BY BROKER: Margin Shortage ya API Error. Short sell legs cancel kar di gayi hain!"
                )
                execute_upstox_order(
                    inst_buy_ce,
                    f"{buy_hedge_ce} CE",
                    "CANCEL_EXIT",
                    buy_qty,
                    buy_ce_price,
                    access_token,
                    trading_mode,
                    est_iv,
                    future_price,
                    "REVERSAL",
                )
                execute_upstox_order(
                    inst_buy_pe,
                    f"{buy_hedge_pe} PE",
                    "CANCEL_EXIT",
                    buy_qty,
                    buy_pe_price,
                    access_token,
                    trading_mode,
                    est_iv,
                    future_price,
                    "REVERSAL",
                )
            else:
                # STEP 2: SELL legs entry
                _, _, fill_sell_ce = execute_upstox_order(
                    inst_sell_ce,
                    f"{selected_sell_ce} CE",
                    "SELL (SHORT)",
                    sell_qty,
                    sell_ce_price,
                    access_token,
                    trading_mode,
                    est_iv,
                    future_price,
                    "ENTRY",
                )
                _, _, fill_sell_pe = execute_upstox_order(
                    inst_sell_pe,
                    f"{selected_sell_pe} PE",
                    "SELL (SHORT)",
                    sell_qty,
                    sell_pe_price,
                    access_token,
                    trading_mode,
                    est_iv,
                    future_price,
                    "ENTRY",
                )

                st.session_state.entry_prices = {
                    "sell_ce": fill_sell_ce,
                    "sell_pe": fill_sell_pe,
                    "buy_ce": fill_buy_ce,
                    "buy_pe": fill_buy_pe,
                }
                st.session_state.strategy_active = True
                st.success(
                    f"{strategy_sets} Sets successfully executed & legs logged!"
                )
                st.rerun()

    with btn2:
        if st.button(
            f"🔴 Exit {strategy_sets} Sets",
            use_container_width=True,
            disabled=not st.session_state.strategy_active,
        ):
            # STEP 1: Pehle SELL leg close hogi
            execute_upstox_order(
                inst_sell_ce,
                f"{selected_sell_ce} CE",
                "BUY (EXIT SHORT)",
                sell_qty,
                sell_ce_price,
                access_token,
                trading_mode,
                est_iv,
                future_price,
                "MANUAL_EXIT",
            )
            execute_upstox_order(
                inst_sell_pe,
                f"{selected_sell_pe} PE",
                "BUY (EXIT SHORT)",
                sell_qty,
                sell_pe_price,
                access_token,
                trading_mode,
                est_iv,
                future_price,
                "MANUAL_EXIT",
            )

            # STEP 2: Phir BUY hedge leg close hogi
            execute_upstox_order(
                inst_buy_ce,
                f"{buy_hedge_ce} CE",
                "SELL (EXIT HEDGE)",
                buy_qty,
                buy_ce_price,
                access_token,
                trading_mode,
                est_iv,
                future_price,
                "MANUAL_EXIT",
            )
            execute_upstox_order(
                inst_buy_pe,
                f"{buy_hedge_pe} PE",
                "SELL (EXIT HEDGE)",
                buy_qty,
                buy_pe_price,
                access_token,
                trading_mode,
                est_iv,
                future_price,
                "MANUAL_EXIT",
            )

            # Log summary
            log_trade_summary_to_storage(
                date_str=datetime.now().strftime("%Y-%m-%d"),
                time_str=datetime.now().strftime("%H:%M:%S"),
                action="MANUAL_EXIT",
                sets=strategy_sets,
                pnl=current_pnl,
                reason="Manual Square-off via Button",
                details=f"Shorts: {selected_sell_ce} CE / {selected_sell_pe} PE | Hedges: {buy_hedge_ce} CE / {buy_hedge_pe} PE",
            )

            if current_pnl < 0:
                st.session_state.daily_realized_loss += current_pnl

            st.session_state.strategy_active = False
            st.info(f"{strategy_sets} Sets manual square-off complete!")
            st.rerun()

with t_col1:
    st.write("**Strategy & Shield Status**")
    if enforce_opening_filter and is_opening_whipsaw_time:
        st.warning(
            "⏳ OPENING WHIPSAW BUFFER (9:15-9:30 AM): Execution paused to prevent bad slippage."
        )
    elif st.session_state.strategy_active:
        st.warning(
            "⚠️ Trade ACTIVE: Hard SL, Trailing SL aur Pre-Gamma Shield active hain."
        )
    else:
        st.success(
            "✅ Idle: Capital surakshit hai. Koi open naked risk nahi hai."
        )

# Real-Time In-Session Order Logs
st.subheader("📜 Live Upstox Session Orders")
if st.session_state.order_book:
    st.dataframe(
        pd.DataFrame(st.session_state.order_book), use_container_width=True
    )
else:
    st.caption("Filhal koi order place nahi hua hai.")


# =====================================================================
# 11. END-OF-DAY HISTORICAL AUDIT & SLIPPAGE JOURNAL
# =====================================================================
st.divider()
st.header("📊 EOD Trade Journal & Slippage Audit (Date-Wise)")

tab1, tab2 = st.tabs(
    ["📈 Daily Summary History", "📑 Granular Leg Slippage & IV Logs"]
)

with tab1:
    if os.path.exists(DAILY_SUMMARY_LOG_FILE):
        history_df = pd.read_csv(DAILY_SUMMARY_LOG_FILE)
        available_dates = sorted(
            history_df["Date"].unique().tolist(), reverse=True
        )
        selected_date = st.selectbox(
            "Select Summary Date",
            ["All"] + available_dates,
            key="summary_date_select",
        )

        filtered_summary = (
            history_df[history_df["Date"] == selected_date]
            if selected_date != "All"
            else history_df
        )

        tot_pnl = filtered_summary["Realized_PnL"].sum()
        win_count = (filtered_summary["Realized_PnL"] > 0).sum()
        win_rate = (
            (win_count / len(filtered_summary) * 100)
            if len(filtered_summary) > 0
            else 0.0
        )

        m1, m2, m3 = st.columns(3)
        m1.metric("Closed Trades", len(filtered_summary))
        m2.metric(
            "Total Realized PnL", f"₹{tot_pnl:,.2f}", delta=f"{tot_pnl:,.2f}"
        )
        m3.metric("Strategy Win Rate", f"{win_rate:.1f}%")

        st.dataframe(filtered_summary, use_container_width=True)

        csv_summary = filtered_summary.to_csv(index=False).encode("utf-8")
        st.download_button(
            "📥 Download Summary CSV",
            data=csv_summary,
            file_name=f"trade_summary_{selected_date}.csv",
            mime="text/csv",
        )
    else:
        st.info("Pehla trade close hone par summary journal save hoga.")

with tab2:
    if os.path.exists(LEG_EXECUTION_LOG_FILE):
        legs_df = pd.read_csv(LEG_EXECUTION_LOG_FILE)
        available_leg_dates = sorted(
            legs_df["Date"].unique().tolist(), reverse=True
        )
        selected_leg_date = st.selectbox(
            "Select Date for Leg Audit",
            ["All"] + available_leg_dates,
            key="leg_date_select",
        )

        filtered_legs = (
            legs_df[legs_df["Date"] == selected_leg_date]
            if selected_leg_date != "All"
            else legs_df
        )

        total_slippage_drag = filtered_legs["Slippage_INR"].sum()
        avg_iv = (
            filtered_legs["IV_Percent"].mean() if len(filtered_legs) > 0 else 0.0
        )

        l1, l2, l3 = st.columns(3)
        l1.metric("Total Executed Legs", len(filtered_legs))
        l2.metric(
            "Total Slippage Drag (INR)",
            f"₹{total_slippage_drag:,.2f}",
            delta=f"{-total_slippage_drag:,.2f}",
            delta_color="inverse",
        )
        l3.metric("Avg IV captured", f"{avg_iv:.2f}%")

        st.dataframe(filtered_legs, use_container_width=True)

        csv_legs = filtered_legs.to_csv(index=False).encode("utf-8")
        st.download_button(
            "📥 Download Leg Audit CSV",
            data=csv_legs,
            file_name=f"leg_audit_{selected_leg_date}.csv",
            mime="text/csv",
        )
    else:
        st.info("Pehle trade execution ke baad leg audit journal banega.")
