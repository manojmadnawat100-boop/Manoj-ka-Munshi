import logging
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
import numpy as np
import pandas as pd
import requests
import streamlit as st

logging.basicConfig(level=logging.INFO)

st.set_page_config(page_title="Institutional EMA + ADX Scanner", layout="wide")
st.title("⚡ Institutional EMA 36/72 Crossover + ADX Engine (All NSE)")

TOKEN = st.secrets.get("UPSTOX_TOKEN", "")
TG_TOKEN = st.secrets.get("TELEGRAM_TOKEN", "")
TG_CHAT = st.secrets.get("TELEGRAM_CHAT_ID", "")

HEADERS = {
    "Authorization": f"Bearer {TOKEN.strip()}",
    "Accept": "application/json"
}

def send_tg(msg: str):
    if not TG_TOKEN or not TG_CHAT:
        return
    try:
        url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
        requests.post(url, json={"chat_id": TG_CHAT, "text": msg, "parse_mode": "Markdown"}, timeout=5)
    except Exception as e:
        logging.error(f"Telegram alert failed: {e}")

def calculate_adx_vectorized(df: pd.DataFrame, period: int = 14) -> pd.Series:
    """High-speed vectorized ADX calculation using NumPy (No row-wise slow loops)"""
    high = df['h'].values
    low = df['l'].values
    close = df['c'].values
    
    # True Range calculation
    hl = high - low
    h_pc = np.abs(high[1:] - close[:-1])
    l_pc = np.abs(low[1:] - close[:-1])
    
    tr = np.zeros(len(df))
    tr[0] = hl[0]
    tr[1:] = np.maximum(hl[1:], np.maximum(h_pc, l_pc))
    
    # Directional Movement
    up = np.zeros(len(df))
    down = np.zeros(len(df))
    up[1:] = high[1:] - high[:-1]
    down[1:] = low[:-1] - low[1:]
    
    plus_dm = np.where((up > down) & (up > 0), up, 0.0)
    minus_dm = np.where((down > up) & (down > 0), down, 0.0)
    
    # Wilder's Smoothing
    tr_s = pd.Series(tr).ewm(alpha=1/period, adjust=False).mean()
    p_dm_s = pd.Series(plus_dm).ewm(alpha=1/period, adjust=False).mean()
    m_dm_s = pd.Series(minus_dm).ewm(alpha=1/period, adjust=False).mean()
    
    plus_di = 100 * (p_dm_s / tr_s.replace(0, np.nan))
    minus_di = 100 * (m_dm_s / tr_s.replace(0, np.nan))
    
    di_sum = (plus_di + minus_di).replace(0, np.nan)
    dx = 100 * (np.abs(plus_di - minus_di) / di_sum)
    adx = dx.ewm(alpha=1/period, adjust=False).mean()
    return adx.fillna(0)

@st.cache_data(ttl=86400)
def get_all_nse_keys():
    try:
        url = "https://assets.upstox.com/market-quote/instruments/exchange/NSE.json.gz"
        inst = pd.read_json(url)
        tc = "trading_symbol" if "trading_symbol" in inst.columns else "tradingsymbol"
        inst = inst[inst["instrument_key"].str.contains("NSE_EQ", na=False)]
        inst = inst[inst["instrument_type"] == "EQ"]
        inst = inst.dropna(subset=[tc, "instrument_key"])
        return dict(zip(inst[tc], inst["instrument_key"]))
    except Exception as e:
        st.error(f"Instruments file load error: {e}")
        return {}

def fetch_candles(ikey: str, tf: str):
    url = f"https://api.upstox.com/v2/historical-candle/intraday/{ikey}/1minute"
    try:
        res = requests.get(url, headers=HEADERS, timeout=8)
        data = []
        if res.status_code == 200:
            data = res.json().get("data", {}).get("candles", [])

        if not data:
            to_date = datetime.now().strftime("%Y-%m-%d")
            url_fallback = f"https://api.upstox.com/v2/historical-candle/{ikey}/30minute/{to_date}"
            res = requests.get(url_fallback, headers=HEADERS, timeout=8)
            if res.status_code == 200:
                data = res.json().get("data", {}).get("candles", [])

        if len(data) < 25:
            return None

        data = list(reversed(data))
        df = pd.DataFrame(data, columns=["ts", "o", "h", "l", "c", "v", "oi"])
        df["ts"] = pd.to_datetime(df["ts"])
        for col in ["o", "h", "l", "c", "v"]:
            df[col] = df[col].astype(float)

        df.set_index("ts", inplace=True)
        rule = "15min" if tf == "15m" else "5min"
        resampled_df = df.resample(rule).agg({
            "o": "first",
            "h": "max",
            "l": "min",
            "c": "last",
            "v": "sum"
        }).dropna().reset_index()

        min_required = 15 if tf == "15m" else 30
        if len(resampled_df) < min_required:
            return None

        return resampled_df
    except Exception:
        return None

def analyze_stock(args):
    sym, ikey, tf = args
    df = fetch_candles(ikey, tf)
    if df is None or len(df) < 20:
        return None

    try:
        df["ema36"] = df["c"].ewm(span=36, adjust=False).mean()
        df["ema72"] = df["c"].ewm(span=72, adjust=False).mean()
        df["ema200"] = df["c"].ewm(span=min(len(df), 200), adjust=False).mean()
        df["adx"] = calculate_adx_vectorized(df, period=14)
        df["vol_sma"] = df["v"].rolling(window=10).mean()

        curr = df.iloc[-2]
        prev = df.iloc[-3]

        prev_diff = prev["ema36"] - prev["ema72"]
        curr_diff = curr["ema36"] - curr["ema72"]

        is_trending = curr["adx"] >= 20.0
        volume_confirmed = curr["v"] > (curr["vol_sma"] * 1.2) if not pd.isna(curr["vol_sma"]) else False

        signal = "NONE"
        quality = "Neutral"

        # Bullish Crossover (EMA 36 crosses above EMA 72)
        if prev_diff <= 0 and curr_diff > 0:
            if curr["c"] > curr["ema200"] and is_trending and volume_confirmed:
                signal = "🟢 HIGH-CONVICTION BULLISH"
                quality = "A+ (Trend + ADX > 20 + Vol)"
            elif curr["c"] > curr["ema200"] and is_trending:
                signal = "🟢 BULLISH (Trend + ADX)"
                quality = "Good (Low Volume)"
            else:
                signal = "⚠️ WEAK BULLISH"
                quality = "Choppy Market (ADX < 20 / Sub-200)"

        # Bearish Crossover (EMA 36 crosses below EMA 72)
        elif prev_diff >= 0 and curr_diff < 0:
            if curr["c"] < curr["ema200"] and is_trending and volume_confirmed:
                signal = "🔴 HIGH-CONVICTION BEARISH"
                quality = "A+ (Trend + ADX > 20 + Vol)"
            elif curr["c"] < curr["ema200"] and is_trending:
                signal = "🔴 BEARISH (Trend + ADX)"
                quality = "Good (Low Volume)"
            else:
                signal = "⚠️ WEAK BEARISH"
                quality = "Choppy Market (ADX < 20 / Above-200)"

        return {
            "Symbol": sym,
            "Price": round(curr["c"], 2),
            "EMA 36": round(curr["ema36"], 2),
            "EMA 72": round(curr["ema72"], 2),
            "ADX": round(curr["adx"], 1),
            "Volume Surge": "✅ Yes" if volume_confirmed else "❌ No",
            "Signal": signal,
            "Quality": quality,
            "Candle Time": str(curr["ts"])[:16]
        }
    except Exception:
        return None

# --- UI Controls ---
col1, col2, col3 = st.columns([2, 2, 1])
with col1:
    tf = st.selectbox("Select Timeframe", ["15m", "5m"], index=0)
with col2:
    scan_limit = st.slider("Scan Stocks Limit (Batching)", min_value=50, max_value=2000, value=200, step=50,
                           help="Zyada stocks scan karne par Upstox rate limits se bachne ke liye limit set karein")
with col3:
    st.write("")
    st.write("")
    scan_btn = st.button("🔄 Scan Market", use_container_width=True)

all_nse_keys = get_all_nse_keys()

if scan_btn or "app_started" not in st.session_state:
    st.session_state["app_started"] = True

    if not TOKEN:
        st.error("⚠️️ Streamlit Secrets mein `UPSTOX_TOKEN` enter karein.")
    elif not all_nse_keys:
        st.error("⚠️ NSE instruments list load nahi ho saki.")
    else:
        selected_symbols = list(all_nse_keys.keys())[:scan_limit]
        st.caption(f"Scanning **{len(selected_symbols)}** active NSE stocks on **{tf}** timeframe...")

        with st.spinner("Market scan in progress..."):
            with ThreadPoolExecutor(max_workers=8) as executor:
                tasks = [(sym, all_nse_keys[sym], tf) for sym in selected_symbols]
                results = list(executor.map(analyze_stock, tasks))

        valid_results = [r for r in results if r is not None]

        if valid_results:
            res_df = pd.DataFrame(valid_results)
            signals_df = res_df[res_df["Signal"] != "NONE"]

            if not signals_df.empty:
                st.subheader(f"🎯 Confirmed Crossover Signals ({len(signals_df)} found)")
                st.dataframe(signals_df.sort_values(by="Signal", ascending=False), use_container_width=True, hide_index=True)

                for _, row in signals_df.iterrows():
                    if "HIGH-CONVICTION" in row["Signal"]:
                        alert_id = f"tg_{row['Symbol']}_{row['Candle Time']}"
                        if alert_id not in st.session_state:
                            msg = (
                                f"🔥 *INSTITUTIONAL SIGNAL*\n"
                                f"*{row['Symbol']}* ({tf})\n"
                                f"Signal: {row['Signal']}\n"
                                f"Price: ₹{row['Price']} | ADX: {row['ADX']}\n"
                                f"Quality: {row['Quality']}"
                            )
                            send_tg(msg)
                            st.session_state[alert_id] = True
            else:
                st.info("ℹ️ Is batch mein koi fresh trend-confirmed 36/72 crossover nahi mila.")

            with st.expander(f"📊 Scanned Stocks Overview ({len(res_df)} stocks)"):
                st.dataframe(res_df.sort_values("Symbol"), use_container_width=True, hide_index=True)
        else:
            st.warning("Koi candle data process nahi ho saka. Market timings ya token check karein.")
            
