
import logging
from datetime import datetime
import numpy as np
import pandas as pd
import requests
import streamlit as st

logging.basicConfig(level=logging.INFO)

st.set_page_config(page_title="Nifty Gamma Blast Radar", layout="wide")
st.title("⚡ NIFTY 50 Institutional Gamma Blast & Short-Covering Engine")

TOKEN = st.secrets.get("UPSTOX_TOKEN", "")
TG_TOKEN = st.secrets.get("TELEGRAM_TOKEN", "")
TG_CHAT = st.secrets.get("TELEGRAM_CHAT_ID", "")

HEADERS = {
    "Authorization": f"Bearer {TOKEN.strip()}",
    "Accept": "application/json"
}

NIFTY_KEY = "NSE_INDEX|Nifty 50"

def send_tg(msg: str):
    if not TG_TOKEN or not TG_CHAT:
        return
    try:
        url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
        requests.post(url, json={"chat_id": TG_CHAT, "text": msg, "parse_mode": "Markdown"}, timeout=5)
    except Exception as e:
        logging.error(f"Telegram fail: {e}")

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
    except Exception as e:
        st.error(f"Expiry fetch error: {e}")
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

# --- UI Controls ---
col1, col2, col3 = st.columns([2, 2, 1])
with col1:
    expiries = get_nifty_expiries()
    if expiries:
        selected_expiry = st.selectbox("Select Expiry Date", expiries, index=0)
    else:
        selected_expiry = None
        st.warning("Expiry dates load nahi ho saki. Token check karein.")

with col2:
    range_strikes = st.slider("Near ATM Strikes View", min_value=5, max_value=20, value=10, step=1)

with col3:
    st.write("")
    st.write("")
    scan_btn = st.button("🔄 Scan Gamma Traps", use_container_width=True)

if (scan_btn or "nifty_chain_loaded" not in st.session_state) and selected_expiry:
    st.session_state["nifty_chain_loaded"] = True

    if not TOKEN:
        st.error("⚠️ Streamlit Secrets mein `UPSTOX_TOKEN` enter karein.")
    else:
        with st.spinner("Option Chain aur Gamma traps calculate ho rahe hain..."):
            chain_data, err = fetch_option_chain(selected_expiry)

        if err:
            st.error(err)
        elif not chain_data:
            st.warning("Option Chain data khali mila.")
        else:
            rows = []
            underlying_spot = 0.0

            total_ce_oi = 0
            total_pe_oi = 0

            for node in chain_data:
                strike = float(node.get("strike_price", 0.0))
                spot = node.get("underlying_spot_price", 0.0)
                if spot > 0:
                    underlying_spot = spot

                call = node.get("call_options", {})
                put = node.get("put_options", {})

                # Call metrics
                c_data = call.get("market_data", {})
                c_ltp = float(c_data.get("ltp", 0.0))
                c_oi = int(c_data.get("oi", 0))
                c_prev_oi = int(c_data.get("prev_oi", 0))
                c_vol = int(c_data.get("volume", 0))
                c_oi_chg = c_oi - c_prev_oi
                c_gamma = float(call.get("option_greeks", {}).get("gamma", 0.0))

                # Put metrics
                p_data = put.get("market_data", {})
                p_ltp = float(p_data.get("ltp", 0.0))
                p_oi = int(p_data.get("oi", 0))
                p_prev_oi = int(p_data.get("prev_oi", 0))
                p_vol = int(p_data.get("volume", 0))
                p_oi_chg = p_oi - p_prev_oi
                p_gamma = float(put.get("option_greeks", {}).get("gamma", 0.0))

                total_ce_oi += c_oi
                total_pe_oi += p_oi

                rows.append({
                    "Strike": strike,
                    "CE_OI": c_oi,
                    "CE_OI_Chg": c_oi_chg,
                    "CE_Vol": c_vol,
                    "CE_LTP": c_ltp,
                    "CE_Gamma": c_gamma,
                    "PE_LTP": p_ltp,
                    "PE_Vol": p_vol,
                    "PE_OI_Chg": p_oi_chg,
                    "PE_OI": p_oi,
                    "PE_Gamma": p_gamma
                })

            df = pd.DataFrame(rows).sort_values("Strike").reset_index(drop=True)

            # PCR Ratio
            pcr = round(total_pe_oi / total_ce_oi, 2) if total_ce_oi > 0 else 1.0

            # Filter Near ATM Strikes
            if underlying_spot > 0:
                atm_idx = (df["Strike"] - underlying_spot).abs().idxmin()
                start_idx = max(0, atm_idx - range_strikes)
                end_idx = min(len(df), atm_idx + range_strikes + 1)
                near_df = df.iloc[start_idx:end_idx].copy()
            else:
                near_df = df.copy()

            # --- Gamma Blast Quantitative Engine ---
            blast_alerts = []
            for _, r in near_df.iterrows():
                strike = int(r["Strike"])

                # 1. CE Short Covering Gamma Blast (Upside Spike)
                # Shart: CE Writers exit kar rahe hain (-ve OI change), Volume > OI, aur Strike Spot ke kareeb hai
                ce_unwinding_pct = (abs(r["CE_OI_Chg"]) / (r["CE_OI"] + abs(r["CE_OI_Chg"]))) * 100 if (r["CE_OI"] + abs(r["CE_OI_Chg"])) > 0 else 0
                if (r["CE_OI_Chg"] < -100000) and (ce_unwinding_pct >= 15) and (r["CE_Vol"] > r["CE_OI"] * 1.2) and (r["CE_LTP"] > 10):
                    intensity = "ULTRA HIGH" if r["CE_Vol"] > r["CE_OI"] * 2 else "HIGH"
                    blast_alerts.append({
                        "Type": "🚀 CE GAMMA BLAST (BULLISH SPIKE)",
                        "Option": f"{strike} CE",
                        "LTP": r["CE_LTP"],
                        "Unwound Contracts": f"{abs(r['CE_OI_Chg']):,}",
                        "Volume Surge": f"{r['CE_Vol']:,}",
                        "Intensity": intensity,
                        "Trigger Logic": "Call Writers Panic Exit + Aggressive Market Buys"
                    })

                # 2. PE Short Covering Gamma Blast (Downside Crash)
                # Shart: PE Writers exit kar rahe hain (-ve OI change), Volume > OI
                pe_unwinding_pct = (abs(r["PE_OI_Chg"]) / (r["PE_OI"] + abs(r["PE_OI_Chg"]))) * 100 if (r["PE_OI"] + abs(r["PE_OI_Chg"])) > 0 else 0
                if (r["PE_OI_Chg"] < -100000) and (pe_unwinding_pct >= 15) and (r["PE_Vol"] > r["PE_OI"] * 1.2) and (r["PE_LTP"] > 10):
                    intensity = "ULTRA HIGH" if r["PE_Vol"] > r["PE_OI"] * 2 else "HIGH"
                    blast_alerts.append({
                        "Type": "🔥 PE GAMMA BLAST (BEARISH CRASH)",
                        "Option": f"{strike} PE",
                        "LTP": r["PE_LTP"],
                        "Unwound Contracts": f"{abs(r['PE_OI_Chg']):,}",
                        "Volume Surge": f"{r['PE_Vol']:,}",
                        "Intensity": intensity,
                        "Trigger Logic": "Put Writers Trap + Heavy Panic Liquidation"
                    })

            # --- Dashboard KPIs ---
            kpi1, kpi2, kpi3 = st.columns(3)
            kpi1.metric("NIFTY 50 Spot", f"₹{underlying_spot:,.2f}")
            kpi2.metric("Overall PCR", f"{pcr} ({'Bullish' if pcr > 1.2 else 'Bearish' if pcr < 0.8 else 'Neutral'})")
            kpi3.metric("Selected Expiry", selected_expiry)

            # Alerts Display
            if blast_alerts:
                st.subheader(f"🚨 Active Gamma Blast Setups ({len(blast_alerts)})")
                adf = pd.DataFrame(blast_alerts)
                st.dataframe(adf, use_container_width=True, hide_index=True)

                for alert in blast_alerts:
                    alert_key = f"tg_{selected_expiry}_{alert['Option']}_{datetime.now().strftime('%H%M')}"
                    if alert_key not in st.session_state:
                        msg = (
                            f"⚡ *NIFTY GAMMA BLAST RADAR*\n"
                            f"Expiry: *{selected_expiry}* | Spot: ₹{underlying_spot:,.1f}\n"
                            f"Setup: *{alert['Type']}*\n"
                            f"Contract: *{alert['Option']}* (LTP: ₹{alert['LTP']})\n"
                            f"🔥 Intensity: *{alert['Intensity']}*\n"
                            f"Panic Unwinding: {alert['Unwound Contracts']} contracts\n"
                            f"Volume: {alert['Volume Surge']}\n"
                            f"Note: {alert['Trigger Logic']}"
                        )
                        send_tg(msg)
                        st.session_state[alert_key] = True
            else:
                st.info("ℹ️ Kisi bhi ATM strike par abhi aggressive panic unwinding trigger nahi hui hai.")

            # Formatted Option Chain Matrix Table
            st.subheader("📊 Near-the-Money Option Chain Matrix")
            styled_df = near_df[[
                "CE_OI", "CE_OI_Chg", "CE_Vol", "CE_LTP", "Strike", 
                "PE_LTP", "PE_Vol", "PE_OI_Chg", "PE_OI"
            ]].copy()

            def highlight_unwinding(val):
                if isinstance(val, (int, float)) and val < -50000:
                    return 'background-color: #ffcccc; color: #900;'
                return ''

            st.dataframe(
                styled_df.style.map(highlight_unwinding, subset=['CE_OI_Chg', 'PE_OI_Chg']),
                use_container_width=True,
                hide_index=True
            )
