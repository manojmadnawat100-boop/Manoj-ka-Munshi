import time
from datetime import datetime, timezone, timedelta
import pandas as pd
import requests
import streamlit as st

# ---------------- Page Config ----------------
st.set_page_config(page_title="Institutional Bank Nifty Scanner", layout="wide")
st.title("🏦 Institutional Bank Nifty Trend & Order Book Engine")

# ---------------- Secrets & Headers ----------------
TOKEN = st.secrets.get("UPSTOX_TOKEN", "")
HEADERS = {
    "Authorization": f"Bearer {TOKEN}", 
    "Accept": "application/json",
    "Api-Version": "2.0" 
}
QUOTE_URL = "https://api.upstox.com/v2/market-quote/quotes"

# Bank Nifty Constituents with approximate NSE weights
BANK_NIFTY_WEIGHTS = {
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

BANK_NIFTY_SYMBOLS = list(BANK_NIFTY_WEIGHTS.keys())

@st.cache_data(ttl=86400)
def get_instrument_keys():
    try:
        inst = pd.read_json("https://assets.upstox.com/market-quote/instruments/exchange/NSE.json.gz")
        
        if 'trading_symbol' in inst.columns:
            inst.rename(columns={'trading_symbol': 'tradingsymbol'}, inplace=True)
        elif 'name' in inst.columns and 'tradingsymbol' not in inst.columns:
            inst.rename(columns={'name': 'tradingsymbol'}, inplace=True)
            
        if 'tradingsymbol' not in inst.columns:
            st.error(f"Instrument format badal gaya hai: {inst.columns.tolist()}")
            return {}

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
        r = requests.get(QUOTE_URL, headers=HEADERS, params={"instrument_key": keys_str}, timeout=10)
        if r.status_code == 401:
            return None, "TOKEN EXPIRED"
        r.raise_for_status()
        
        data = r.json().get("data", {})
        results = []
        
        for symbol, ikey in instrument_dict.items():
            quote = data.get(f"NSE_EQ:{symbol}") or data.get(ikey)
            
            if quote:
                ltp = quote.get("last_price", 0)
                ohlc = quote.get("ohlc", {})
                prev_close = ohlc.get("close", 1) 
                open_price = ohlc.get("open", ltp)
                
                buy_qty = quote.get("total_buy_quantity", 0)
                sell_qty = quote.get("total_sell_quantity", 0)
                volume = quote.get("volume", 0)
                
                price_change = ((ltp - prev_close) / prev_close) * 100
                day_open_bias = ltp > open_price
                
                # --- False-Signal Filter (Confluence Logic) ---
                # Positive condition: Price up from close AND open, Buy depth > Sell depth
                is_bullish = (price_change > 0.05) and day_open_bias and (buy_qty > sell_qty)
                # Negative condition: Price down from close AND open, Sell depth > Buy depth
                is_bearish = (price_change < -0.05) and (not day_open_bias) and (sell_qty > buy_qty)
                
                weight = BANK_NIFTY_WEIGHTS.get(symbol, 1.0)
                
                if is_bullish:
                    signal_state = "🟢 Strong Bullish"
                    score = 1
                elif is_bearish:
                    signal_state = "🔴 Strong Bearish"
                    score = -1
                else:
                    signal_state = "⚪ Mixed / Sideways"
                    score = 0
                    
                results.append({
                    "Share": symbol,
                    "Weight (%)": weight,
                    "LTP (₹)": ltp,
                    "% Change": round(price_change, 2),
                    "Open (₹)": open_price,
                    "Pending Buy Qty": int(buy_qty),
                    "Pending Sell Qty": int(sell_qty),
                    "Volume": int(volume),
                    "State": signal_state,
                    "Score": score,
                    "Weighted_Score": score * weight
                })
                
        if not results:
            return None, "Data parse nahi hua."
             
        return results, None
    except Exception as e:
        return None, str(e)

# ---------------- Dashboard UI & Logic ----------------
if not TOKEN:
    st.error("UPSTOX_TOKEN set nahi hai. Secrets file check karein.")
    st.stop()

instrument_keys = get_instrument_keys()
if not instrument_keys:
    st.stop()

@st.fragment(run_every=30)
def live_dashboard():
    col1, col2 = st.columns([3, 1])
    with col2:
        if st.button("🔄 Abhi Refresh Karein"):
            st.rerun()
            
    ist_offset = timezone(timedelta(hours=5, minutes=30))
    current_time = datetime.now(ist_offset).strftime('%H:%M:%S')
    st.caption(f"Aakhri Sync (IST): {current_time} | Auto-refresh: 30 sec")
    
    data, error = fetch_live_data(instrument_keys)
    
    if error:
        if error == "TOKEN EXPIRED":
            st.error("Token expire ho gaya hai. Naya UPSTOX_TOKEN dalein.")
        else:
            st.error(f"API Error: {error}")
        return
        
    if data:
        df = pd.DataFrame(data)
        
        # ---------------- Decision Matrix ----------------
        total_weight = df["Weight (%)"].sum()
        bullish_weight = df[df["Score"] == 1]["Weight (%)"].sum()
        bearish_weight = df[df["Score"] == -1]["Weight (%)"].sum()
        
        bullish_pct = (bullish_weight / total_weight) * 100
        bearish_pct = (bearish_weight / total_weight) * 100
        
        # Heavyweight verification (HDFC + ICICI)
        hdfc_score = df.loc[df["Share"] == "HDFCBANK", "Score"].values[0] if "HDFCBANK" in df["Share"].values else 0
        icici_score = df.loc[df["Share"] == "ICICIBANK", "Score"].values[0] if "ICICIBANK" in df["Share"].values else 0
        
        st.markdown("### 🧭 Bank Nifty High-Conviction Signal Engine")
        m1, m2, m3, m4 = st.columns(4)
        m1.metric("Bullish Weight Power", f"{bullish_pct:.1f}%")
        m2.metric("Bearish Weight Power", f"{bearish_pct:.1f}%")
        m3.metric("HDFC Bank Bias", "🟢 Bull" if hdfc_score == 1 else ("🔴 Bear" if hdfc_score == -1 else "⚪ Neutral"))
        m4.metric("ICICI Bank Bias", "🟢 Bull" if icici_score == 1 else ("🔴 Bear" if icici_score == -1 else "⚪ Neutral"))
        
        # Final Strict Logic: Minimum 65% weighted power + At least one heavyweight aligned
        if bullish_pct >= 65 and (hdfc_score == 1 or icici_score == 1) and (hdfc_score != -1 and icici_score != -1):
            st.success("## 🟢 HIGH-CONVICTION BUY: Bullish Power 65%+ & Heavyweights Supporting")
        elif bearish_pct >= 65 and (hdfc_score == -1 or icici_score == -1) and (hdfc_score != 1 and icici_score != 1):
            st.error("## 🔴 HIGH-CONVICTION SELL: Bearish Power 65%+ & Heavyweights Supporting")
        else:
            st.warning("## 🟡 NO TRADE / CHOPPY ZONE: False Signal Traps Active or Lack of Confluence")
            
        st.divider()
        
        # ---------------- Shares Data Table ----------------
        st.markdown("### 📊 Constituents Heatmap & Depth Status")
        
        table_df = df.drop(columns=["Score", "Weighted_Score"]).sort_values(by="Weight (%)", ascending=False)
        
        st.dataframe(
            table_df.style.apply(
                lambda x: ['background: #d4edda' if v > 0 else ('background: #f8d7da' if v < 0 else '') for v in x], 
                subset=['% Change']
            ),
            use_container_width=True,
            hide_index=True
        )

live_dashboard()
