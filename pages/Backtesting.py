import logging
import time
from datetime import datetime, timedelta
import numpy as np
import pandas as pd
import requests
import streamlit as st

logging.basicConfig(level=logging.INFO)

st.set_page_config(page_title="Institutional Strategy Backtester", layout="wide")
st.title("🏛️ Institutional EMA 36/72 Backtester")

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
        st.error(f"Instruments file load error: {e}")
        return {}

keys = get_keys()

# --- Main Controls ---
c1, c2, c3 = st.columns([2, 1, 1])
with c1:
    symbol = st.text_input("NSE Stock Symbol", "SBIN").upper().strip()
with c2:
    timeframe = st.selectbox("Timeframe", ["30minute", "15minute", "day"], index=0)
with c3:
    days = st.slider("Lookback Days", 30, 180, 90)

with st.expander("⚙️ Risk, SL/Target & Friction Settings", expanded=False):
    r1, r2, r3, r4 = st.columns(4)
    with r1:
        use_trend_filter = st.checkbox("200 EMA Filter (Trend Only)", value=True)
    with r2:
        target_pct = st.number_input("Target Profit %", min_value=0.5, max_value=20.0, value=3.0, step=0.5)
    with r3:
        sl_pct = st.number_input("Stop Loss %", min_value=0.2, max_value=10.0, value=1.5, step=0.2)
    with r4:
        slippage_pct = st.number_input("Brokerage + Slippage %", min_value=0.0, max_value=1.0, value=0.08, step=0.02)

run_btn = st.button("🚀 Run Institutional Test", use_container_width=True)

@st.cache_data(ttl=600, show_spinner=False)
def fetch_data_chunked(ikey, tf, total_days):
    # 1. DAILY TIMEFRAME: Direct historical endpoint with minimum 365 days
    if tf == "day":
        to_str = datetime.now().strftime("%Y-%m-%d")
        lookback = max(total_days, 365)
        from_str = (datetime.now() - timedelta(days=lookback)).strftime("%Y-%m-%d")
        
        url = f"https://api.upstox.com/v2/historical-candle/{ikey}/day/{to_str}/{from_str}"
        res = requests.get(url, headers=HEADERS, timeout=12)
        if res.status_code != 200:
            return None, f"Upstox Day Error: {res.text}"
            
        candles = res.json().get("data", {}).get("candles", [])
        if len(candles) < 75:
            return None, f"Daily candles kam hain ({len(candles)} candles mile). Lookback badhayein."
            
        df = pd.DataFrame(list(reversed(candles)), columns=["ts", "o", "h", "l", "c", "v", "oi"])
        df["ts"] = pd.to_datetime(df["ts"])
        for col in ["o", "h", "l", "c", "v"]:
            df[col] = df[col].astype(float)
        return df, None

    # 2. INTRADAY TIMEFRAME: 30minute direct ya 15minute via 1minute resampling
    api_interval = "30minute" if tf == "30minute" else "1minute"
    chunk_days = 25 if api_interval == "30minute" else 6
    effective_days = min(total_days, 40) if tf == "15minute" else total_days

    all_candles = []
    current_to = datetime.now()
    end_date = current_to - timedelta(days=effective_days)

    while current_to > end_date:
        current_from = max(current_to - timedelta(days=chunk_days), end_date)
        to_str = current_to.strftime("%Y-%m-%d")
        from_str = current_from.strftime("%Y-%m-%d")

        url = f"https://api.upstox.com/v2/historical-candle/{ikey}/{api_interval}/{to_str}/{from_str}"
        
        success = False
        for attempt in range(3):
            res = requests.get(url, headers=HEADERS, timeout=12)
            if res.status_code == 200:
                chunk = res.json().get("data", {}).get("candles", [])
                if chunk:
                    all_candles.extend(chunk)
                success = True
                break
            elif res.status_code == 429:
                time.sleep(2.5 * (attempt + 1))
            else:
                return None, f"Upstox API Error (HTTP {res.status_code}): {res.text}"

        if not success:
            return None, "Rate limit hit ho gaya. Kripya 20 second baad try karein."

        time.sleep(0.35)
        current_to = current_from - timedelta(days=1)

    if not all_candles:
        return None, "Candle data nahi mila."

    df = pd.DataFrame(all_candles, columns=["ts", "o", "h", "l", "c", "v", "oi"])
    df.drop_duplicates(subset=["ts"], inplace=True)
    df["ts"] = pd.to_datetime(df["ts"])
    df.sort_values("ts", inplace=True)
    df.reset_index(drop=True, inplace=True)
    for col in ["o", "h", "l", "c", "v"]:
        df[col] = df[col].astype(float)

    # 15-minute timeframe resampling
    if tf == "15minute":
        df.set_index("ts", inplace=True)
        df = df.resample("15min").agg({
            "o": "first",
            "h": "max",
            "l": "min",
            "c": "last",
            "v": "sum"
        }).dropna().reset_index()

    if len(df) < 75:
        return None, f"Candles count kam mila ({len(df)} candles). Timeframe/Days adjust karein."

    return df, None

# Run Simulation
if run_btn:
    if not TOKEN:
        st.error("⚠️ Streamlit Secrets mein `UPSTOX_TOKEN` missing hai.")
    elif keys and symbol not in keys:
        st.warning(f"'{symbol}' NSE list mein nahi mila. (e.g. SBIN, RELIANCE, TCS)")
    elif keys:
        with st.spinner(f"{symbol} ({timeframe}) ka data load aur simulate ho raha hai..."):
            ikey = keys[symbol]
            df, err = fetch_data_chunked(ikey, timeframe, days)

        if err:
            st.error(err)
        else:
            # Indicator Calculations
            df["ema36"] = df["c"].ewm(span=36, adjust=False).mean()
            df["ema72"] = df["c"].ewm(span=72, adjust=False).mean()
            df["ema200"] = df["c"].ewm(span=min(len(df), 200), adjust=False).mean()

            trades = []
            pos = None
            friction = slippage_pct / 100.0
            start_index = min(72, len(df) - 2)

            for i in range(start_index, len(df) - 1):
                prev_diff = df["ema36"].iloc[i-1] - df["ema72"].iloc[i-1]
                curr_diff = df["ema36"].iloc[i] - df["ema72"].iloc[i]
                c_close = df["c"].iloc[i]
                c_ema200 = df["ema200"].iloc[i]

                # 1. Manage Active Position (Intrabar High/Low fill)
                if pos is not None:
                    high = df["h"].iloc[i]
                    low = df["l"].iloc[i]
                    open_p = df["o"].iloc[i]
                    exit_price = None
                    reason = None

                    if pos["type"] == "LONG":
                        if low <= pos["sl_price"]:
                            exit_price = min(open_p, pos["sl_price"])
                            reason = "Stop Loss"
                        elif high >= pos["target_price"]:
                            exit_price = max(open_p, pos["target_price"])
                            reason = "Target"
                        elif prev_diff >= 0 and curr_diff < 0:
                            exit_price = c_close
                            reason = "Reverse Cross"

                    elif pos["type"] == "SHORT":
                        if high >= pos["sl_price"]:
                            exit_price = max(open_p, pos["sl_price"])
                            reason = "Stop Loss"
                        elif low <= pos["target_price"]:
                            exit_price = min(open_p, pos["target_price"])
                            reason = "Target"
                        elif prev_diff <= 0 and curr_diff > 0:
                            exit_price = c_close
                            reason = "Reverse Cross"

                    if exit_price:
                        if pos["type"] == "LONG":
                            net_ret = ((exit_price - pos["entry"]) / pos["entry"]) - friction
                        else:
                            net_ret = ((pos["entry"] - exit_price) / pos["entry"]) - friction

                        trades.append({
                            "Type": pos["type"],
                            "Entry Time": str(pos["time"])[:16],
                            "Exit Time": str(df["ts"].iloc[i])[:16],
                            "Entry Price": round(pos["entry"], 2),
                            "Exit Price": round(exit_price, 2),
                            "PnL %": round(net_ret * 100, 2),
                            "Reason": reason
                        })
                        pos = None

                # 2. Next-Bar Open Execution
                if pos is None:
                    next_open = df["o"].iloc[i+1]
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

            # 3. Output Analytics Dashboard
            if trades:
                tdf = pd.DataFrame(trades)
                tdf["Cumulative_PnL"] = tdf["PnL %"].cumsum()

                equity_curve = (1 + tdf["PnL %"] / 100).cumprod()
                peak = equity_curve.cummax()
                drawdown = (equity_curve - peak) / peak
                max_drawdown = drawdown.min() * 100

                wins = tdf[tdf["PnL %"] > 0]
                losses = tdf[tdf["PnL %"] <= 0]
                win_rate = (len(wins) / len(tdf)) * 100
                total_gain = wins["PnL %"].sum()
                total_loss = abs(losses["PnL %"].sum())
                profit_factor = (total_gain / total_loss) if total_loss > 0 else np.nan

                m1, m2, m3, m4, m5 = st.columns(5)
                m1.metric("Total Trades", len(tdf))
                m2.metric("Win Rate", f"{win_rate:.1f}%")
                m3.metric("Net Return", f"{tdf['PnL %'].sum():.2f}%")
                m4.metric("Profit Factor", f"{profit_factor:.2f}" if not np.isnan(profit_factor) else "N/A")
                m5.metric("Max Drawdown", f"{max_drawdown:.2f}%")

                st.subheader("📈 Cumulative P&L Curve (%)")
                st.line_chart(tdf.set_index("Exit Time")["Cumulative_PnL"])

                st.subheader("📋 Trade Log")
                st.dataframe(tdf, use_container_width=True, hide_index=True)
            else:
                st.info("Chune gaye time period mein koi valid 36/72 setup trigger nahi hua.")
else:
    st.info("👆 Stock symbol aur parameters select karke **Run Institutional Test** par click karein.")
