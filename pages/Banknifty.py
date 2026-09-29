import time
import pandas as pd
import requests
import streamlit as st

# ---------------- Page Config ----------------
st.set_page_config(page_title="Bank Nifty Live Scanner", layout="wide")
st.title("🏦 Bank Nifty Live Order Book & Trend Scanner")

# ---------------- Secrets & Headers ----------------
TOKEN = st.secrets.get("UPSTOX_TOKEN", "")
HEADERS = {
    "Authorization": f"Bearer {TOKEN}", 
    "Accept": "application/json",
    "Api-Version": "2.0" 
}
QUOTE_URL = "https://api.upstox.com/v2/market-quote/quotes"

# Bank Nifty ke 12 main shares ki list
BANK_NIFTY_SYMBOLS = [
    "HDFCBANK", "ICICIBANK", "AXISBANK", "KOTAKBANK", 
    "SBIN", "INDUSINDBK", "BANKBARODA", "AUBANK", 
    "FEDERALBNK", "IDFCFIRSTB", "PNB", "BANDHANBNK"
]

@st.cache_data(ttl=86400)
def get_instrument_keys():
    try:
        # Pura DataFrame load karein
        inst = pd.read_json("https://assets.upstox.com/market-quote/instruments/exchange/NSE.json.gz")
        
        # Upstox API updates ko handle karne ke liye column check aur rename logic
        if 'trading_symbol' in inst.columns:
            inst.rename(columns={'trading_symbol': 'tradingsymbol'}, inplace=True)
        elif 'name' in inst.columns and 'tradingsymbol' not in inst.columns:
            inst.rename(columns={'name': 'tradingsymbol'}, inplace=True)
            
        # Agar error abhi bhi hai, toh exact columns screen par dikhayega
        if 'tradingsymbol' not in inst.columns:
            st.error(f"Instrument format badal gaya hai. Available columns yeh hain: {inst.columns.tolist()}")
            return {}

        # Safely filter conditions apply karein
        if 'segment' in inst.columns and 'instrument_type' in inst.columns:
            inst = inst[(inst["segment"] == "NSE_EQ") & (inst["instrument_type"] == "EQ")]
        
        bn_df = inst[inst["tradingsymbol"].isin(BANK_NIFTY_SYMBOLS)]
        
        return dict(zip(bn_df["tradingsymbol"], bn_df["instrument_key"]))
    except Exception as e:
        st.error(f"Error loading instruments: {e}")
        return {}

def fetch_live_data(instrument_dict):
    if not instrument_dict:
        return None, "Instrument list khali hai."

    keys_str = ",".join(instrument_dict.values())
    
    try:
        r = requests.get(f"{QUOTE_URL}?instrument_key={keys_str}", headers=HEADERS, timeout=10)
        if r.status_code == 401:
            return None, "TOKEN EXPIRED"
        r.raise_for_status()
        
        data = r.json().get("data", {})
        results = []
        
        for symbol, ikey in instrument_dict.items():
            if ikey in data:
                quote = data[ikey]
                
                # Live data points
                ltp = quote.get("last_price", 0)
                
                # Previous close 'ohlc' ke andar hota hai
                ohlc = quote.get("ohlc", {})
                prev_close = ohlc.get("close", 1) 
                
                buy_qty = quote.get("total_buy_quantity", 0)
                sell_qty = quote.get("total_sell_quantity", 0)
                volume = quote.get("volume", 0)
                
                # Price Trend 
                price_change = ((ltp - prev_close) / prev_close) * 100
                
                # Volume Side & Pending Order Logic
                if buy_qty > sell_qty:
                    order_trend = "🟢 Buyers Jyada"
                    strength = 1
                elif sell_qty > buy_qty:
                    order_trend = "🔴 Sellers Jyada"
                    strength = -1
                else:
                    order_trend = "⚪ Neutral"
                    strength = 0
                    
                results.append({
                    "Share": symbol,
                    "LTP (₹)": ltp,
                    "% Change": round(price_change, 2),
                    "Pending Buy Qty": buy_qty,
                    "Pending Sell Qty": sell_qty,
                    "Volume": volume,
                    "Order Trend": order_trend,
                    "Strength": strength
                })
                
        return results, None
    except Exception as e:
        return None, str(e)

# ---------------- Dashboard UI & Logic ----------------
if not TOKEN:
    st.error("UPSTOX_TOKEN set nahi hai. Streamlit secrets (`.streamlit/secrets.toml`) check karein.")
    st.stop()

instrument_keys = get_instrument_keys()

if not instrument_keys:
    st.stop() # Error message ab direct function ke andar se dikhega

# Auto Refresh logic 
@st.fragment(run_every=60)
def live_dashboard():
    col1, col2 = st.columns([3, 1])
    with col2:
        if st.button("🔄 Abhi Refresh Karein"):
            st.rerun()
            
    st.caption(f"Aakhri Update: {time.strftime('%H:%M:%S')}")
    
    data, error = fetch_live_data(instrument_keys)
    
    if error:
        if error == "TOKEN EXPIRED":
            st.error("Token expire ho gaya hai. Naya UPSTOX_TOKEN generate karke dalein.")
        else:
            st.error(f"API Error: {error}")
        return
        
    if data:
        df = pd.DataFrame(data)
        
        # ---------------- Overall Bank Nifty Trend Calculation ----------------
        total_shares = len(df)
        positive_shares = len(df[df["Strength"] == 1])
        negative_shares = len(df[df["Strength"] == -1])
        
        req_60_percent = total_shares * 0.60
        
        st.markdown("### 🧭 Bank Nifty Final Signal")
        c1, c2, c3, c4 = st.columns(4)
        c1.metric("Total Shares", total_shares)
        c2.metric("🟢 Positive (Buyers Jyada)", positive_shares)
        c3.metric("🔴 Negative (Sellers Jyada)", negative_shares)
        
        with c4:
            if positive_shares >= req_60_percent:
                st.success("## 🟢 BUY BANK NIFTY")
            elif negative_shares >= req_60_percent:
                st.error("## 🔴 SELL BANK NIFTY")
            else:
                st.warning("## 🟡 HOLD (No Clear Trend)")
                
        st.divider()
        
        # ---------------- Shares Data Table ----------------
        st.markdown("### 📊 Sabhi Shares ki Live Condition")
        
        display_df = df.drop(columns=["Strength"]).copy()
        
        st.dataframe(
            display_df.style.apply(
                lambda x: ['background: #e6ffe6' if v > 0 else ('background: #ffe6e6' if v < 0 else '') for v in x], 
                subset=['% Change']
            ),
            use_container_width=True,
            hide_index=True
        )
        
live_dashboard()
    
