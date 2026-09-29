import logging
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
import pandas as pd
import requests
import streamlit as st

logging.basicConfig(level=logging.INFO)

st.set_page_config(page_title="Institutional EMA Scanner - Full NSE", layout="wide")
st.title("⚡ Institutional EMA 36/72 Crossover + Volume Engine (All NSE)")

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

@st.cache_data(ttl=86400)
def get_all_nse_keys():
    try:
        url = "https://assets.upstox.com/market-quote/instruments/exchange/NSE.json.gz"
        inst = pd.read_json(url)
        tc = "trading_symbol" if "trading_symbol" in inst.columns else "tradingsymbol"
        # Sirf standard active NSE equity shares filter karo
        inst = inst[inst["instrument_key"].str.contains("NSE_EQ", na=False)]
        inst = inst[inst["instrument_type"] == "EQ"]
        inst = inst.dropna(subset=[tc, "instrument_key"])
        return dict(zip(inst[tc], inst["instrument_key"]))
    except Exception as e:
        st.error(f"NSE instruments file download fail hui: {e}")
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

        if len(data) < 20:
            return None, "Insufficient raw candles"

        data = list(reversed(data))
        df = pd.DataFrame(data, columns=["ts", "o", "h", "l", "c", "v", "oi"])
        df["ts"] = pd.to_datetime(df["ts"])
        for col in ["o", "h", "l", "c", "v"]:
            df[col] = df[col].astype(float)

        # Timeframe Resampling
        df.set_index("ts", inplace=True)
        rule = "15min" if tf == "15m" else "5min"
        resampled_df = df.resample(rule).agg({
            "o": "first",
            "h": "max",
            "l": "min",
            "c": "last",
            "v": "sum"
        }).dropna().reset_index()

        # 15m me max 25 candles banti hain, isliye limit 15 rakhi hai
        min_required = 15 if tf == "15m" else 30
        if len(resampled_df) < min_required:
            return None, "Insufficient resampled candles"

        return resampled_df, None
    except Exception as e:
        return None, str(e)

def analyze_institutional_signal(args):
    sym, ikey, tf = args

    df, err = fetch_candles(ikey, tf)
    if df is None:
        return None

    try:
        df["ema36"] = df["c"].ewm(span=36, adjust=False).mean()
        df["ema72"] = df["c"].ewm(span=72, adjust=False).mean()
        df["ema200"] = df["c"].ewm(span=min(len(df), 200), adjust=False).mean()
        df["vol_sma"] = df["v"].rolling(window=10).mean()

        curr = df.iloc[-2]
        prev = df.iloc[-3]

        prev_diff = prev["ema36"] - prev["ema72"]
        curr_diff = curr["ema36"] - curr["ema72"]

        volume_confirmed = curr["v"] > (curr["vol_sma"] * 1.2) if not pd.isna(curr["vol_sma"]) else False

        signal = "NONE"
        quality = "Neutral"

        # Bullish Crossover (36 crosses above 72)
        if prev_diff <= 0 and curr_diff > 0:
            if curr["c"] > curr["ema200"] and volume_confirmed:
                signal = "🟢 HIGH-CONVICTION BULLISH"
                quality = "A+ (Volume + Trend Aligned)"
            elif curr["c"] > curr["ema200"]:
                signal = "🟢 BULLISH"
                quality = "Moderate (Low Volume)"
            else:
                signal = "⚠️ COUNTER-TREND BULLISH"
                quality = "Weak (Below 200 EMA)"

        # Bearish Crossover (36 crosses below 72)
        elif prev_diff >= 0 and curr_diff < 0:
            if curr["c"] < curr["ema200"] and volume_confirmed:
                signal = "🔴 HIGH-CONVICTION BEARISH"
                quality = "A+ (Volume + Trend Aligned)"
            elif curr["c"] < curr["ema200"]:
                signal = "🔴 BEARISH"
                quality = "Moderate (Low Volume)"
            else:
                signal = "⚠️ COUNTER-TREND BEARISH"
                quality = "Weak (Above 200 EMA)"

        return {
            "Symbol": sym,
            "Price": round(curr["c"], 2),
            "EMA 36": round(curr["ema36"], 2),
            "EMA 72": round(curr["ema72"], 2),
            "EMA 200": round(curr["ema200"], 2),
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
                           help="Zyada stocks scan karne par Upstox rate-limits se bachne ke liye limit set karein")
with col3:
    st.write("")
    st.write("")
    scan_btn = st.button("🔄 Scan Market", use_container_width=True)

all_nse_keys = get_all_nse_keys()

if scan_btn or "app_started" not in st.session_state:
    st.session_state["app_started"] = True

    if not TOKEN:
        st.error("⚠️ Streamlit Secrets mein `UPSTOX_TOKEN` enter karein.")
    elif not all_nse_keys:
        st.error("⚠️ NSE instruments list load nahi hui.")
    else:
        # User defined limit ke mutabiq NSE stocks select karo
        selected_symbols = list(all_nse_keys.keys())[:scan_limit]
        st.caption(f"Scanning total **{len(selected_symbols)}** active NSE stocks on **{tf}** timeframe...")

        with st.spinner("Market scan in progress..."):
            with ThreadPoolExecutor(max_workers=8) as executor:
                tasks = [(sym, all_nse_keys[sym], tf) for sym in selected_symbols]
                results = list(executor.map(analyze_institutional_signal, tasks))

        valid_results = [r for r in results if r is not None]

        if valid_results:
            res_df = pd.DataFrame(valid_results)
            signals_df = res_df[res_df["Signal"] != "NONE"]

            if not signals_df.empty:
                st.subheader(f"🎯 Active Crossover Signals ({len(signals_df)} found)")
                st.dataframe(signals_df.sort_values(by="Signal", ascending=False), use_container_width=True, hide_index=True)

                for _, row in signals_df.iterrows():
                    if "HIGH-CONVICTION" in row["Signal"]:
                        alert_id = f"tg_{row['Symbol']}_{row['Candle Time']}"
                        if alert_id not in st.session_state:
                            msg = (
                                f"🔥 *INSTITUTIONAL SIGNAL*\n"
                                f"*{row['Symbol']}* ({tf})\n"
                                f"Signal: {row['Signal']}\n"
                                f"Price: ₹{row['Price']} | 200 EMA: ₹{row['EMA 200']}\n"
                                f"Volume Confirmed: {row['Volume Surge']}"
                            )
                            send_tg(msg)
                            st.session_state[alert_id] = True
            else:
                st.info("ℹ️ Is batch mein abhi koi fresh 36/72 crossover trigger nahi hua hai.")

            with st.expander(f"📊 Scanned Stocks Overview ({len(res_df)} stocks)"):
                st.dataframe(res_df.sort_values("Symbol"), use_container_width=True, hide_index=True)
        else:
            st.warning("Koi candle data process nahi ho saka. Market timings ya token check karein.")
