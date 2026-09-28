import time  # FIXED: Capital 'I' se small 'i' kar diya
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from urllib.parse import quote

import pandas as pd
import requests
import streamlit as st

# ---------------- Page Config ----------------
st.set_page_config(page_title="Manoj Ji ki Dukaan", layout="wide")
st.title("🏪 Manoj Ji ki Dukaan - Nifty 500 (15 Min EMA Crossover)")

# ---------------- Secrets & Headers ----------------
TOKEN = st.secrets.get("UPSTOX_TOKEN", "")
HEADERS = {"Authorization": f"Bearer {TOKEN}", "Accept": "application/json"}
BASE = "https://api.upstox.com/v2/historical-candle/intraday"

# ---------------- Sidebar settings ----------------
st.sidebar.header("⚙️ Settings")
vol_mult = st.sidebar.number_input("Heavy volume (x avg)", 1.0, 10.0, 2.5, 0.5)
max_stocks = st.sidebar.slider("Kitne shares scan karein", 50, 500, 500, 50)
auto = st.sidebar.checkbox("Auto refresh", value=False)
interval = st.sidebar.slider("Refresh gap (minute)", 2, 15, 5)


# ---------------- Stock list ----------------
@st.cache_data(ttl=86400)
def load_stocks():
    inst = pd.read_json(
        "https://assets.upstox.com/market-quote/instruments/exchange/NSE.json.gz"
    )
    inst = inst[(inst["segment"] == "NSE_EQ") & (inst["instrument_type"] == "EQ")]
    inst = inst[["instrument_key", "trading_symbol"]]
    try:
        n500 = pd.read_csv(
            "https://niftyindices.com/IndexConstituent/ind_nifty500list.csv",
            storage_options={"User-Agent": "Mozilla/5.0"},
        )
        df = inst.merge(n500[["Symbol"]], left_on="trading_symbol", right_on="Symbol")
        return df[["instrument_key", "trading_symbol"]].reset_index(drop=True), True
    except Exception:
        return inst.head(500).reset_index(drop=True), False


# ---------------- Signal logic ----------------
def get_signal(df):
    # EMA 72 ke liye kam se kam 80 candles hona zaroori hai
    if len(df) < 80:
        return None, None, None, None
        
    last5 = df.tail(5)
    prev = df.iloc[:-5].tail(30)
    
    # Volume logic
    avg_vol = prev["v"].mean()
    vol5 = float(last5["v"].sum())
    ratio = (last5["v"].mean() / avg_vol) if avg_vol > 0 else 0
    price = float(df["c"].iloc[-1])

    # EMA 36 (Green) aur EMA 72 (Red) calculation
    df['EMA_36'] = df['c'].ewm(span=36, adjust=False).mean()
    df['EMA_72'] = df['c'].ewm(span=72, adjust=False).mean()

    # Crossover check (Last 2 candles)
    prev_ema36 = df['EMA_36'].iloc[-2]
    prev_ema72 = df['EMA_72'].iloc[-2]
    curr_ema36 = df['EMA_36'].iloc[-1]
    curr_ema72 = df['EMA_72'].iloc[-1]

    sig = None
    # BUY: Green (36) line Red (72) line ko neeche se upar cut kare
    if (prev_ema36 <= prev_ema72) and (curr_ema36 > curr_ema72):
        sig = "BUY"
    # SELL: Green (36) line Red (72) line ko upar se neeche cut kare
    elif (prev_ema36 >= prev_ema72) and (curr_ema36 < curr_ema72):
        sig = "SELL"

    return sig, price, vol5, ratio


def fetch_one(row):
    key, name = row["instrument_key"], row["trading_symbol"]
    try:
        # FIXED: Rate limit (10 req/sec) bachane ke liye delay badhaya 
        # (4 workers * 0.45s delay = safe execution)
        time.sleep(0.45) 
        
        # Yahan '15minute' lagaya gaya hai
        r = requests.get(
            f"{BASE}/{quote(key, safe='')}/15minute", headers=HEADERS, timeout=10
        )
        if r.status_code == 401:
            return {"error": "TOKEN"}
        if r.status_code == 429:
            return {"error": "RATE"}
        r.raise_for_status()
        candles = r.json()["data"]["candles"]
        if not candles:
            return None
            
        # Upstox newest-first deta hai -> ulta karke purani se nayi
        df = pd.DataFrame(
            candles, columns=["t", "o", "h", "l", "c", "v", "oi"]
        ).iloc[::-1].reset_index(drop=True)
        
        sig, price, vol5, ratio = get_signal(df)
        if not sig:
            return None
            
        return {
            "Share": name,
            "Signal": sig,
            "Bhav": round(price, 2),
            "Volume": int(vol5),
            "Vol x": round(ratio, 1),
            "Heavy": ratio >= vol_mult,
        }
    except Exception as e:
        return {"error": f"{name}: {e}"}


def run_scan(stocks):
    results, errors = [], []
    bar = st.progress(0, text="Scan chal raha hai...")
    total = len(stocks)
    rows = [r for _, r in stocks.iterrows()]
    
    # 4 workers taaki API block na ho
    with ThreadPoolExecutor(max_workers=4) as ex:
        for i, out in enumerate(ex.map(fetch_one, rows), 1):
            if out:
                (errors if "error" in out else results).append(out)
            if i % 10 == 0 or i == total:
                bar.progress(i / total, text=f"{i}/{total} shares")
    
    bar.empty()
    st.session_state["signals"] = results
    st.session_state["errors"] = errors
    st.session_state["last_scan"] = time.time()


# ---------------- Guard ----------------
if not TOKEN:
    st.error("UPSTOX_TOKEN secrets mein nahi mila. Token roz naya banana padta hai.")
    st.stop()

stocks, is_real = load_stocks()
stocks = stocks.head(max_stocks)
if not is_real:
    st.warning("Nifty 500 list nahi mili, NSE ke pehle shares scan ho rahe hain.")


# ---------------- Auto-Refresh Fragment ----------------
# UI block kiye bina timer chalane ke liye Streamlit fragment
@st.fragment(run_every=interval * 60 if auto else None)
def auto_scanner_section():
    manual_scan = st.button("🔍 Abhi Scan Karo (15 Min)")
    
    last = st.session_state.get("last_scan", 0)
    time_since_last = time.time() - last
    due = auto and (time_since_last >= interval * 60)
    
    # Agar button click hua ya auto-timer trigger hua
    if manual_scan or due:
        run_scan(stocks)
        st.rerun() # Refresh karke nayi list dikhane ke liye

    if "last_scan" in st.session_state:
        st.caption(
            "Aakhri scan: "
            + time.strftime("%H:%M:%S", time.localtime(st.session_state["last_scan"]))
        )
    else:
        st.info("Market time mein 'Abhi Scan Karo' dabao.")

# Fragment ko call karna
auto_scanner_section()


# ---------------- Errors & Results Display ----------------
errs = st.session_state.get("errors", [])
if any(e["error"] == "TOKEN" for e in errs):
    st.error("Token expire ho gaya (401). Naya token secrets mein daalo.")
elif any(e["error"] == "RATE" for e in errs):
    st.warning("Rate limit lag gayi. Shares kam karo ya refresh gap badhao.")
elif errs:
    with st.expander(f"⚠️ {len(errs)} shares mein error"):
        st.write([e["error"] for e in errs[:20]])

signals = st.session_state.get("signals", [])
heavy = [s for s in signals if s["Heavy"]]

if "positions" not in st.session_state:
    st.session_state["positions"] = {}


def add_pos(s):
    st.session_state["positions"][s["Share"]] = s
    st.toast("Rakh liya!")


# ---------------- Dukaan ----------------
st.divider()
col1, col2 = st.columns(2)
with col1:
    st.subheader(f"📊 Saare Signals ({len(signals)})")
    for idx, s in enumerate(signals):
        c1, c2 = st.columns([3, 1])
        c1.write(f"**{s['Share']}** @ ₹{s['Bhav']}")
        
        # Unique button key taaki galat share add na ho
        if s["Signal"] == "BUY":
            c2.button("🟢 BUY", key=f"b_{s['Share']}_{idx}", on_click=add_pos, args=(s,))
        else:
            c2.button("🔴 SELL", key=f"s_{s['Share']}_{idx}", on_click=add_pos, args=(s,))

with col2:
    st.subheader(f"🔥 Heavy Volume ({len(heavy)})")
    if heavy:
        hdf = pd.DataFrame(heavy)[["Share", "Signal", "Bhav", "Volume", "Vol x"]]
        st.dataframe(hdf, use_container_width=True)
    else:
        st.info("Abhi toofan nahi hai")


# ---------------- Position folder (paper only) ----------------
st.divider()
st.subheader("📁 Meri Position (sirf record, order nahi jata)")
pos = list(st.session_state["positions"].values())
if pos:
    pdf = pd.DataFrame(pos)[["Share", "Signal", "Bhav"]]
    st.dataframe(pdf, use_container_width=True)
    
    st.download_button(
        "📒 Hisab Download",
        pdf.to_csv(index=False).encode("utf-8"),
        f"hisab_{date.today()}.csv",
    )
    if st.button("❌ Sab Bech Do"):
        st.session_state["positions"] = {}
        st.rerun()
else:
    st.info("Folder khaali hai")
    
