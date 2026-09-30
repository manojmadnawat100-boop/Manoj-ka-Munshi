import math
from datetime import datetime
from dataclasses import dataclass
from typing import Dict, List
import streamlit as st
import pandas as pd
from scipy.optimize import brentq
from scipy.stats import norm

st.set_page_config(
    page_title="Nifty Institutional Terminal - Anti-Gamma Guard",
    page_icon="⚡",
    layout="wide"
)

# =====================================================================
# 1. QUANTITATIVE PRICING, GREEKS (DELTA & GAMMA) ENGINE
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
    def gamma(cls, F: float, K: float, T: float, r: float, sigma: float) -> float:
        """Calculates Black-76 Gamma w.r.t underlying futures."""
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
# 2. SESSION & OMS (Order Management System)
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

def log_order(symbol: str, side: str, qty: int, price: float, order_type: str = "LIMIT"):
    order_entry = {
        "Timestamp": datetime.now().strftime("%H:%M:%S"),
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
    st.header("⚙️ General Settings")
    lot_size = st.number_input("Nifty Lot Size", min_value=25, max_value=150, value=75, step=25)
    delta_threshold = st.slider("Delta Rebalance Tolerance (Lots)", 0.1, 2.0, 0.5, 0.1)
    r_rate = st.number_input("Risk-Free Rate", min_value=0.01, max_value=0.15, value=0.065, step=0.005)
    days_to_expiry = st.slider("Days to Expiry (DTE)", 0.05, 30.0, 2.0, 0.05)
    est_iv = st.slider("Implied Volatility (IV %)", 5.0, 50.0, 14.5, 0.5) / 100.0

    st.divider()
    st.header("💥 Early Gamma Blast Circuit Breakers")
    # Gamma Threshold per Lot
    max_net_gamma = st.number_input("Max Net Portfolio Gamma Cutoff", min_value=0.05, max_value=2.0, value=0.35, step=0.05,
                                    help="Net short gamma isse zyada hone par gamma explosion se pehle auto-exit hoga.")
    strike_proximity_pct = st.slider("Proximity to Short Strike Alert (%)", 0.2, 1.5, 0.6, 0.1,
                                     help="Price agar short strike ke itne kareeb aayegi toh gamma risk active hoga.")

    st.divider()
    st.header("🛡️ Hard Risk & Profit Protectors")
    max_loss_per_set = st.number_input("Max Loss Limit Per Set (₹)", min_value=500, max_value=25000, value=2500, step=250)
    target_profit_per_set = st.number_input("Target Profit Per Set (₹)", min_value=500, max_value=25000, value=4000, step=250)
    trail_trigger = st.number_input("Trailing Trigger Profit (₹/Set)", min_value=500, max_value=20000, value=2000, step=250)
    auto_exit_time = st.time_input("Expiry Day Auto-Kill Time", datetime.strptime("15:10:00", "%H:%M:%S").time())

    st.divider()
    if st.button("🚨 EMERGENCY KILL SWITCH", use_container_width=True, type="primary"):
        st.session_state.hedge_position = 0
        st.session_state.strategy_active = False
        st.session_state.peak_pnl = 0.0
        log_order("PORTFOLIO_ALL", "SQUARE_OFF", 0, 0.0, "MARKET_KILL")
        st.warning("All active positions terminated!")

dte_y = max(days_to_expiry / 365.25, 1e-5)


# =====================================================================
# 4. AUTO STRIKE SELECTOR (Minimum 80% PoP)
# =====================================================================
st.title("⚡ Nifty Algo Terminal (80%+ PoP & Pre-Gamma Blast Shield)")

future_price = st.number_input("Nifty Future LTP", min_value=15000.0, max_value=35000.0, value=24800.0, step=5.0)
strike_step = 50
atm_strike = int(round(future_price / strike_step) * strike_step)

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
# 5. STRATEGY SETS (1-10) & MARGIN CALCULATOR
# =====================================================================
st.divider()
st.subheader("📦 Strategy Sets & Execution Parameters")

sets_col1, sets_col2, sets_col3 = st.columns([1, 1.5, 1.5])
with sets_col1:
    strategy_sets = st.selectbox(
        "Trade Sets (1-10)",
        options=list(range(1, 11)),
        index=0
    )

sell_qty = int(strategy_sets * 1 * lot_size)
buy_qty = int(strategy_sets * 2 * lot_size)

buy_premium_total = (buy_ce_price + buy_pe_price) * buy_qty
sell_premium_total = (sell_ce_price + sell_pe_price) * sell_qty
net_cash_flow = sell_premium_total - buy_premium_total

# Margin calculation
spread_width = hedge_offset
max_spread_risk = spread_width * lot_size
base_span_exposure_per_set = max(35000.0, max_spread_risk * 1.5)
total_hedged_span_margin = base_span_exposure_per_set * strategy_sets
total_required_capital = total_hedged_span_margin + buy_premium_total

total_max_loss_limit = max_loss_per_set * strategy_sets
total_target_profit = target_profit_per_set * strategy_sets

with sets_col2:
    st.info(
        f"**Order Breakdown ({strategy_sets} Sets):**\n"
        f"• **Buy Leg (Margin Hedge):** {strategy_sets * 2} Lots = **{buy_qty} Qty**\n"
        f"• **Sell Leg (Short Leg):** {strategy_sets * 1} Lot = **{sell_qty} Qty**"
    )

with sets_col3:
    st.success(
        f"**💰 Margin & Safeguards:**\n"
        f"• **Req Capital:** **₹{total_required_capital:,.2f}**\n"
        f"• **Hard Stop-Loss:** -₹{total_max_loss_limit:,.2f}\n"
        f"• **Target Profit:** +₹{total_target_profit:,.2f}"
    )


# =====================================================================
# 6. GREEKS ENGINE (DELTA + ADVANCED GAMMA BLAST MONITOR)
# =====================================================================
# Deltas
d_sell_ce = RobustBlack76.delta(future_price, selected_sell_ce, dte_y, r_rate, est_iv, "CE")
d_sell_pe = RobustBlack76.delta(future_price, selected_sell_pe, dte_y, r_rate, est_iv, "PE")
d_buy_ce = RobustBlack76.delta(future_price, buy_hedge_ce, dte_y, r_rate, est_iv, "CE")
d_buy_pe = RobustBlack76.delta(future_price, buy_hedge_pe, dte_y, r_rate, est_iv, "PE")

# Gammas
g_sell_ce = RobustBlack76.gamma(future_price, selected_sell_ce, dte_y, r_rate, est_iv)
g_sell_pe = RobustBlack76.gamma(future_price, selected_sell_pe, dte_y, r_rate, est_iv)
g_buy_ce = RobustBlack76.gamma(future_price, buy_hedge_ce, dte_y, r_rate, est_iv)
g_buy_pe = RobustBlack76.gamma(future_price, buy_hedge_pe, dte_y, r_rate, est_iv)

# Net Portfolio Greeks
options_net_delta = (-sell_qty * d_sell_ce) + (-sell_qty * d_sell_pe) + (buy_qty * d_buy_ce) + (buy_qty * d_buy_pe)
total_net_delta = options_net_delta + float(st.session_state.hedge_position)

# Portfolio Net Gamma Risk (Negative gamma signifies blast vulnerability)
net_portfolio_gamma = (-sell_qty * g_sell_ce) + (-sell_qty * g_sell_pe) + (buy_qty * g_buy_ce) + (buy_qty * g_buy_pe)

# Proximity to short strikes
dist_to_call = abs(selected_sell_ce - future_price) / future_price * 100.0
dist_to_put = abs(future_price - selected_sell_pe) / future_price * 100.0
min_proximity = min(dist_to_call, dist_to_put)

# PnL tracking
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
k2.metric("Net Gamma Risk", f"{net_portfolio_gamma:.4f}", f"Threshold: ±{max_net_gamma:.2f}")
k3.metric("Short Strike Distance", f"{min_proximity:.2f}%", f"Min Alert: {strike_proximity_pct}%")
k4.metric("Live PnL", f"₹{current_pnl:,.2f}", f"Peak: ₹{st.session_state.peak_pnl:,.2f}")


# =====================================================================
# 7. EARLY GAMMA BLAST DETECTION & AUTO-SQUARE OFF CIRCUIT
# =====================================================================
auto_exit_triggered = False
exit_reason = ""

if st.session_state.strategy_active:
    now_time = datetime.now().time()
    
    # --- GAMMA BLAST TRIGGER A: Portfolio Net Gamma Spike ---
    if abs(net_portfolio_gamma) >= (max_net_gamma * strategy_sets):
        auto_exit_triggered = True
        exit_reason = f"🚨 PRE-GAMMA BLAST DETECTED! Net Gamma ({net_portfolio_gamma:.4f}) breached safety limits."

    # --- GAMMA BLAST TRIGGER B: Expiry Day Strike Proximity Spike ---
    elif (days_to_expiry <= 1.0) and (min_proximity <= strike_proximity_pct):
        auto_exit_triggered = True
        exit_reason = f"🚨 DANGEROUS STRIKE PROXIMITY! Spot is only {min_proximity:.2f}% away from short strike on low DTE."

    # Hard Max Loss
    elif current_pnl <= -total_max_loss_limit:
        auto_exit_triggered = True
        exit_reason = f"HARD MAX LOSS HIT (-₹{abs(current_pnl):,.2f})"

    # Target Profit
    elif current_pnl >= total_target_profit:
        auto_exit_triggered = True
        exit_reason = f"TARGET PROFIT ACHIEVED (+₹{current_pnl:,.2f})"

    # Dynamic Trailing SL
    elif st.session_state.peak_pnl >= (trail_trigger * strategy_sets):
        allowed_pullback = 0.4 * st.session_state.peak_pnl
        if current_pnl <= (st.session_state.peak_pnl - allowed_pullback):
            auto_exit_triggered = True
            exit_reason = f"TRAILING SL HIT (Secured: ₹{current_pnl:,.2f})"

    # 3:10 PM Rule
    elif now_time >= auto_exit_time:
        auto_exit_triggered = True
        exit_reason = f"TIME LIMIT REACHED ({now_time.strftime('%H:%M')} >= {auto_exit_time.strftime('%H:%M')})"

    if auto_exit_triggered:
        # Pre-Gamma auto exit sequence: PEHLE Short cover, PHIR Hedge exit
        log_order(f"{selected_sell_ce}CE", "CIRCUIT_BUY (COVER SHORT)", sell_qty, sell_ce_price, "GAMMA_SHIELD")
        log_order(f"{selected_sell_pe}PE", "CIRCUIT_BUY (COVER SHORT)", sell_qty, sell_pe_price, "GAMMA_SHIELD")
        log_order(f"{buy_hedge_ce}CE", "CIRCUIT_SELL (CLOSE HEDGE)", buy_qty, buy_ce_price, "GAMMA_SHIELD")
        log_order(f"{buy_hedge_pe}PE", "CIRCUIT_SELL (CLOSE HEDGE)", buy_qty, buy_pe_price, "GAMMA_SHIELD")
        
        st.session_state.strategy_active = False
        st.session_state.peak_pnl = 0.0
        st.error(f"🛑 **AUTOMATIC RISK SQUARED-OFF:** {exit_reason}")


# =====================================================================
# 8. EXECUTION TERMINAL (STRICT ORDER SEQUENCING)
# =====================================================================
st.subheader("🖥 Execution Terminal")
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
    st.write(f"**Execute Orders (Req: ₹{total_required_capital:,.0f})**")
    btn1, btn2 = st.columns(2)
    
    with btn1:
        if st.button(f"🟢 Execute {strategy_sets} Sets", use_container_width=True, disabled=st.session_state.strategy_active):
            # STEP 1: Pehle BUY hedge execute hoga margin benefit ke liye
            log_order(f"{buy_hedge_ce}CE", "BUY (ENTRY)", buy_qty, buy_ce_price)
            log_order(f"{buy_hedge_pe}PE", "BUY (ENTRY)", buy_qty, buy_pe_price)
            
            # STEP 2: Baad me SELL orders place honge
            log_order(f"{selected_sell_ce}CE", "SELL (ENTRY)", sell_qty, sell_ce_price)
            log_order(f"{selected_sell_pe}PE", "SELL (ENTRY)", sell_qty, sell_pe_price)

            st.session_state.entry_prices = {
                "sell_ce": sell_ce_price,
                "sell_pe": sell_pe_price,
                "buy_ce": buy_ce_price,
                "buy_pe": buy_pe_price
            }
            st.session_state.strategy_active = True
            st.session_state.peak_pnl = 0.0
            st.success(f"{strategy_sets} Sets successfully executed with Anti-Gamma Guard active!")
            st.rerun()
            
    with btn2:
        if st.button(f"🔴 Exit {strategy_sets} Sets", use_container_width=True, disabled=not st.session_state.strategy_active):
            # STEP 1: Pehle SELL leg buy-back hokar close hogi taaki naked risk na rahe
            log_order(f"{selected_sell_ce}CE", "BUY (EXIT SHORT)", sell_qty, sell_ce_price)
            log_order(f"{selected_sell_pe}PE", "BUY (EXIT SHORT)", sell_qty, sell_pe_price)
            
            # STEP 2: Baad me BUY hedge positions exit hongi
            log_order(f"{buy_hedge_ce}CE", "SELL (EXIT HEDGE)", buy_qty, buy_ce_price)
            log_order(f"{buy_hedge_pe}PE", "SELL (EXIT HEDGE)", buy_qty, buy_pe_price)

            st.session_state.strategy_active = False
            st.session_state.peak_pnl = 0.0
            st.info(f"{strategy_sets} Sets manual square off ho gaye!")
            st.rerun()

# Order Log Sequence Display
st.subheader("📜 Live Order Log Sequence")
if st.session_state.order_book:
    st.dataframe(pd.DataFrame(st.session_state.order_book), use_container_width=True)
else:
    st.caption("Filhal koi active order placed nahi hua hai.")
