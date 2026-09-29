import logging
from datetime import datetime, timedelta
import numpy as np
import pandas as pd
import requests
import streamlit as st

logging.basicConfig(level=logging.INFO)

st.set_page_config(page_title="Institutional Strategy Backtester", layout="wide")
st.title("🏛️ Institutional EMA 36/72 Engine (Next-Open Execution & Risk Analytics)")

TOKEN = st.secrets.get("UPSTOX_TOKEN", "")
HEADERS = {
    "Authorization": f"Bearer {TOKEN.strip()}",
    "Accept": "application/json"
}

@st.cache_data(ttl=86400)
def get_keys():
    try:
        url = "https://assets.upstox.com/market-quote/instruments/exchange/NSE.json.gz"
        inst = pd.read_json(url)
        tc = "trading_symbol" if "trading_symbol" in inst.columns else "tradingsymbol"
        inst = inst[inst["instrument_key"].str.contains("NSE_EQ", na=False)]
        inst = inst[inst["instrument_type"] == "EQ"]
        return dict(zip(inst[tc], inst["instrument_key"]))
    except Exception as e:
        st.error(f"Instruments load error: {e}")
        return {}

keys = get_keys()

# --- Sidebar Inputs ---
st.sidebar.header("⚙️ Configuration")
symbol = st.sidebar.text_input("NSE Trading Symbol", "SBIN").upper().strip()
timeframe = st.sidebar.selectbox("Timeframe", ["30minute", "15minute", "day"], index=0)
days = st.sidebar.slider("Historical Range (Days)", 30, 365, 120)

st.sidebar.subheader("Risk & Friction (Realistic Setup)")
use_trend_filter = st.sidebar.checkbox("Filter with 200 EMA (Trend Only)", value=True)
target_pct = st.sidebar.number_input("Target Profit %", min_value=0.5, max_value=20.0, value=3.0, step=0.5)
sl_pct = st.sidebar.number_input("Stop Loss %", min_value=0.2, max_value=10.0, value=1.5, step=0.2)
slippage_pct = st.sidebar.number_input("Slippage + Brokerage per Trade %", min_value=0.0, max_value=1.0, value=0.08, step=0.02)

def fetch_data(ikey, tf, days_back):
    to_date = datetime.now().strftime("%Y-%m-%d")
    from_date = (datetime.now() - timedelta(days=days_back)).strftime("%Y-%m-%d")

    # Correct Upstox API v2 date route: {to_date}/{from_date}
    url = f"https://api.upstox.com/v2/historical-candle/{ikey}/{tf}/{to_date}/{from_date}"
    res = requests.get(url, headers=HEADERS, timeout=15)
    
    if res.status_code != 200:
        return None, f"Upstox API Error: HTTP {res.status_code} ({res.text})"
        
    candles = res.json().get("data", {}).get("candles", [])
    if len(candles) < 210:
        return None, f"Insufficient candles ({len(candles)} found). 200 EMA stabilization ke liye minimum 210 candles chahiye."

    candles = list(reversed(candles))
    df = pd.DataFrame(candles, columns=["ts", "o", "h", "l", "c", "v", "oi"])
    for col in ["o", "h", "l", "c", "v"]:
        df[col] = df[col].astype(float)
    return df, None

if st.sidebar.button("🚀 Run Institutional Test", use_container_width=True):
    if symbol not in keys:
        st.error(f"'{symbol}' NSE list mein valid nahi mila.")
    elif not TOKEN:
        st.error("Secrets mein UPSTOX_TOKEN missing hai.")
    else:
        with st.spinner("Fetching data and running tick-simulated execution..."):
            ikey = keys[symbol]
            df, err = fetch_data(ikey, timeframe, days)

        if err:
            st.error(err)
        else:
            # Indicator Seeding
            df["ema36"] = df["c"].ewm(span=36, adjust=False).mean()
            df["ema72"] = df["c"].ewm(span=72, adjust=False).mean()
            df["ema200"] = df["c"].ewm(span=200, adjust=False).mean()

            trades = []
            pos = None
            friction = slippage_pct / 100.0

            # Realistic Bar-by-Bar Simulation (Starts from index 200 for full EMA warmup)
            for i in range(200, len(df) - 1):
                prev_diff = df["ema36"].iloc[i-1] - df["ema72"].iloc[i-1]
                curr_diff = df["ema36"].iloc[i] - df["ema72"].iloc[i]
                c_close = df["c"].iloc[i]
                c_ema200 = df["ema200"].iloc[i]

                # --- 1. Manage Active Position (Intrabay Wick Resolution on i-th bar) ---
                if pos is not None:
                    high = df["h"].iloc[i]
                    low = df["l"].iloc[i]
                    open_p = df["o"].iloc[i]
                    exit_price = None
                    reason = None

                    if pos["type"] == "LONG":
                        # Check Stop Loss first (Conservative institutional approach)
                        if low <= pos["sl_price"]:
                            exit_price = min(open_p, pos["sl_price"])
                            reason = "Stop Loss"
                        elif high >= pos["target_price"]:
                            exit_price = max(open_p, pos["target_price"])
                            reason = "Target"
                        elif prev_diff >= 0 and curr_diff < 0:
                            # Reversal Cross on candle close
                            exit_price = c_close
                            reason = "Reverse Signal"

                    elif pos["type"] == "SHORT":
                        if high >= pos["sl_price"]:
                            exit_price = max(open_p, pos["sl_price"])
                            reason = "Stop Loss"
                        elif low <= pos["target_price"]:
                            exit_price = min(open_p, pos["target_price"])
                            reason = "Target"
                        elif prev_diff <= 0 and curr_diff > 0:
                            exit_price = c_close
                            reason = "Reverse Signal"

                    if exit_price:
                        # Friction deduction (Buy + Sell slippage)
                        if pos["type"] == "LONG":
                            net_ret = ((exit_price - pos["entry"]) / pos["entry"]) - friction
                        else:
                            net_ret = ((pos["entry"] - exit_price) / pos["entry"]) - friction

                        trades.append({
                            "Type": pos["type"],
                            "Entry Time": pos["time"],
                            "Exit Time": df["ts"].iloc[i],
                            "Entry Price": round(pos["entry"], 2),
                            "Exit Price": round(exit_price, 2),
                            "PnL %": round(net_ret * 100, 2),
                            "Reason": reason
                        })
                        pos = None

                # --- 2. Check for New Entry on Agli Candle ka Open (Next Bar Execution) ---
                if pos is None:
                    next_open = df["o"].iloc[i+1] # Agli candle open par order fill
                    next_time = df["ts"].iloc[i+1]

                    bullish_cross = (prev_diff <= 0 and curr_diff > 0)
                    bearish_cross = (prev_diff >= 0 and curr_diff < 0)

                    if use_trend_filter:
                        bullish_cross = bullish_cross and (c_close > c_ema200)
                        bearish_cross = bearish_cross and (c_close < c_ema200)

                    if bullish_cross:
                        pos = {
                            "type": "LONG",
                            "entry": next_open,
                            "sl_price": next_open * (1.0 - (sl_pct / 100.0)),
                            "target_price": next_open * (1.0 + (target_pct / 100.0)),
                            "time": next_time
                        }
                    elif bearish_cross:
                        pos = {
                            "type": "SHORT",
                            "entry": next_open,
                            "sl_price": next_open * (1.0 + (sl_pct / 100.0)),
                            "target_price": next_open * (1.0 - (target_pct / 100.0)),
                            "time": next_time
                        }

            # --- 3. Institutional Quantitative Dashboard ---
            if trades:
                tdf = pd.DataFrame(trades)
                tdf["Cumulative_PnL"] = tdf["PnL %"].cumsum()
                
                # Drawdown Analysis
                equity_curve = (1 + tdf["PnL %"] / 100).cumprod()
                peak = equity_curve.cummax()
                drawdown = (equity_curve - peak) / peak
                max_drawdown = drawdown.min() * 100

                # Metrics
                wins = tdf[tdf["PnL %"] > 0]
                losses = tdf[tdf["PnL %"] <= 0]
                win_rate = (len(wins) / len(tdf)) * 100
                total_gain = wins["PnL %"].sum()
                total_loss = abs(losses["PnL %"].sum())
                profit_factor = (total_gain / total_loss) if total_loss > 0 else np.nan
                
                mean_pnl = tdf["PnL %"].mean()
                std_pnl = tdf["PnL %"].std()
                sharpe = (mean_pnl / std_pnl * np.sqrt(252)) if std_pnl > 0 else 0.0

                # KPI Displays
                kpi1, kpi2, kpi3, kpi4, kpi5 = st.columns(5)
                kpi1.metric("Total Trades", len(tdf))
                kpi2.metric("Win Rate", f"{win_rate:.1f}%")
                kpi3.metric("Net Return (After Fees)", f"{tdf['PnL %'].sum():.2f}%")
                kpi4.metric("Profit Factor", f"{profit_factor:.2f}" if not np.isnan(profit_factor) else "Inf")
                kpi5.metric("Max Drawdown (MDD)", f"{max_drawdown:.2f}%")

                # Charts
                st.subheader("📈 Institutional Equity Curve (Net Realized Returns)")
                st.line_chart(tdf.set_index("Exit Time")["Cumulative_PnL"])

                # Detailed Ledger
                st.subheader("📋 Trade Log Details")
                st.dataframe(tdf, use_container_width=True, hide_index=True)
            else:
                st.warning("Diye gaye parameters aur timeframe par koi valid trading signal nahi bana.")
                  
