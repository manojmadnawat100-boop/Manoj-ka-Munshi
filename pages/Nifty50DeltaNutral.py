import math
from typing import Dict
from dataclasses import dataclass
import streamlit as st
from scipy.stats import norm
from scipy.optimize import brentq

st.set_page_config(page_title="Nifty Delta Neutral Engine", page_icon="⚡", layout="wide")

class RobustBlack76:
    @staticmethod
    def calc_d1_d2(F: float, K: float, T: float, sigma: float):
        if T <= 1e-5 or sigma <= 1e-4:
            return 0.0, 0.0
        vt = sigma * math.sqrt(T)
        d1 = (math.log(F / K) + 0.5 * (sigma ** 2) * T) / vt
        d2 = d1 - vt
        return d1, d2

    @classmethod
    def price(cls, F: float, K: float, T: float, r: float, sigma: float, option_type: str):
        if T <= 1e-5:
            return max(0.0, (F - K) if option_type.upper() == "CE" else (K - F))
        d1, d2 = cls.calc_d1_d2(F, K, T, sigma)
        df = math.exp(-r * T)
        if option_type.upper() == "CE":
            return df * (F * norm.cdf(d1) - K * norm.cdf(d2))
        else:
            return df * (K * norm.cdf(-d2) - F * norm.cdf(-d1))

    @classmethod
    def implied_volatility(cls, ltp: float, F: float, K: float, T: float, r: float, option_type: str, fallback_iv: float = 0.16):
        if T <= 1e-5 or ltp <= 0:
            return fallback_iv
        df = math.exp(-r * T)
        intrinsic = max(0.0, (F - K) if option_type.upper() == "CE" else (K - F)) * df
        
        # आर्बिट्राज/लिक्विडिटी क्रंच से बचाव
        if ltp <= intrinsic * 0.99:
            return fallback_iv

        def objective(sigma):
            return cls.price(F, K, T, r, sigma, option_type) - ltp

        try:
            return brentq(objective, 0.01, 3.5, maxiter=80, xtol=1e-4)
        except (ValueError, RuntimeError):
            return fallback_iv

    @classmethod
    def delta(cls, F: float, K: float, T: float, r: float, sigma: float, option_type: str):
        if T <= 1e-5 or sigma <= 1e-4:
            if option_type.upper() == "CE":
                return 1.0 if F > K else 0.0
            else:
                return -1.0 if F < K else 0.0
        d1, _ = cls.calc_d1_d2(F, K, T, sigma)
        df = math.exp(-r * T)
        return (df * norm.cdf(d1)) if option_type.upper() == "CE" else (-df * norm.cdf(-d1))

@dataclass
class PositionLeg:
    symbol: str
    strike: float
    option_type: str
    quantity: int
    entry_price: float
    current_iv: float = 0.16

class DeltaEngine:
    def __init__(self, lot_size: int, delta_band_lots: float, risk_free_rate: float = 0.065):
        self.lot_size = lot_size
        self.delta_band = delta_band_lots * lot_size
        self.r = risk_free_rate

    def calculate_state(self, legs: list, future_ltp: float, ticks: Dict[str, float], dte_years: float, hedge_qty: int):
        total_delta = float(hedge_qty)
        total_pnl = 0.0
        leg_metrics = []

        for leg in legs:
            ltp = ticks.get(leg.symbol, leg.entry_price)
            pnl = (ltp - leg.entry_price) * leg.quantity
            total_pnl += pnl

            iv = RobustBlack76.implied_volatility(
                ltp=ltp, F=future_ltp, K=leg.strike, T=dte_years,
                r=self.r, option_type=leg.option_type, fallback_iv=leg.current_iv
            )
            unit_delta = RobustBlack76.delta(
                F=future_ltp, K=leg.strike, T=dte_years,
                r=self.r, sigma=iv, option_type=leg.option_type
            )
            leg_delta = unit_delta * leg.quantity
            total_delta += leg_delta
            leg.current_iv = iv

            leg_metrics.append({
                "Symbol": leg.symbol,
                "Type": leg.option_type,
                "Qty": leg.quantity,
                "Entry": leg.entry_price,
                "LTP": ltp,
                "IV (%)": round(iv * 100, 2),
                "Unit Delta": round(unit_delta, 3),
                "Pos Delta": round(leg_delta, 2),
                "PnL (₹)": round(pnl, 2)
            })

        return total_delta, total_pnl, leg_metrics

# --- UI SETUP ---
st.title("🛡️ Institutional Delta-Neutral Risk Engine")
st.caption("Black-76 Dynamic Hedging Engine for Nifty 50")

with st.sidebar:
    st.header("⚙️ Parameters")
    lot_size = st.number_input("Nifty Lot Size", min_value=25, max_value=150, value=75, step=25)
    delta_threshold = st.slider("Delta Rebalance Tolerance (in Lots)", 0.1, 2.0, 0.5, 0.1)
    r_rate = st.number_input("Risk-Free Rate", min_value=0.01, max_value=0.15, value=0.065, step=0.005)
    days_to_expiry = st.slider("Days to Expiry (DTE)", 0.0, 30.0, 3.5, 0.5)

col1, col2, col3 = st.columns(3)
with col1:
    future_price = st.number_input("Nifty Future LTP", min_value=15000.0, max_value=35000.0, value=24800.0, step=10.0)
with col2:
    strike_selected = st.number_input("ATM Strike", min_value=15000, max_value=35000, value=24800, step=50)
with col3:
    existing_hedge = st.number_input("Existing Futures Hedged Qty", value=0, step=int(lot_size))

st.subheader("📊 Market Feed (LTP + Entry Input)")
m1, m2, m3, m4 = st.columns(4)
with m1:
    ce_entry = st.number_input("CE Entry Price", value=125.0, step=0.5)
with m2:
    ce_ltp = st.number_input(f"CE LTP ({strike_selected}CE)", value=125.0, step=0.5)
with m3:
    pe_entry = st.number_input("PE Entry Price", value=120.0, step=0.5)
with m4:
    pe_ltp = st.number_input(f"PE LTP ({strike_selected}PE)", value=120.0, step=0.5)

legs = [
    PositionLeg(f"NIFTY{strike_selected}CE", strike_selected, "CE", -int(lot_size * 2), ce_entry),
    PositionLeg(f"NIFTY{strike_selected}PE", strike_selected, "PE", -int(lot_size * 2), pe_entry)
]
ticks = {f"NIFTY{strike_selected}CE": ce_ltp, f"NIFTY{strike_selected}PE": pe_ltp}

engine = DeltaEngine(int(lot_size), delta_threshold, r_rate)
dte_y = max(days_to_expiry / 365.25, 1e-5)
net_delta, net_pnl, metrics = engine.calculate_state(legs, future_price, ticks, dte_y, int(existing_hedge))

st.divider()
kpi1, kpi2, kpi3, kpi4 = st.columns(4)
kpi1.metric("Net Portfolio Delta", f"{net_delta:.2f}")
kpi2.metric("Tolerance Limit", f"±{delta_threshold * lot_size:.1f}")
kpi3.metric("Net Unrealized PnL", f"₹{net_pnl:,.2f}")

# शुद्ध और सटीक हेज ऑर्डर लॉजिक:
# चूंकि net_delta में existing_hedge पहले से जुड़ा हुआ है, डेल्टा को 0 करने के लिए
# सीधा अतिरिक्त ऑर्डर = -round(net_delta / lot_size) * lot_size होगा
order_qty = -int(round(net_delta / lot_size) * lot_size)
kpi4.metric("Additional Hedge Order", f"{order_qty} Qty")

st.subheader("📑 Active Legs Breakdown")
st.dataframe(metrics, use_container_width=True)

if abs(net_delta) > (delta_threshold * lot_size):
    action = "BUY" if order_qty > 0 else "SELL"
    st.error(f"🚨 **REBALANCE TRIGGERED:** Net Delta ({net_delta:.2f}) breached tolerance band. Send **{action} {abs(order_qty)} Qty** Nifty Futures.")
else:
    st.success("✅ **DELTA NEUTRAL:** Portfolio remains safely within the designated delta band.")
