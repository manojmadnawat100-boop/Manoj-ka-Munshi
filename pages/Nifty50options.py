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
NIFTY_LOT_SIZE = 75  # Nifty 1 lot size

# Trailing SL tracking state dictionary
if "tsl_tracker" not in st.session_state:
    st.session_state["tsl_tracker"] = {}

def send_tg(msg: str):
    if not TG_TOKEN or not TG_CHAT:
        return
    try:
        url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
        requests.post(url, json={"chat_id": TG_CHAT, "text": msg, "parse_mode": "Markdown"}, timeout=5)
    except Exception as e:
        logging.error(f"Telegram fail: {e}")

def place_upstox_order(instrument_token: str, symbol_name: str, quantity: int, transaction_type: str = "BUY"):
    """Places Intraday (MIS) Market Order"""
    url = "https://api.upstox.com/v2/order/place"
    payload = {
        "quantity": quantity,
        "product": "I",              # MIS (Intraday)
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
    url = "https://api.upstox.com/v2/portfolio/short-term-positions"
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
    url = f"https://api.upstox.com/v2/option/contract?instrument_key={NIFTY_KEY}"
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
    url = f"https://api.upstox.com/v2/option/chain?instrument_key={NIFTY_KEY}&expiry_date={expiry_date}"
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
        st.markdown(f"**Total Live P&L:** <span style='font-size:20px; font-weight:bold; color:{pnl_color};'>₹{total_pnl:,.2f}</span>", unsafe_allow_html=True)

        for pos in open_positions:
            sym = pos.get("trading_symbol", "")
            qty = int(pos.get("quantity", 0))
            buy_avg = float(pos.get("buy_price", 0.0))
            ltp = float(pos.get("last_price", 0.0))
            pnl = float(pos.get("pnl", 0.0))
            ikey = pos.get("instrument_token", "")
            pos_color = "#28a745" if pnl >= 0 else "#dc3545"

            # --- DYNAMIC TRAILING SL COMPUTATION ---
            if sym not in st.session_state["tsl_tracker"]:
                st.session_state["tsl_tracker"][sym] = {
                    "peak_ltp": max(ltp, buy_avg),
                    "current_sl": round(buy_avg * 0.65, 2),  # Base Initial SL (-35%)
                    "stage": "INITIAL"
                }

            tdata = st.session_state["tsl_tracker"][sym]
            if ltp > tdata["peak_ltp"]:
                tdata["peak_ltp"] = ltp

            gain_pct = (ltp - buy_avg) / buy_avg if buy_avg > 0 else 0

            # Stage Upgrades
            if gain_pct >= 1.50:  # 150%+ Super Blast: Trail 20% below peak
                tdata["current_sl"] = max(tdata["current_sl"], round(tdata["peak_ltp"] * 0.80, 2))
                tdata["stage"] = "🚀 20% PEAK TRAIL (SUPER BLAST)"
            elif gain_pct >= 1.00:  # 100%+ Double: Lock 50% Profit
                tdata["current_sl"] = max(tdata["current_sl"], round(buy_avg * 1.50, 2))
                tdata["stage"] = "🔒 +50% PROFIT LOCKED"
            elif gain_pct >= 0.50:  # +50% Spike: Cost to Cost
                tdata["current_sl"] = max(tdata["current_sl"], buy_avg)
                tdata["stage"] = "🛡️ BREAKEVEN (COST-TO-COST)"
            else:
                tdata["stage"] = "🎯 BASE SL (-35%)"

            # --- AUTO EXIT TRIGGER CHECK ---
            if ltp <= tdata["current_sl"]:
                st.error(f"🛑 Trailing SL Hit for {sym} at ₹{ltp} (SL Level: ₹{tdata['current_sl']})! Auto-Exiting...")
                place_upstox_order(ikey, sym, abs(qty), transaction_type="SELL")
                del st.session_state["tsl_tracker"][sym]
                st.rerun()

            # Display Matrix Row
            with st.container():
                p1, p2, p3, p4, p5 = st.columns([3, 2, 2, 2, 2])
                with p1:
                    st.markdown(f"### `{sym}`")
                    st.caption(f"Qty: **{qty}** | Avg Buy: **₹{buy_avg:,.2f}**")
                with p2:
                    st.metric("Current LTP", f"₹{ltp:,.2f}")
                    st.caption(f"Peak LTP: **₹{tdata['peak_ltp']:,.2f}**")
                with p3:
                    st.markdown(f"**Trailing SL:** <span style='font-size:18px; font-weight:bold; color:#ff9800;'>₹{tdata['current_sl']:,.2f}</span>", unsafe_allow_html=True)
                    st.caption(f"Regime: `{tdata['stage']}`")
                with p4:
                    ret_pct = ((ltp - buy_avg) / buy_avg * 100) if buy_avg > 0 else 0
                    st.metric("Return %", f"{ret_pct:+.2f}%")
                    st.markdown(f"<span style='color:{pos_color}; font-weight:bold;'>₹{pnl:,.2f}</span>", unsafe_allow_html=True)
                with p5:
                    st.write("")
                    if st.button("🛑 Manual Exit", key=f"exit_{sym}", type="primary", use_container_width=True):
                        if sym in st.session_state["tsl_tracker"]:
                            del st.session_state["tsl_tracker"][sym]
                        place_upstox_order(ikey, sym, abs(qty), transaction_type="SELL")
                st.divider()
    else:
        st.session_state["tsl_tracker"].clear()
        st.info("ℹ️ Abhi koi active open position nahi hai. Jaise hi entry hogi, auto-trailing engine active ho jayega.")

st.markdown("---")

# ==========================================
# ⚡ 2. LOT SELECTION, EXPIRY & RADAR
# ==========================================
today_str = datetime.now().strftime("%Y-%m-%d")
expiries = get_nifty_expiries()

c1, c2, c3, c4 = st.columns([2, 1, 1, 1])
with c1:
    selected_expiry = st.selectbox("Select Expiry Date", expiries, index=0) if expiries else None
with c2:
    lots = st.selectbox("Lots (1-10)", list(range(1, 11)), index=0)
    total_qty = lots * NIFTY_LOT_SIZE
    st.caption(f"Total Qty: **{total_qty}**")
with c3:
    range_strikes = st.slider("Strikes View", min_value=5, max_value=15, value=7, step=1)
with c4:
    st.write("")
    st.write("")
    refresh_btn = st.button("🔄 Refresh Radar", use_container_width=True)

is_today_expiry = (selected_expiry == today_str)

if not is_today_expiry and selected_expiry:
    st.warning(f"⚠️ Aaj Expiry Day nahi hai (Selected Expiry: {selected_expiry} | Today: {today_str}). Gamma alerts Expiry day par activate honge.")

if selected_expiry and TOKEN:
    with st.spinner("Option Chain aur liquidity metrics load ho rahe hain..."):
        chain_data, err = fetch_option_chain(selected_expiry)

    if not err and chain_data:
        rows = []
        underlying_spot = 0.0

        for node in chain_data:
            strike = float(node.get("strike_price", 0.0))
            spot = node.get("underlying_spot_price", 0.0)
            if spot > 0:
                underlying_spot = spot

            call = node.get("call_options", {})
            put = node.get("put_options", {})

            c_data = call.get("market_data", {})
            c_ltp = float(c_data.get("ltp", 0.0))
            c_oi = int(c_data.get("oi", 0))
            c_prev_oi = int(c_data.get("prev_oi", 0))
            c_vol = int(c_data.get("volume", 0))
            c_oi_chg = c_oi - c_prev_oi
            c_key = call.get("instrument_key", "")

            p_data = put.get("market_data", {})
            p_ltp = float(p_data.get("ltp", 0.0))
            p_oi = int(p_data.get("oi", 0))
            p_prev_oi = int(p_data.get("prev_oi", 0))
            p_vol = int(p_data.get("volume", 0))
            p_oi_chg = p_oi - p_prev_oi
            p_key = put.get("instrument_key", "")

            rows.append({
                "Strike": strike,
                "CE_Key": c_key,
                "CE_OI": c_oi,
                "CE_OI_Chg": c_oi_chg,
                "CE_Vol": c_vol,
                "CE_LTP": c_ltp,
                "PE_Key": p_key,
                "PE_LTP": p_ltp,
                "PE_Vol": p_vol,
                "PE_OI_Chg": p_oi_chg,
                "PE_OI": p_oi
            })

        df = pd.DataFrame(rows).sort_values("Strike").reset_index(drop=True)

        if underlying_spot > 0:
            atm_idx = (df["Strike"] - underlying_spot).abs().idxmin()
            start_idx = max(0, atm_idx - range_strikes)
            end_idx = min(len(df), atm_idx + range_strikes + 1)
            near_df = df.iloc[start_idx:end_idx].copy()
        else:
            near_df = df.copy()

        # Gamma Traps Detection
        blast_alerts = []
        for _, r in near_df.iterrows():
            strike = int(r["Strike"])

            ce_unwinding_pct = (abs(r["CE_OI_Chg"]) / (r["CE_OI"] + abs(r["CE_OI_Chg"]))) * 100 if (r["CE_OI"] + abs(r["CE_OI_Chg"])) > 0 else 0
            if (r["CE_OI_Chg"] < -80000) and (ce_unwinding_pct >= 15) and (r["CE_Vol"] > r["CE_OI"] * 1.2) and (r["CE_LTP"] > 5):
                blast_alerts.append({
                    "Type": "🚀 CE GAMMA BLAST (BULLISH)",
                    "Option": f"{strike} CE",
                    "Key": r["CE_Key"],
                    "LTP": r["CE_LTP"],
                    "Unwound": f"{abs(r['CE_OI_Chg']):,}",
                    "Volume": f"{r['CE_Vol']:,}"
                })

            pe_unwinding_pct = (abs(r["PE_OI_Chg"]) / (r["PE_OI"] + abs(r["PE_OI_Chg"]))) * 100 if (r["PE_OI"] + abs(r["PE_OI_Chg"])) > 0 else 0
            if (r["PE_OI_Chg"] < -80000) and (pe_unwinding_pct >= 15) and (r["PE_Vol"] > r["PE_OI"] * 1.2) and (r["PE_LTP"] > 5):
                blast_alerts.append({
                    "Type": "🔥 PE GAMMA BLAST (BEARISH)",
                    "Option": f"{strike} PE",
                    "Key": r["PE_Key"],
                    "LTP": r["PE_LTP"],
                    "Unwound": f"{abs(r['PE_OI_Chg']):,}",
                    "Volume": f"{r['PE_Vol']:,}"
                })

        st.metric("NIFTY 50 Spot Price", f"₹{underlying_spot:,.2f}")

        # High Probability Blast Execution
        if blast_alerts:
            st.subheader(f"🎯 High-Probability Gamma Blast Opportunities ({len(blast_alerts)})")
            for alert in blast_alerts:
                b1, b2, b3, b4 = st.columns([3, 2, 2, 2])
                with b1:
                    st.markdown(f"**{alert['Type']}** — `{alert['Option']}`")
                    st.caption(f"Unwound: {alert['Unwound']} | Vol: {alert['Volume']}")
                with b2:
                    st.markdown(f"**LTP:** ₹{alert['LTP']}")
                with b3:
                    st.markdown(f"**Order:** {lots} Lot ({total_qty} Qty)")
                with b4:
                    btn_text = f"🟢 BUY {alert['Option']}" if "CE" in alert['Option'] else f"🔴 BUY {alert['Option']}"
                    if st.button(btn_text, key=f"blast_{alert['Option']}"):
                        place_upstox_order(alert["Key"], alert["Option"], total_qty, "BUY")
            st.divider()

        # 1-Click Option Matrix
        st.subheader(f"📋 Near ATM Strikes (Order Size: {lots} Lot / {total_qty} Qty)")
        h1, h2, h3, h4, h5, h6, h7 = st.columns([2, 1, 1, 1, 1, 1, 2])
        h1.markdown("**CE Order & Volume**")
        h2.markdown("**CE OI Chg**")
        h3.markdown("**CE LTP**")
        h4.markdown("**Strike**")
        h5.markdown("**PE LTP**")
        h6.markdown("**PE OI Chg**")
        h7.markdown("**PE Order & Volume**")

        for _, row in near_df.iterrows():
            strike_val = int(row["Strike"])
            m1, m2, m3, m4, m5, m6, m7 = st.columns([2, 1, 1, 1, 1, 1, 2])

            with m1:
                if st.button(f"🟢 Buy {strike_val} CE", key=f"mat_ce_{strike_val}"):
                    place_upstox_order(row["CE_Key"], f"{strike_val} CE", total_qty, "BUY")
                st.caption(f"Vol: {row['CE_Vol']:,}")
            with m2:
                ce_chg = row["CE_OI_Chg"]
                color = "red" if ce_chg < 0 else "green"
                st.markdown(f"<span style='color:{color}; font-weight:bold;'>{ce_chg:,}</span>", unsafe_allow_html=True)
            with m3:
                st.markdown(f"**₹{row['CE_LTP']}**")
            with m4:
                if abs(strike_val - underlying_spot) < 30:
                    st.markdown(f"🎯 **{strike_val}**")
                else:
                    st.markdown(f"**{strike_val}**")
            with m5:
                st.markdown(f"**₹{row['PE_LTP']}**")
            with m6:
                pe_chg = row["PE_OI_Chg"]
                color = "red" if pe_chg < 0 else "green"
                st.markdown(f"<span style='color:{color}; font-weight:bold;'>{pe_chg:,}</span>", unsafe_allow_html=True)
            with m7:
                if st.button(f"🔴 Buy {strike_val} PE", key=f"mat_pe_{strike_val}"):
                    place_upstox_order(row["PE_Key"], f"{strike_val} PE", total_qty, "BUY")
                st.caption(f"Vol: {row['PE_Vol']:,}")
E_OI_Chg"])) > 0 else 0
            if (r["PE_OI_Chg"] < -80000) and (pe_unwinding_pct >= 15) and (r["PE_Vol"] > r["PE_OI"] * 1.2) and (r["PE_LTP"] > 5):
                blast_alerts.append({
                    "Type": "🔥 PE GAMMA BLAST (BEARISH)",
                    "Option": f"{strike} PE",
                    "Key": r["PE_Key"],
                    "LTP": r["PE_LTP"],
                    "Unwound": f"{abs(r['PE_OI_Chg']):,}",
                    "Volume": f"{r['PE_Vol']:,}"
                })

        st.metric("NIFTY 50 Spot Price", f"₹{underlying_spot:,.2f}")

        # High Probability Blast Execution
        if blast_alerts:
            st.subheader(f"🎯 High-Probability Gamma Blast Opportunities ({len(blast_alerts)})")
            for alert in blast_alerts:
                b1, b2, b3, b4 = st.columns([3, 2, 2, 2])
                with b1:
                    st.markdown(f"**{alert['Type']}** — `{alert['Option']}`")
                    st.caption(f"Unwound: {alert['Unwound']} | Vol: {alert['Volume']}")
                with b2:
                    st.markdown(f"**LTP:** ₹{alert['LTP']}")
                with b3:
                    st.markdown(f"**Order:** {lots} Lot ({total_qty} Qty)")
                with b4:
                    btn_text = f"🟢 BUY {alert['Option']}" if "CE" in alert['Option'] else f"🔴 BUY {alert['Option']}"
                    if st.button(btn_text, key=f"blast_{alert['Option']}"):
                        place_upstox_order(alert["Key"], alert["Option"], total_qty, "BUY")
            st.divider()

        # 1-Click Option Matrix
        st.subheader(f"📋 Near ATM Strikes (Order Size: {lots} Lot / {total_qty} Qty)")
        h1, h2, h3, h4, h5, h6, h7 = st.columns([2, 1, 1, 1, 1, 1, 2])
        h1.markdown("**CE Order & Volume**")
        h2.markdown("**CE OI Chg**")
        h3.markdown("**CE LTP**")
        h4.markdown("**Strike**")
        h5.markdown("**PE LTP**")
        h6.markdown("**PE OI Chg**")
        h7.markdown("**PE Order & Volume**")

        for _, row in near_df.iterrows():
            strike_val = int(row["Strike"])
            m1, m2, m3, m4, m5, m6, m7 = st.columns([2, 1, 1, 1, 1, 1, 2])

            with m1:
                if st.button(f"🟢 Buy {strike_val} CE", key=f"mat_ce_{strike_val}"):
                    place_upstox_order(row["CE_Key"], f"{strike_val} CE", total_qty, "BUY")
                st.caption(f"Vol: {row['CE_Vol']:,}")
            with m2:
                ce_chg = row["CE_OI_Chg"]
                color = "red" if ce_chg < 0 else "green"
                st.markdown(f"<span style='color:{color}; font-weight:bold;'>{ce_chg:,}</span>", unsafe_allow_html=True)
            with m3:
                st.markdown(f"**₹{row['CE_LTP']}**")
            with m4:
                if abs(strike_val - underlying_spot) < 30:
                    st.markdown(f"🎯 **{strike_val}**")
                else:
                    st.markdown(f"**{strike_val}**")
            with m5:
                st.markdown(f"**₹{row['PE_LTP']}**")
            with m6:
                pe_chg = row["PE_OI_Chg"]
                color = "red" if pe_chg < 0 else "green"
                st.markdown(f"<span style='color:{color}; font-weight:bold;'>{pe_chg:,}</span>", unsafe_allow_html=True)
            with m7:
                if st.button(f"🔴 Buy {strike_val} PE", key=f"mat_pe_{strike_val}"):
                    place_upstox_order(row["PE_Key"], f"{strike_val} PE", total_qty, "BUY")
                st.caption(f"Vol: {row['PE_Vol']:,}")
                
