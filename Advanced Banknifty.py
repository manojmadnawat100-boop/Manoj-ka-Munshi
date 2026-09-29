import time
import requests
import pandas as pd
import streamlit as st
from datetime import datetime, timezone, timedelta
from concurrent.futures import ThreadPoolExecutor

# ---------------- Streamlit UI Setup ----------------
st.set_page_config(
    page_title="Institutional Bank Nifty Terminal", 
    page_icon="🏦", 
    layout="wide"
)

st.title("🏦 Institutional Bank Nifty Trend, Depth & Regime Engine")

# ---------------- Secrets & Endpoints ----------------
TOKEN = st.secrets.get("UPSTOX_TOKEN", "")
TG_TOKEN = st.secrets.get("TELEGRAM_TOKEN", "")
TG_CHAT = st.secrets.get("TELEGRAM_CHAT_ID", "")

HEADERS = {
    "Authorization": f"Bearer {TOKEN}",
    "Accept": "application/json",
    "Api-Version": "2.0"
}
QUOTE_URL = "https://api.upstox.com/v2/market-quote/quotes"
HIST_URL = "https://api.upstox.com/v3/historical-candle"

# Official Weightage Hierarchy
WEIGHTS = {
    "HDFCBANK": 29.0,
    "ICICIBANK": 23.0,
    "SBIN": 10.5,
    "AXISBANK": 9.5,
    "KOTAKBANK": 9.0,
    "INDUSINDBK": 5.5,
    "BANKBARODA": 3.0,
    "FEDERALBNK": 3.0,
    "PNB": 2.5,
    "IDFCFIRSTB": 2.0,
    "AUBANK": 1.8,
    "BANDHANBNK": 1.2
}
SYMBOLS = list(WEIGHTS.keys())

# ---------------- Helper Functions ----------------
def send_telegram_alert(msg: str):
    if not TG_TOKEN or not TG_CHAT:
        return
    try:
        url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
        payload = {"chat_id": TG_CHAT, "text": msg, "parse_mode": "Markdown"}
        requests.post(url, json=payload, timeout=5)
    except Exception:
        pass

def compute_wilder_rsi(closes: pd.Series, period: int = 14) -> float:
    if len(closes) < period + 1:
        return 50.0
    delta = closes.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1/period, min_periods=period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1/period, min_periods=period, adjust=False).mean()
    rs = avg_gain / (avg_loss + 1e-9)
    rsi_series = 100 - (100 / (1 + rs))
    return float(rsi_series.iloc[-1])

@st.cache_data(ttl=86400)
def get_instrument_keys():
    try:
        inst = pd.read_json("https://assets.upstox.com/market-quote/instruments/exchange/NSE.json.gz")
        tc = "trading_symbol" if "trading_symbol" in inst.columns else "tradingsymbol"
        inst = inst[inst["instrument_key"].str.contains("NSE_EQ", na=False)]
        symbol_map = dict(zip(inst[tc], inst["instrument_key"]))
        return {s: symbol_map[s] for s in SYMBOLS if s in symbol_map}
    except Exception as e:
        st.error(f"Instruments Load Failed: {e}")
        return {}

def fetch_candle_metrics(args):
    sym, ikey, today_str = args
    try:
        url = f"{HIST_URL}/{ikey}/minutes/5/{today_str}"
        resp = requests.get(url, headers=HEADERS, timeout=6)
        if resp.status_code != 200:
            return sym, {"rsi": 50.0, "ema9": 0.0, "ema21": 0.0, "above_vwap": True, "vwap": 0.0}
        
        candles = resp.json().get("data", {}).get("candles", [])
        if len(candles) < 10:
            return sym, {"rsi": 50.0, "ema9": 0.0, "ema21": 0.0, "above_vwap": True, "vwap": 0.0}
        
        # Upstox returns candles [timestamp, open, high, low, close, volume, open_interest]
        candles = list(reversed(candles))
        df = pd.DataFrame(candles, columns=["ts", "o", "h", "l", "c", "v", "oi"])
        
        closes = df["c"].astype(float)
        highs = df["h"].astype(float)
        lows = df["l"].astype(float)
        vols = df["v"].astype(float)
        
        rsi = compute_wilder_rsi(closes)
        ema9 = closes.ewm(span=9, adjust=False).mean().iloc[-1]
        ema21 = closes.ewm(span=21, adjust=False).mean().iloc[-1]
        
        # Intraday True VWAP
        typical_price = (highs + lows + closes) / 3.0
        vwap = (typical_price * vols).sum() / (vols.sum() + 1e-9)
        ltp = float(closes.iloc[-1])
        
        return sym, {
            "rsi": round(rsi, 1),
            "ema9": round(ema9, 2),
            "ema21": round(ema21, 2),
            "above_vwap": (ltp >= vwap),
            "vwap": round(vwap, 2)
        }
    except Exception:
        return sym, {"rsi": 50.0, "ema9": 0.0, "ema21": 0.0, "above_vwap": True, "vwap": 0.0}

def fetch_vix_index():
    try:
        r = requests.get(QUOTE_URL, headers=HEADERS, params={"instrument_key": "NSE_INDEX|India VIX"}, timeout=5).json()
        val = list(r.get("data", {}).values())[0]
        return float(val.get("last_price", 14.0))
    except Exception:
        return 14.0

# ---------------- Main Execution ----------------
if not TOKEN:
    st.error("UPSTOX_TOKEN set nahi hai. Secrets file check karein.")
    st.stop()

keys = get_instrument_keys()
if not keys:
    st.stop()

# Auto Refresh & Session Memory
if "last_signal" not in st.session_state:
    st.session_state.last_signal = "INIT"
if "last_alert_time" not in st.session_state:
    st.session_state.last_alert_time = 0

@st.fragment(run_every=30)
def render_dashboard():
    # IST Time Conversion
    ist_tz = timezone(timedelta(hours=5, minutes=30))
    now = datetime.now(ist_tz)
    ist_time = now.strftime('%H:%M:%S')
    today_str = now.strftime('%Y-%m-%d')

    top_c1, top_c2 = st.columns([4, 1])
    with top_c2:
        if st.button("🔄 Force Refresh"):
            st.rerun()

    vix = fetch_vix_index()

    # VIX Risk Rules
    vix_high_block = vix > 22.0
    vix_low_chop = vix < 11.5

    # 1. Concurrent Candle Processing
    candle_tasks = [(sym, ik, today_str) for sym, ik in keys.items()]
    metrics_map = {}
    with ThreadPoolExecutor(max_workers=8) as executor:
        results = executor.map(fetch_candle_metrics, candle_tasks)
        for sym, metric in results:
            metrics_map[sym] = metric

    # 2. Batch Market Depth Quotes
    keys_param = ",".join(keys.values())
    try:
        quote_resp = requests.get(QUOTE_URL, headers=HEADERS, params={"instrument_key": keys_param}, timeout=8).json()
        quote_data = quote_resp.get("data", {})
    except Exception:
        quote_data = {}

    rows = []
    scores = {}

    for sym, ik in keys.items():
        q = quote_data.get(f"NSE_EQ:{sym}") or quote_data.get(ik)
        if not q:
            continue
        
        ltp = q.get("last_price", 0.0)
        ohlc = q.get("ohlc", {})
        prev_close = ohlc.get("close", 1.0) or 1.0
        open_price = ohlc.get("open", ltp)
        pct_chg = round(((ltp - prev_close) / prev_close) * 100, 2)
        
        # Order Depth Calculation
        depth = q.get("depth", {})
        buy_qty = sum(item.get("quantity", 0) for item in depth.get("buy", []))
        sell_qty = sum(item.get("quantity", 0) for item in depth.get("sell", []))
        
        adv = metrics_map.get(sym, {"rsi": 50.0, "ema9": 0.0, "ema21": 0.0, "above_vwap": True, "vwap": ltp})
        weight = WEIGHTS.get(sym, 1.0)
        
        # --- Strict Institutional Multi-Condition Logic ---
        # 1. Directional Price Move
        # 2. Above/Below Intraday Opening
        # 3. Dynamic VWAP Support
        # 4. Short-term Trend Alignment (EMA 9 vs 21)
        # 5. Non-Exhausted RSI Corridor
        bull_technicals = (pct_chg > 0.05) and (ltp >= open_price) and adv["above_vwap"] and (adv["ema9"] >= adv["ema21"]) and (adv["rsi"] < 70)
        bear_technicals = (pct_chg < -0.05) and (ltp <= open_price) and (not adv["above_vwap"]) and (adv["ema9"] <= adv["ema21"]) and (adv["rsi"] > 30)

        # Depth Confluence
        if buy_qty > 0 or sell_qty > 0:
            bull = bull_technicals and (buy_qty > sell_qty)
            bear = bear_technicals and (sell_qty > buy_qty)
        else:
            bull = bull_technicals
            bear = bear_technicals

        score = 1 if bull else (-1 if bear else 0)
        scores[sym] = score
        state = "🟢 Bull Confluence" if score == 1 else ("🔴 Bear Confluence" if score == -1 else "⚪ Choppy")
        
        rows.append({
            "Share": sym,
            "Weight (%)": weight,
            "LTP (₹)": ltp,
            "% Chg": pct_chg,
            "VWAP": adv["vwap"],
            "RSI (14)": adv["rsi"],
            "EMA Trend": "Bull" if adv["ema9"] >= adv["ema21"] else "Bear",
            "Order Book": "Buyers > Sellers" if buy_qty > sell_qty else "Sellers > Buyers",
            "State": state,
            "Score": score,
            "Weighted_Score": score * weight
        })

    if not rows:
        st.warning("Data sync chal raha hai...")
        return

    df = pd.DataFrame(rows)
    total_active_weight = df["Weight (%)"].sum()
    bullish_weight = df[df["Score"] == 1]["Weight (%)"].sum()
    bearish_weight = df[df["Score"] == -1]["Weight (%)"].sum()
    
    bull_power = (bullish_weight / total_active_weight) * 100
    bear_power = (bearish_weight / total_active_weight) * 100
    
    hdfc_bias = scores.get("HDFCBANK", 0)
    icici_bias = scores.get("ICICIBANK", 0)

    # ---------------- Metrics Display ----------------
    top_c1.caption(f"Sync Time: {ist_time} (IST) | Multi-Threaded Engine: Active")
    
    m1, m2, m3, m4 = st.columns(4)
    m1.metric("🟢 Bull Weight Power", f"{bull_power:.1f}%")
    m2.metric("🔴 Bear Weight Power", f"{bear_power:.1f}%")
    m3.metric("India VIX", f"{vix:.2f}", delta="High Volatility" if vix_high_block else ("Low Chop" if vix_low_chop else "Optimal Trade Regime"))
    m4.metric("Heavyweights Bias", f"HDFC: {hdfc_bias} | ICICI: {icici_bias}")

    # ---------------- Decision Engine ----------------
    current_signal = "HOLD"
    signal_reason = ""

    if vix_high_block:
        current_signal = "BLOCKED"
        signal_reason = "VIX > 22.0: Wild whipsaws active. Capital preservation mode."
    elif vix_low_chop:
        current_signal = "BLOCKED"
        signal_reason = "VIX < 11.5: Option premiums eating theta, directional moves absent."
    elif bull_power >= 62.0 and (hdfc_bias == 1 and icici_bias >= 0):
        current_signal = "BUY"
        signal_reason = f"Bull Power at {bull_power:.1f}% supported by HDFC Bank."
    elif bear_power >= 62.0 and (hdfc_bias == -1 and icici_bias <= 0):
        current_signal = "SELL"
        signal_reason = f"Bear Power at {bear_power:.1f}% backed by Heavyweight selloff."
    else:
        current_signal = "HOLD"
        signal_reason = "Consolidation Zone: Heavyweights divergent or power below 62% threshold."

    # Visual Display
    if current_signal == "BUY":
        st.success(f"## 🟢 INSTITUTIONAL BUY ACTIVE\n*{signal_reason}*")
    elif current_signal == "SELL":
        st.error(f"## 🔴 INSTITUTIONAL SELL ACTIVE\n*{signal_reason}*")
    elif current_signal == "BLOCKED":
        st.warning(f"## ⛔ VOLATILITY SHIELD ENGAGED\n*{signal_reason}*")
    else:
        st.info(f"## 🟡 NO TRADE / ACCUMULATION ZONE\n*{signal_reason}*")

    # ---------------- Telegram Dispatch with Cooldown ----------------
    current_timestamp = time.time()
    # 15 minutes cooldown (900 seconds) for the same signal, or immediate trigger if signal flips
    if current_signal in ["BUY", "SELL"]:
        is_new_signal = (current_signal != st.session_state.last_signal)
        cooldown_elapsed = (current_timestamp - st.session_state.last_alert_time) > 900
        
        if is_new_signal or cooldown_elapsed:
            tg_text = (
                f"🚨 *BANK NIFTY SIGNAL ALERT*\n\n"
                f"*Action:* {'BUY CALL / FUT' if current_signal == 'BUY' else 'BUY PUT / SHORT FUT'}\n"
                f"*Bull Power:* {bull_power:.1f}%\n"
                f"*Bear Power:* {bear_power:.1f}%\n"
                f"*India VIX:* {vix:.2f}\n"
                f"*Heavyweights:* HDFC: {hdfc_bias}, ICICI: {icici_bias}\n"
                f"*Time:* {ist_time} IST\n\n"
                f"_{signal_reason}_"
            )
            send_telegram_alert(tg_text)
            st.session_state.last_signal = current_signal
            st.session_state.last_alert_time = current_timestamp
            st.toast("⚡ Telegram Alert Broadcasted Successfully")
    elif current_signal == "HOLD":
        st.session_state.last_signal = "HOLD"

    st.divider()

    # ---------------- Heatmap Data Table ----------------
    st.markdown("### 📋 Constituent Matrix Breakdown")
    display_df = df.drop(columns=["Score", "Weighted_Score"]).sort_values("Weight (%)", ascending=False)
    
    st.dataframe(
        display_df.style.apply(
            lambda x: ['background: #e8f5e9' if v > 0 else ('background: #ffebee' if v < 0 else '') for v in x],
            subset=['% Chg']
        ),
        use_container_width=True,
        hide_index=True
    )

render_dashboard()
      
