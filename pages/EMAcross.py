import logging
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
import pandas as pd
import requests
import streamlit as st

logging.basicConfig(level=logging.INFO)

st.set_page_config(page_title="Institutional EMA Scanner", layout="wide")
st.title("⚡ Institutional EMA 36/72 Crossover + Volume Engine")

TOKEN = st.secrets.get("UPSTOX_TOKEN", "")
TG_TOKEN = st.secrets.get("TELEGRAM_TOKEN", "")
TG_CHAT = st.secrets.get("TELEGRAM_CHAT_ID", "")

HEADERS = {
    "Authorization": f"Bearer {TOKEN.strip()}",
    "Accept": "application/json"
}

WATCHLIST = [
    "SBICARD", "HDFCBANK", "ICICIBANK", "SBIN", "AXISBANK",
    "RELIANCE", "TATAMOTORS", "INFY", "TCS"
]

def send_tg(msg: str):
    if not TG_TOKEN or not TG_CHAT:
        return
    try:
        url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
        requests.post(url, json={"chat_id": TG_CHAT, "text": msg, "parse_mode": "Markdown"}, timeout=5)
    except Exception as e:
        logging.error(f"Telegram alert failed: {e}")

@st.cache_data(ttl=86400)
def get_keys():
    try:
        url = "https://assets.upstox.com/market-quote/instruments/exchange/NSE.json.gz"
        inst = pd.read_json(url)
        tc = "trading_symbol" if "trading_symbol" in inst.columns else "tradingsymbol"
        inst = inst[inst["instrument_key"].str.contains("NSE_EQ", na=False)]
        d = dict(zip(inst[tc], inst["instrument_key"]))
        return {s: d[s] for s in WATCHLIST if s in d}
    except Exception as e:
        st.error(f"Instruments file fetch fail hui: {e}")
        return {}

def fetch_candles(ikey: str, tf: str):
    to_date = datetime.now().strftime("%Y-%m-%d")
    days_back = 10 if tf == "5m" else 20
    from_date = (datetime.now() - timedelta(days=days_back)).strftime("%Y-%m-%d")
    
    interval = "minutes/15" if tf == "15m" else "minutes/5"

    # DIRECT unencoded instrument key use karo (Upstox endpoint requirement)
    url = f"https://api.upstox.com/v2/historical-candle/{ikey}/{interval}/{to_date}/{from_date}"
    
    try:
        res = requests.get(url, headers=HEADERS, timeout=10)
        if res.status_code != 200:
            return None, f"HTTP {res.status_code}: {res.text}"

        data = res.json().get("data", {}).get("candles", [])
        if len(data) < 75:
            return None, f"Insufficient candles ({len(data)} mile)"

        data = list(reversed(data))
        df = pd.DataFrame(data, columns=["ts", "o", "h", "l", "c", "v", "oi"])
        for col in ["o", "h", "l", "c", "v"]:
            df[col] = df[col].astype(float)
        return df, None
    except Exception as e:
        return None, str(e)

def analyze_institutional_signal(args):
    sym, ikey, tf = args

    df, err = fetch_candles(ikey, tf)
    if df is None:
        return {"Symbol": sym, "Error": err}

    try:
        df["ema36"] = df["c"].ewm(span=36, adjust=False).mean()
        df["ema72"] = df["c"].ewm(span=72, adjust=False).mean()
        df["ema200"] = df["c"].ewm(span=min(len(df), 200), adjust=False).mean()
        df["vol_sma"] = df["v"].rolling(window=20).mean()

        now_time = datetime.now().time()
        market_open = datetime.strptime("09:15", "%H:%M").time()
        market_close = datetime.strptime("15:30", "%H:%M").time()

        if market_open <= now_time <= market_close:
            curr_idx, prev_idx = -2, -3
        else:
            curr_idx, prev_idx = -1, -2

        prev = df.iloc[prev_idx]
        curr = df.iloc[curr_idx]

        prev_diff = prev["ema36"] - prev["ema72"]
        curr_diff = curr["ema36"] - curr["ema72"]

        volume_confirmed = curr["v"] > (curr["vol_sma"] * 1.2) if not pd.isna(curr["vol_sma"]) else False

        signal = "NONE"
        quality = "Neutral"

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
            "Candle Time": str(curr["ts"])[:16],
            "Error": None
        }
    except Exception as e:
        return {"Symbol": sym, "Error": str(e)}

# --- UI Controls ---
col1, col2 = st.columns([3, 1])
with col1:
    tf = st.selectbox("Select Timeframe", ["15m", "5m"], index=0)
with col2:
    st.write("")
    st.write("")
    scan_btn = st.button("🔄 Run Scanner", use_container_width=True)

keys = get_keys()

if scan_btn or "app_started" not in st.session_state:
    st.session_state["app_started"] = True

    if not TOKEN:
        st.error("⚠️ Streamlit Secrets mein `UPSTOX_TOKEN` missing hai.")
    elif not keys:
        st.error("⚠️ Watchlist ke instruments fetch nahi ho paye.")
    else:
        with st.spinner("Candles fetch aur analyze ho rahi hain..."):
            with ThreadPoolExecutor(max_workers=5) as executor:
                tasks = [(sym, key, tf) for sym, key in keys.items()]
                results = list(executor.map(analyze_institutional_signal, tasks))

        valid_results = [r for r in results if r and r.get("Error") is None]
        errors = [r for r in results if r and r.get("Error")]

        if valid_results:
            res_df = pd.DataFrame(valid_results)
            signals_df = res_df[res_df["Signal"] != "NONE"]

            if not signals_df.empty:
                st.subheader("🎯 Active Crossover Signals")
                st.dataframe(signals_df, use_container_width=True, hide_index=True)

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
                st.info("ℹ️ Abhi watchlist mein koi fresh 36/72 crossover trigger nahi hua hai.")

            with st.expander("📊 Complete Watchlist Status", expanded=True):
                st.dataframe(res_df.sort_values("Symbol"), use_container_width=True, hide_index=True)
        else:
            st.error("Data fetch nahi hua. Niche Upstox ka exact response dekhein:")
            for err in errors[:3]:
                st.code(f"{err['Symbol']}: {err['Error']}")
