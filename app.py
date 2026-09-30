import logging
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
import numpy as np
import pandas as pd
import requests
import streamlit as st

logging.basicConfig(level=logging.INFO)

st.set_page_config(page_title="Ultra Institutional Scanner", layout="wide")
st.title("⚡ Ultra Institutional EMA 36/72 + VWAP + RSI + ATR Engine")

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
        logging.error(f"Telegram failed: {e}")

def calculate_adx_vectorized(df: pd.DataFrame, period: int = 14) -> pd.Series:
    high = df['h'].values
    low = df['l'].values
    close = df['c'].values
    
    hl = high - low
    h_pc = np.abs(high[1:] - close[:-1])
    l_pc = np.abs(low[1:] - close[:-1])
    
    tr = np.zeros(len(df))
    tr[0] = hl[0]
    tr[1:] = np.maximum(hl[1:], np.maximum(h_pc, l_pc))
    
    up = np.zeros(len(df))
    down = np.zeros(len(df))
    up[1:] = high[1:] - high[:-1]
    down[1:] = low[:-1] - low[1:]
    
    plus_dm = np.where((up > down) & (up > 0), up, 0.0)
    minus_dm = np.where((down > up) & (down > 0), down, 0.0)
    
    tr_s = pd.Series(tr).ewm(alpha=1/period, adjust=False).mean()
    p_dm_s = pd.Series(plus_dm).ewm(alpha=1/period, adjust=False).mean()
    m_dm_s = pd.Series(minus_dm).ewm(alpha=1/period, adjust=False).mean()
    
    plus_di = 100 * (p_dm_s / tr_s.replace(0, np.nan))
    minus_di = 100 * (m_dm_s / tr_s.replace(0, np.nan))
    
    di_sum = (plus_di + minus_di).replace(0, np.nan)
    dx = 100 * (np.abs(plus_di - minus_di) / di_sum)
    return dx.ewm(alpha=1/period, adjust=False).mean().fillna(0)

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
        st.error(f"Instruments file fetch fail: {e}")
        return {}

def fetch_candles(ikey: str, tf: str):
    url = f"https://api.upstox.com/v2/historical-candle/intraday/{ikey}/1minute"
    try:
        res = requests.get(url, headers=HEADERS, timeout=8)
        data = res.json().get("data", {}).get("candles", []) if res.status_code == 200 else []

        if not data:
            to_date = datetime.now().strftime("%Y-%m-%d")
            url2 = f"https://api.upstox.com/v2/historical-candle/{ikey}/30minute/{to_date}"
            res = requests.get(url2, headers=HEADERS, timeout=8)
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
            "o": "first", "h": "max", "l": "min", "c": "last", "v": "sum"
        }).dropna().reset_index()

        min_required = 15 if tf == "15m" else 30
        if len(resampled_df) < min_required:
            return None

        return resampled_df
    except Exception:
        return None

def analyze_institutional_signal(args):
    sym, ikey, tf = args
    df = fetch_candles(ikey, tf)
    if df is None or len(df) < 15:
        return None

    try:
        df["ema36"] = df["c"].ewm(span=36, adjust=False).mean()
        df["ema72"] = df["c"].ewm(span=72, adjust=False).mean()
        df["ema200"] = df["c"].ewm(span=min(len(df), 200), adjust=False).mean()
        df["vol_sma"] = df["v"].rolling(window=10).mean()
        df["adx"] = calculate_adx_vectorized(df)

        # 1. Day-Anchored Intraday VWAP
        typical_price = (df['h'] + df['l'] + df['c']) / 3
        df['date'] = df['ts'].dt.date
        df['pv'] = typical_price * df['v']
        df['vwap'] = df.groupby('date')['pv'].cumsum() / df.groupby('date')['v'].cumsum().replace(0, np.nan)

        # 2. Dynamic ATR Volatility
        tr1 = df['h'] - df['l']
        tr2 = (df['h'] - df['c'].shift(1)).abs()
        tr3 = (df['l'] - df['c'].shift(1)).abs()
        df['tr'] = np.maximum(tr1, np.maximum(tr2, tr3))
        df['atr'] = df['tr'].rolling(window=14).mean()

        # 3. Momentum RSI
        delta = df['c'].diff()
        gain = (delta.where(delta > 0, 0)).rolling(window=14).mean()
        loss = (-delta.where(delta < 0, 0)).rolling(window=14).mean()
        rs = gain / loss.replace(0, np.nan)
        df['rsi'] = 100 - (100 / (1 + rs))

        curr = df.iloc[-2]
        prev = df.iloc[-3]
        prev_diff = prev["ema36"] - prev["ema72"]
        curr_diff = curr["ema36"] - curr["ema72"]

        # Institutional Confluence Filters
        is_long_valid = (
            (prev_diff <= 0 and curr_diff > 0) and
            (curr['c'] > curr['ema200']) and
            (curr['c'] > curr['vwap']) and
            (curr['adx'] >= 20.0) and
            (55 <= curr['rsi'] <= 70) and
            (curr['v'] > curr['vol_sma'] * 1.3)
        )

        is_short_valid = (
            (prev_diff >= 0 and curr_diff < 0) and
            (curr['c'] < curr['ema200']) and
            (curr['c'] < curr['vwap']) and
            (curr['adx'] >= 20.0) and
            (30 <= curr['rsi'] <= 45) and
            (curr['v'] > curr['vol_sma'] * 1.3)
        )

        atr_val = curr['atr'] if not pd.isna(curr['atr']) else (curr['c'] * 0.01)

        signal = "NONE"
        quality = "Neutral"
        sl = 0.0
        tgt = 0.0

        if is_long_valid:
            signal = "🟢 ULTRA LONG"
            quality = f"A++ (RSI {round(curr['rsi'],1)} | ADX {round(curr['adx'],1)})"
            sl = round(curr['c'] - (1.5 * atr_val), 2)
            tgt = round(curr['c'] + (3.0 * atr_val), 2)
        elif is_short_valid:
            signal = "🔴 ULTRA SHORT"
            quality = f"A++ (RSI {round(curr['rsi'],1)} | ADX {round(curr['adx'],1)})"
            sl = round(curr['c'] + (1.5 * atr_val), 2)
            tgt = round(curr['c'] - (3.0 * atr_val), 2)
        elif prev_diff <= 0 and curr_diff > 0:
            signal = "⚠️ WEAK BULLISH"
            quality = f"Filter Reject (RSI {round(curr['rsi'],1)} | ADX {round(curr['adx'],1)})"
        elif prev_diff >= 0 and curr_diff < 0:
            signal = "⚠️ WEAK BEARISH"
            quality = f"Filter Reject (RSI {round(curr['rsi'],1)} | ADX {round(curr['adx'],1)})"

        return {
            "Symbol": sym,
            "Price": round(curr['c'], 2),
            "VWAP": round(curr['vwap'], 2),
            "RSI": round(curr['rsi'], 1) if not pd.isna(curr['rsi']) else 0,
            "ADX": round(curr['adx'], 1),
            "Signal": signal,
            "Quality": quality,
            "SL (1.5x ATR)": sl,
            "Target (3x ATR)": tgt,
            "Candle Time": str(curr["ts"])[:16]
        }
    except Exception:
        return None

# --- UI Controls ---
col1, col2, col3 = st.columns([2, 2, 1])
with col1:
    tf = st.selectbox("Select Timeframe", ["15m", "5m"], index=0)
with col2:
    scan_limit = st.slider("Scan Stocks Limit", min_value=50, max_value=2000, value=200, step=50)
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
        st.error("⚠️ NSE instruments list load nahi ho saki.")
    else:
        selected_symbols = list(all_nse_keys.keys())[:scan_limit]
        st.caption(f"Scanning **{len(selected_symbols)}** active NSE stocks on **{tf}** timeframe...")

        with st.spinner("Confluence metrics aur candlestick series calculate ho rahi hain..."):
            with ThreadPoolExecutor(max_workers=8) as executor:
                tasks = [(sym, all_nse_keys[sym], tf) for sym in selected_symbols]
                results = list(executor.map(analyze_institutional_signal, tasks))

        valid_results = [r for r in results if r is not None]

        if valid_results:
            res_df = pd.DataFrame(valid_results)
            signals_df = res_df[res_df["Signal"].str.contains("ULTRA")]

            if not signals_df.empty:
                st.subheader(f"🎯 Confirmed Ultra Institutional Setups ({len(signals_df)} found)")
                st.dataframe(signals_df.sort_values(by="Signal", ascending=False), use_container_width=True, hide_index=True)

                for _, row in signals_df.iterrows():
                    alert_id = f"tg_{row['Symbol']}_{row['Candle Time']}"
                    if alert_id not in st.session_state:
                        direction = "BUY" if "LONG" in row["Signal"] else "SELL"
                        msg = (
                            f"⚡ *ULTRA INSTITUTIONAL SETUP*\n"
                            f"*{row['Symbol']}* ({tf}) - {direction}\n"
                            f"Signal: {row['Signal']}\n"
                            f"Entry Price: ₹{row['Price']}\n"
                            f"VWAP: ₹{row['VWAP']} | RSI: {row['RSI']} | ADX: {row['ADX']}\n"
                            f"🛡️ Stop-Loss: ₹{row['SL (1.5x ATR)']}\n"
                            f"🎯 Target: ₹{row['Target (3x ATR)']}"
                        )
                        send_tg(msg)
                        st.session_state[alert_id] = True
            else:
                st.info("ℹ️ Is batch mein abhi koi high-accuracy ULTRA institutional setup trigger nahi hua hai.")

            with st.expander(f"📊 Complete Watchlist Overview ({len(res_df)} stocks)"):
                st.dataframe(res_df.sort_values("Symbol"), use_container_width=True, hide_index=True)
        else:
            st.warning("Koi candle data process nahi ho saka. Market timings ya token check karein.")
