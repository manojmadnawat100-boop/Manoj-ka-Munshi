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

# ---------------- Custom CSS for Buttons ----------------
st.markdown("""
<style>
div[data-testid="stButton"] > button[kind="primary"] {
    background-color: #00c853 !important;
    color: white !important;
    border: none !important;
    font-weight: bold;
    width: 100%;
}
div[data-testid="stButton"] > button[kind="secondary"] {
    background-color: #d50000 !important;
    color: white !important;
    border: none !important;
    font-weight: bold;
    width: 100%;
}
</style>
""", unsafe_allow_html=True)

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
HIST_URL = "https://api.upstox.com/v2/historical-candle"

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
LOT_SIZE = 15  # Bank Nifty standard lot size

# ---------------- Session State Initialization ----------------
if "last_signal" not in st.session_state:
    st.session_state.last_signal = "INIT"
if "last_alert_time" not in st.session_state:
    st.session_state.last_alert_time = 0
if "open_positions" not in st.session_state:
    st.session_state.open_positions = []

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
    default_res = {"rsi": 50.0, "ema9": 0.0, "ema21": 0.0, "above_vwap": True, "vwap": 0.0}
    try:
        url = f"{HIST_URL}/{ikey}/30minute/{today_str}"
        resp = requests.get(url, headers=HEADERS, timeout=6)
        if resp.status_code != 200:
            url = f"{HIST_URL}/{ikey}/day/{today_str}"
            resp = requests.get(url, headers=HEADERS, timeout=6)
            
        if resp.status_code != 200:
            return sym, default_res
        
        candles = resp.json().get("data", {}).get("candles", [])
        if not candles or len(candles) < 5:
            return sym, default_res
        
        candles = list(reversed(candles))
        df = pd.DataFrame(candles, columns=["ts", "o", "h", "l", "c", "v", "oi"])
        closes = df["c"].astype(float)
        highs = df["h"].astype(float)
        lows = df["l"].astype(float)
        vols = df["v"].astype(float)
        
        rsi = compute_wilder_rsi(closes)
        ema9 = closes.ewm(span=9, adjust=False).mean().iloc[-1]
        ema21 = closes.ewm(span=21, adjust=False).mean().iloc[-1]
        
        typical_price = (highs + lows + closes) / 3.0
        total_vol = vols.sum()
        vwap = (typical_price * vols).sum() / (total_vol + 1e-9) if total_vol > 0 else float(closes.iloc[-1])
        ltp = float(closes.iloc[-1])
        
        return sym, {
            "rsi": round(rsi, 1),
            "ema9": round(ema9, 2),
            "ema21": round(ema21, 2),
            "above_vwap": (ltp >= vwap),
            "vwap": round(vwap, 2)
        }
    except Exception:
        return sym, default_res

def fetch_vix_index():
    try:
        r = requests.get(QUOTE_URL, headers=HEADERS, params={"instrument_key": "NSE_INDEX|India VIX"}, timeout=5).json()
        val = list(r.get("data", {}).values())[0]
        return float(val.get("last_price", 14.0))
    except Exception:
        return 14.0

def fetch_banknifty_spot():
    try:
        r = requests.get(QUOTE_URL, headers=HEADERS, params={"instrument_key": "NSE_INDEX|Nifty Bank"}, timeout=5).json()
        val = list(r.get("data", {}).values())[0]
        return float(val.get("last_price", 0.0))
    except Exception:
        return 0.0

# ---------------- Main Execution ----------------
if not TOKEN:
    st.error("UPSTOX_TOKEN set nahi hai. Secrets file check karein.")
    st.stop()

keys = get_instrument_keys()
if not keys:
    st.stop()

@st.fragment(run_every=30)
def render_dashboard():
    ist_tz = timezone(timedelta(hours=5, minutes=30))
    now = datetime.now(ist_tz)
    ist_time = now.strftime('%H:%M:%S')
    today_str = now.strftime('%Y-%m-%d')

    top_c1, top_c2 = st.columns([4, 1])
    with top_c2:
        if st.button("🔄 Force Refresh"):
            st.rerun()

    vix = fetch_vix_index()
    spot_price = fetch_banknifty_spot()

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
        
        depth = q.get("depth", {})
        buy_qty = sum(item.get("quantity", 0) for item in depth.get("buy", []))
        sell_qty = sum(item.get("quantity", 0) for item in depth.get("sell", []))
        
        adv = metrics_map.get(sym, {"rsi": 50.0, "ema9": 0.0, "ema21": 0.0, "above_vwap": True, "vwap": ltp})
        weight = WEIGHTS.get(sym, 1.0)
        
        bull_score = 0
        bear_score = 0

        if pct_chg > 0.0: bull_score += 1
        elif pct_chg < 0.0: bear_score += 1

        if ltp >= open_price: bull_score += 1
        else: bear_score += 1

        if adv["vwap"] > 0:
            if adv["above_vwap"]: bull_score += 1
            else: bear_score += 1

        if adv["ema9"] > 0 and adv["ema21"] > 0:
            if adv["ema9"] >= adv["ema21"]: bull_score += 1
            else: bear_score += 1

        if adv["rsi"] >= 50: bull_score += 1
        else: bear_score += 1

        if buy_qty > sell_qty: bull_score += 1
        elif sell_qty > buy_qty: bear_score += 1

        if bull_score >= 4:
            score = 1
            state = "🟢 Bull Confluence"
        elif bear_score >= 4:
            score = -1
            state = "🔴 Bear Confluence"
        else:
            score = 0
            state = "⚪ Choppy"

        scores[sym] = score
        
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
    
    bull_power = (bullish_weight / total_active_weight) * 100 if total_active_weight > 0 else 0.0
    bear_power = (bearish_weight / total_active_weight) * 100 if total_active_weight > 0 else 0.0
    
    hdfc_bias = scores.get("HDFCBANK", 0)
    icici_bias = scores.get("ICICIBANK", 0)

    # ---------------- Metrics Display ----------------
    top_c1.caption(f"Sync Time: {ist_time} (IST) | Multi-Threaded Engine: Active")
    
    m1, m2, m3, m4 = st.columns(4)
    m1.metric("🟢 Bull Weight Power", f"{bull_power:.1f}%")
    m2.metric("🔴 Bear Weight Power", f"{bear_power:.1f}%")
    m3.metric("India VIX", f"{vix:.2f}", delta="Optimal Trade Regime" if not (vix_high_block or vix_low_chop) else "Caution")
    m4.metric("Heavyweights Bias", f"HDFC: {hdfc_bias} | ICICI: {icici_bias}")

    # ---------------- Dynamic Decision Engine ----------------
    current_signal = "HOLD"
    signal_reason = ""

    # Heavyweight logic mapping
    hw_bull_confirmed = (hdfc_bias == 1 and icici_bias == 1) or \
                        (hdfc_bias == 1 and icici_bias == 0) or \
                        (icici_bias == 1 and hdfc_bias == 0)

    hw_bear_confirmed = (hdfc_bias == -1 and icici_bias == -1) or \
                        (hdfc_bias == -1 and icici_bias == 0) or \
                        (icici_bias == -1 and hdfc_bias == 0)

    if vix_high_block:
        current_signal = "BLOCKED"
        signal_reason = "VIX > 22.0: Wild whipsaws active. Capital preservation mode."
    elif vix_low_chop:
        current_signal = "BLOCKED"
        signal_reason = "VIX < 11.5: Option premiums eating theta, directional moves absent."
    elif bull_power >= 30.0 and bear_power < 10.0 and hw_bull_confirmed:
        current_signal = "BUY CALL"
        signal_reason = f"Buyers Power ({bull_power:.1f}%) >= 30% & Sellers Power ({bear_power:.1f}%) < 10% with Bullish Heavyweights alignment."
    elif bear_power >= 30.0 and bull_power < 10.0 and hw_bear_confirmed:
        current_signal = "BUY PUT"
        signal_reason = f"Sellers Power ({bear_power:.1f}%) >= 30% & Buyers Power ({bull_power:.1f}%) < 10% with Bearish Heavyweights alignment."
    else:
        current_signal = "HOLD"
        signal_reason = "Consolidation Zone: Criteria not satisfied (Need Power >= 30% with Opposite < 10% & Heavyweights aligned)."

    # Visual Display
    if current_signal == "BUY CALL":
        st.success(f"## 🟢 RECOMMENDATION: BUY CALL (CE)\n*{signal_reason}*")
    elif current_signal == "BUY PUT":
        st.error(f"## 🔴 RECOMMENDATION: BUY PUT (PE)\n*{signal_reason}*")
    elif current_signal == "BLOCKED":
        st.warning(f"## ⛔ VOLATILITY SHIELD ENGAGED\n*{signal_reason}*")
    else:
        st.info(f"## 🟡 NO TRADE / ACCUMULATION ZONE\n*{signal_reason}*")

    # ---------------- Telegram Dispatch ----------------
    current_timestamp = time.time()
    if current_signal in ["BUY CALL", "BUY PUT"]:
        is_new_signal = (current_signal != st.session_state.last_signal)
        cooldown_elapsed = (current_timestamp - st.session_state.last_alert_time) > 900
        
        if is_new_signal or cooldown_elapsed:
            tg_text = (
                f"🚨 *BANK NIFTY SIGNAL ALERT*\n\n"
                f"*Action:* {current_signal}\n"
                f"*Bull Power:* {bull_power:.1f}%\n"
                f"*Bear Power:* {bear_power:.1f}%\n"
                f"*India VIX:* {vix:.2f}\n"
                f"*Heavyweights:* HDFC: {hdfc_bias}, ICICI: {icici_bias}\n"
                f"*Spot Price:* ₹{spot_price:,.2f}\n"
                f"*Time:* {ist_time} IST\n\n"
                f"_{signal_reason}_"
            )
            send_telegram_alert(tg_text)
            st.session_state.last_signal = current_signal
            st.session_state.last_alert_time = current_timestamp
            st.toast("⚡ Telegram Alert Broadcasted")
    elif current_signal == "HOLD":
        st.session_state.last_signal = "HOLD"

    st.divider()

    # ---------------- Order Execution & Lot Selection ----------------
    st.subheader("⚡ Quick Trade Execution Panel")
    
    col_lot, col_ce_buy, col_ce_sell, col_pe_buy, col_pe_sell = st.columns([1.5, 2, 2, 2, 2])
    
    with col_lot:
        lots = st.selectbox("Lots (1 Lot = 15 Qty)", options=list(range(1, 11)), index=0)
        total_qty = lots * LOT_SIZE
        st.caption(f"Total Quantity: **{total_qty}**")

    # Order action buttons
    with col_ce_buy:
        if st.button(f"🟢 Buy CALL (CE)\n[{lots} Lot]", key="btn_buy_ce", type="primary"):
            trade = {
                "id": len(st.session_state.open_positions) + 1,
                "type": "BUY CE",
                "lots": lots,
                "qty": total_qty,
                "entry_spot": spot_price,
                "time": ist_time
            }
            st.session_state.open_positions.append(trade)
            st.toast(f"✅ Order Placed: BUY CE {lots} Lot ({total_qty} Qty)")

    with col_ce_sell:
        if st.button(f"🔴 Sell CALL (CE)\n[{lots} Lot]", key="btn_sell_ce", type="secondary"):
            trade = {
                "id": len(st.session_state.open_positions) + 1,
                "type": "SELL CE",
                "lots": lots,
                "qty": total_qty,
                "entry_spot": spot_price,
                "time": ist_time
            }
            st.session_state.open_positions.append(trade)
            st.toast(f"✅ Order Placed: SELL CE {lots} Lot ({total_qty} Qty)")

    with col_pe_buy:
        if st.button(f"🔴 Buy PUT (PE)\n[{lots} Lot]", key="btn_buy_pe", type="secondary"):
            trade = {
                "id": len(st.session_state.open_positions) + 1,
                "type": "BUY PE",
                "lots": lots,
                "qty": total_qty,
                "entry_spot": spot_price,
                "time": ist_time
            }
            st.session_state.open_positions.append(trade)
            st.toast(f"✅ Order Placed: BUY PE {lots} Lot ({total_qty} Qty)")

    with col_pe_sell:
        if st.button(f"🟢 Sell PUT (PE)\n[{lots} Lot]", key="btn_sell_pe", type="primary"):
            trade = {
                "id": len(st.session_state.open_positions) + 1,
                "type": "SELL PE",
                "lots": lots,
                "qty": total_qty,
                "entry_spot": spot_price,
                "time": ist_time
            }
            st.session_state.open_positions.append(trade)
            st.toast(f"✅ Order Placed: SELL PE {lots} Lot ({total_qty} Qty)")

    # ---------------- Active Positions Section ----------------
    st.markdown("### 📊 Active Positions")
    if st.session_state.open_positions:
        pos_df = pd.DataFrame(st.session_state.open_positions)
        
        st.dataframe(pos_df, use_container_width=True, hide_index=True)
        
        if st.button("🗑️ Square Off / Clear All Positions"):
            st.session_state.open_positions = []
            st.rerun()
    else:
        st.info("Filhal koi active position open nahi hai.")

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
      
