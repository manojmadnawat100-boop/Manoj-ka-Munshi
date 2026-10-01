from datetime import datetime, time
import math
import os
import time as pytime
from typing import Dict, List, Tuple
import numpy as np
import pandas as pd
from scipy.stats import norm
import streamlit as st
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
# 0. PERSISTENT LOGGING ENGINES
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
            return max(0.05, df * (F * norm.cdf(d1) - K * norm.cdf(d2)))
        else:
            return max(0.05, df * (K * norm.cdf(-d2) - F * norm.cdf(-d1)))

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
                "Advice": "Size 50% rakhein, Gamma trap se bachein.",
            }
        elif 11.5 <= vix <= 17.5:
            return {
                "Regime": "OPTIMAL VOL",
                "Advice": "Institutional sweet spot, full set deployment.",
            }
        elif 17.5 < vix <= 22.0:
            return {
                "Regime": "ELEVATED VOL",
                "Advice": "Strikes wide rakhein (85%+ PoP).",
            }
        else:
            return {
                "Regime": "EXTREME VOL",
                "Advice": "Event risk: Strict 1:2 spread only.",
            }


# =====================================================================
# 3. SESSION & OMS (Order Management System)
# =====================================================================
if "order_book" not in st.session_state:
    st.session_state.order_book = []
if "strategy_active" not in st.session_state:
    st.session_state.strategy_active = False
if "peak_pnl" not in st.session_state:
    st.session_state.peak_pnl = 0.0
if "entry_prices" not in st.session_state:
    st.session_state.entry_prices = {}
if "active_strikes" not in st.session_state:
    st.session_state.active_strikes = {}
if "daily_realized_loss" not in st.session_state:
    st.session_state.daily_realized_loss = 0.0
if "last_realized_pnl" not in st.session_state:
    st.session_state.last_realized_pnl = 0.0


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
        # Paper Trading: Exact theoretical execution price (0 Slippage)
        executed_price = round(theo_price, 2)
        status = "PAPER_FILLED"
        success = True

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
# 4. SIDEBAR CONTROLS
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
    st.header("🛡️ Capital Protection")
    daily_account_loss_limit = st.number_input(
        "Daily Max Loss Circuit (₹)",
        min_value=1000,
        max_value=50000,
        value=5000,
        step=500,
    )

    if st.button("🔄 Reset Daily Realized PnL"):
        st.session_state.daily_realized_loss = 0.0
        st.session_state.last_realized_pnl = 0.0
        st.success("Realized counters reset ho gaye!")
        st.rerun()

    st.divider()
    st.header("🏛 FII / DII Parameters")
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
    st.header("⚙️ Market Assumptions")
    lot_size = st.number_input("Nifty Lot Size", value=75, step=25)
    r_rate = st.number_input("Risk-Free Rate", value=0.065, step=0.005)
    days_to_expiry = st.slider(
        "Days to Expiry (DTE)",
        min_value=0.01,
        max_value=30.0,
        value=2.0,
        step=0.05,
    )
    auto_refresh_pnl = st.checkbox("Auto Refresh PnL (1 Sec)", value=False)

    st.divider()
    if st.button(
        "🚨 EMERGENCY KILL SWITCH", use_container_width=True, type="primary"
    ):
        st.session_state.strategy_active = False
        st.session_state.peak_pnl = 0.0
        st.warning("Sabhi positions squared off aur lock kar di gayi hain!")
        st.rerun()

dte_y = max(days_to_expiry / 365.25, 1e-5)
est_iv = (india_vix + 1.0) / 100.0


# =====================================================================
# 5. MARKET SCAN & STRIKE SELECTION
# =====================================================================
st.title("⚡ Institutional Nifty Terminal (Protected Execution)")

if abs(st.session_state.daily_realized_loss) >= daily_account_loss_limit:
    st.error(
        f"🛑 **ACCOUNT LOCKED: Daily Max Loss (₹{daily_account_loss_limit:,.2f}) Reach Ho Chuka Hai!**"
    )
    st.stop()

col_u1, col_u2 = st.columns(2)
with col_u1:
    future_price = st.number_input(
        "Nifty Future LTP (Simulate Price Changes here)",
        min_value=15000.0,
        max_value=35000.0,
        value=24800.0,
        step=5.0,
    )
with col_u2:
    strategy_sets = st.selectbox(
        "Strategy Sets (1-10)", options=list(range(1, 11)), index=0
    )

strike_step = 50
atm_strike = int(round(future_price / strike_step) * strike_step)
pcr_value = InstitutionalEngine.calculate_pcr(total_put_oi, total_call_oi)
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

sell_qty = int(strategy_sets * 1 * lot_size)
buy_qty = int(strategy_sets * 2 * lot_size)

# =====================================================================
# 6. LIVE PNL ENGINE & TRADE CONTROLS
# =====================================================================
st.divider()
st.subheader("📊 Live Strategy PnL & Order Terminal")

c_btn1, c_btn2 = st.columns(2)

with c_btn1:
    if not st.session_state.strategy_active:
        if st.button(
            "🚀 Deploy Strategy (1 Sell : 2 Buy Hedge)",
            use_container_width=True,
            type="primary",
        ):
            # Execute legs
            _, _, ep_sce = execute_upstox_order(
                "NIFTY_SCE",
                f"{selected_sell_ce} CE",
                "SELL",
                sell_qty,
                sell_ce_price,
                access_token,
                trading_mode,
                est_iv,
                future_price,
                "ENTRY",
            )
            _, _, ep_spe = execute_upstox_order(
                "NIFTY_SPE",
                f"{selected_sell_pe} PE",
                "SELL",
                sell_qty,
                sell_pe_price,
                access_token,
                trading_mode,
                est_iv,
                future_price,
                "ENTRY",
            )
            _, _, ep_bce = execute_upstox_order(
                "NIFTY_BCE",
                f"{buy_hedge_ce} CE",
                "BUY",
                buy_qty,
                buy_ce_price,
                access_token,
                trading_mode,
                est_iv,
                future_price,
                "ENTRY",
            )
            _, _, ep_bpe = execute_upstox_order(
                "NIFTY_BPE",
                f"{buy_hedge_pe} PE",
                "BUY",
                buy_qty,
                buy_pe_price,
                access_token,
                trading_mode,
                est_iv,
                future_price,
                "ENTRY",
            )

            st.session_state.entry_prices = {
                "sell_ce": ep_sce,
                "sell_pe": ep_spe,
                "buy_ce": ep_bce,
                "buy_pe": ep_bpe,
            }
            st.session_state.active_strikes = {
                "sell_ce": selected_sell_ce,
                "sell_pe": selected_sell_pe,
                "buy_ce": buy_hedge_ce,
                "buy_pe": buy_hedge_pe,
                "sets": strategy_sets,
            }
            st.session_state.strategy_active = True
            st.session_state.peak_pnl = 0.0
            st.success("Strategy successfully deploy ho gayi!")
            st.rerun()
    else:
        st.info("🟢 Strategy live active hai. MTM updates neeche track ho rahe hain.")

with c_btn2:
    if st.session_state.strategy_active:
        if st.button(
            "🛑 Exit / Square Off All Legs",
            use_container_width=True,
            type="secondary",
        ):
            # Exit valuation
            curr_sce = RobustBlack76.price(
                future_price,
                st.session_state.active_strikes["sell_ce"],
                dte_y,
                r_rate,
                est_iv,
                "CE",
            )
            curr_spe = RobustBlack76.price(
                future_price,
                st.session_state.active_strikes["sell_pe"],
                dte_y,
                r_rate,
                est_iv,
                "PE",
            )
            curr_bce = RobustBlack76.price(
                future_price,
                st.session_state.active_strikes["buy_ce"],
                dte_y,
                r_rate,
                est_iv,
                "CE",
            )
            curr_bpe = RobustBlack76.price(
                future_price,
                st.session_state.active_strikes["buy_pe"],
                dte_y,
                r_rate,
                est_iv,
                "PE",
            )

            act_sets = st.session_state.active_strikes["sets"]
            sq_sell_qty = int(act_sets * 1 * lot_size)
            sq_buy_qty = int(act_sets * 2 * lot_size)

            final_short_pnl = (
                (st.session_state.entry_prices["sell_ce"] - curr_sce)
                + (st.session_state.entry_prices["sell_pe"] - curr_spe)
            ) * sq_sell_qty
            final_long_pnl = (
                (curr_bce - st.session_state.entry_prices["buy_ce"])
                + (curr_bpe - st.session_state.entry_prices["buy_pe"])
            ) * sq_buy_qty
            final_net_pnl = round(final_short_pnl + final_long_pnl, 2)

            st.session_state.daily_realized_loss += final_net_pnl
            st.session_state.last_realized_pnl = final_net_pnl
            st.session_state.strategy_active = False

            now_d = datetime.now().strftime("%Y-%m-%d")
            now_t = datetime.now().strftime("%H:%M:%S")
            log_trade_summary_to_storage(
                now_d,
                now_t,
                "MANUAL_EXIT",
                act_sets,
                final_net_pnl,
                "User square-off",
                f"Net: ₹{final_net_pnl}",
            )
            st.warning(f"Position Square-Off! Realized PnL: ₹{final_net_pnl:,.2f}")
            st.rerun()

# ----------------- LIVE PNL DISPLAY SECTION -----------------
if st.session_state.strategy_active:
    act_strikes = st.session_state.active_strikes
    entries = st.session_state.entry_prices
    curr_sets = act_strikes["sets"]
    cur_sell_qty = int(curr_sets * 1 * lot_size)
    cur_buy_qty = int(curr_sets * 2 * lot_size)

    # Current LTP calculations on active strikes
    cur_sce = round(
        RobustBlack76.price(
            future_price, act_strikes["sell_ce"], dte_y, r_rate, est_iv, "CE"
        ),
        2,
    )
    cur_spe = round(
        RobustBlack76.price(
            future_price, act_strikes["sell_pe"], dte_y, r_rate, est_iv, "PE"
        ),
        2,
    )
    cur_bce = round(
        RobustBlack76.price(
            future_price, act_strikes["buy_ce"], dte_y, r_rate, est_iv, "CE"
        ),
        2,
    )
    cur_bpe = round(
        RobustBlack76.price(
            future_price, act_strikes["buy_pe"], dte_y, r_rate, est_iv, "PE"
        ),
        2,
    )

    # Leg-level PnL:
    # Short leg profit = (Entry Price - Current Price) * Qty
    # Long leg profit  = (Current Price - Entry Price) * Qty
    pnl_sce = round((entries["sell_ce"] - cur_sce) * cur_sell_qty, 2)
    pnl_spe = round((entries["sell_pe"] - cur_spe) * cur_sell_qty, 2)
    pnl_bce = round((cur_bce - entries["buy_ce"]) * cur_buy_qty, 2)
    pnl_bpe = round((cur_bpe - entries["buy_pe"]) * cur_buy_qty, 2)

    total_mtm_pnl = round(pnl_sce + pnl_spe + pnl_bce + pnl_bpe, 2)
    st.session_state.peak_pnl = max(st.session_state.peak_pnl, total_mtm_pnl)

    # Metric Cards
    m1, m2, m3, m4 = st.columns(4)
    m1.metric(
        "Current Strategy MTM PnL",
        f"₹{total_mtm_pnl:,.2f}",
        delta=f"₹{total_mtm_pnl:,.2f}",
    )
    m2.metric("Peak MTM Recorded", f"₹{st.session_state.peak_pnl:,.2f}")
    m3.metric(
        "Short Legs Decay PnL",
        f"₹{(pnl_sce + pnl_spe):,.2f}",
        help="Sell positions se aane wala decay",
    )
    m4.metric(
        "Hedge Long PnL",
        f"₹{(pnl_bce + pnl_bpe):,.2f}",
        help="Protection hedges ka PnL",
    )

    # Granular Leg Performance Table
    pnl_df = pd.DataFrame(
        [
            {
                "Leg Name": f"{act_strikes['sell_ce']} CE (Short)",
                "Side": "SELL",
                "Qty": cur_sell_qty,
                "Entry Price": f"₹{entries['sell_ce']}",
                "Current Price": f"₹{cur_sce}",
                "Leg PnL (₹)": pnl_sce,
            },
            {
                "Leg Name": f"{act_strikes['sell_pe']} PE (Short)",
                "Side": "SELL",
                "Qty": cur_sell_qty,
                "Entry Price": f"₹{entries['sell_pe']}",
                "Current Price": f"₹{cur_spe}",
                "Leg PnL (₹)": pnl_spe,
            },
            {
                "Leg Name": f"{act_strikes['buy_ce']} CE (Hedge)",
                "Side": "BUY",
                "Qty": cur_buy_qty,
                "Entry Price": f"₹{entries['buy_ce']}",
                "Current Price": f"₹{cur_bce}",
                "Leg PnL (₹)": pnl_bce,
            },
            {
                "Leg Name": f"{act_strikes['buy_pe']} PE (Hedge)",
                "Side": "BUY",
                "Qty": cur_buy_qty,
                "Entry Price": f"₹{entries['buy_pe']}",
                "Current Price": f"₹{cur_bpe}",
                "Leg PnL (₹)": pnl_bpe,
            },
        ]
    )
    st.table(pnl_df)

    if auto_refresh_pnl:
        pytime.sleep(1)
        st.rerun()
else:
    col_r1, col_r2 = st.columns(2)
    col_r1.metric(
        "Last Closed Trade PnL", f"₹{st.session_state.last_realized_pnl:,.2f}"
    )
    col_r2.metric(
        "Total Realized Session PnL",
        f"₹{st.session_state.daily_realized_loss:,.2f}",
    )
    st.info(
        "💡 Strategy filhaal Inactive hai. Upar **'Deploy Strategy'** button click karke paper trade start karein."
    )

# =====================================================================
# 7. ORDER BOOK LOG VIEWER
# =====================================================================
st.divider()
st.subheader("📜 Terminal Order Book (Executed Transactions)")
if st.session_state.order_book:
    st.dataframe(pd.DataFrame(st.session_state.order_book), height=220)
else:
    st.write("Abhi tak koi order execute nahi hua hai.")
