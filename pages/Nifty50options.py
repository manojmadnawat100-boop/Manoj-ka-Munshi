import logging
from datetime import datetime
import pandas as pd
import requests
import streamlit as st

logging.basicConfig(level=logging.INFO)

st.set_page_config(page_title="Nifty Gamma Blast Trader & Auto-TSL", layout="wide")
st.title("⚡ NIFTY 50 Gamma Blast: Multi-Lot & Auto-Trailing SL Engine")

TOKEN = st.secrets.get("UPSTOX_TOKEN", "")
TG_TOKEN = st.secrets.get("TELEGRAM_TOKEN", "")
TG_CHAT = st.secrets.get("TELEGRAM_CHAT_ID", "")

HEADERS = {
    "Authorization": f"Bearer {TOKEN.strip()}",
    "Accept": "application/json"
}

NIFTY_KEY = "NSE_INDEX|Nifty 50"
NIFTY_LOT_SIZE = 75

if "tsl_tracker" not in st.session_state:
    st.session_state["tsl_tracker"] = {}

def send_tg(msg: str):
    if not TG_TOKEN or not TG_CHAT:
        return
    try:
        url = f"[https://api.telegram.org/bot](https://api.telegram.org/bot){TG_TOKEN}/sendMessage"
        requests.post(url, json={"chat_id": TG_CHAT, "text": msg, "parse_mode": "Markdown"}, timeout=5)
    except Exception as e:
        logging.error(f"Telegram fail: {e}")

def place_upstox_order(instrument_token: str, symbol_name: str, quantity: int, transaction_type: str = "BUY"):
    url = "[https://api.upstox.com/v2/order/place](https://api.upstox.com/v2/order/place)"
    payload = {
        "quantity": quantity,
        "product": "I",
        "validity": "DAY",
        "price": 0,
        "tag": "gamma_blast",
        "instrument_token": instrument_token,
        "order_type": "MARKET",
        "transaction_type": transaction_type,
        "disclosed_quantity": 0,
        "trigger_price": 0,
        "is_amo": False
    }
    
    try:
        res = requests.post(url, json=payload, headers=HEADERS, timeout=10)
        res_data = res.json()
        if res.status_code == 200 and res_data.get("status") == "success":
            order_id = res_data.get("data", {}).get("order_id", "Success")
            action_text = "BOUGHT" if transaction_type == "BUY" else "SQUARED OFF"
            st.success(f"✅ {action_text}: {symbol_name} ({quantity} Qty) | Order ID: {order_id}")
            send_tg(f"⚡ *ORDER ALERT*\nAction: *{action_text}*\nSymbol: *{symbol_name}*\nQty: {quantity}\nOrder ID: {order_id}")
            st.rerun()
        else:
            err_msg = res_data.get("errors", [{}])[0].get("message", res.text)
            st.error(f"❌ Order Failed: {err_msg}")
    except Exception as e:
        st.error(f"Order API error: {e}")

def fetch_live_positions():
    url = "[https://api.upstox.com/v2/portfolio/short-term-positions](https://api.upstox.com/v2/portfolio/short-term-positions)"
    try:
        res = requests.get(url, headers=HEADERS, timeout=8)
        if res.status_code == 200:
            return res.json().get("data", [])
        return []
    except Exception as e:
        logging.error(f"Positions fetch error: {e}")
        return []

@st.cache_data(ttl=1800)
def get_nifty_expiries():
    url = f"[https://api.upstox.com/v2/option/contract?instrument_key=](https://api.upstox.com/v2/option/contract?instrument_key=){NIFTY_KEY}"
    try:
        res = requests.get(url, headers=HEADERS, timeout=8)
        if res.status_code == 200:
            contracts = res.json().get("data", [])
            expiries = sorted(list(set([c["expiry"] for c in contracts if "expiry" in c])))
            return expiries
        return []
    except Exception:
        return []

def fetch_option_chain(expiry_date: str):
    url = f"[https://api.upstox.com/v2/option/chain?instrument_key=](https://api.upstox.com/v2/option/chain?instrument_key=){NIFTY_KEY}&expiry_date={expiry_date}"
    try:
        res = requests.get(url, headers=HEADERS, timeout=10)
        if res.status_code != 200:
            return None, f"HTTP {res.status_code}: {res.text}"
        data = res.json().get("data", [])
        return data, None
    except Exception as e:
        return None, str(e)

# ==========================================
# 💼 1. LIVE POSITIONS & AUTO-TRAILING ENGINE
# ==========================================
positions = fetch_live_positions()
open_positions = [p for p in positions if int(p.get("quantity", 0)) != 0]

st.subheader("💼 Active Positions & Institutional Trailing SL")

with st.container():
    if open_positions:
        total_pnl = sum([float(p.get("pnl", 0.0)) for p in open_positions])
        pnl_color = "green" if total_pnl >= 0 else "red"
        st.markdown(f"**
        
