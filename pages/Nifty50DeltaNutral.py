import gzip
import io
import time
from datetime import datetime
from typing import Dict, List, Optional, Tuple
import pandas as pd
import requests
import streamlit as st
import upstox_client
from upstox_client.rest import ApiException


# =====================================================================
# 1. UPSTOX CONTRACT MASTER FETCHER & CACHE
# =====================================================================
@st.cache_data(ttl=3600 * 6)  # Cache 6 hours ke liye taaki bar-bar download na ho
def load_nifty_options_contract_master() -> pd.DataFrame:
    """Upstox ke official complete instrument master se Nifty F&O contracts parse karta hai."""
    url = "https://assets.upstox.com/market-quote/instruments/exchange/complete.json.gz"
    headers = {"User-Agent": "Mozilla/5.0"}
    res = requests.get(url, headers=headers, timeout=15)
    res.raise_for_status()

    # Gzip decompress and load to DataFrame
    with gzip.GzipFile(fileobj=io.BytesIO(res.content)) as f:
        df = pd.read_json(f)

    # Filter strictly for NSE F&O Nifty Options
    fo_df = df[
        (df["exchange"] == "NSE_FO")
        & (df["instrument_type"].isin(["CE", "PE"]))
        & (df["name"] == "NIFTY")
    ].copy()

    # Clean date columns
    fo_df["expiry"] = pd.to_datetime(fo_df["expiry"]).dt.date
    fo_df["strike"] = fo_df["strike"].astype(float)

    return fo_df[[
        "instrument_key",
        "trading_symbol",
        "strike",
        "instrument_type",
        "expiry",
        "lot_size",
        "tick_size",
    ]]


def get_target_nifty_contracts(
    contract_df: pd.DataFrame, target_strikes: Dict[str, float]
) -> Dict[str, dict]:
    """Nearest active expiry ke base par required strikes ki exact details return karta hai.

    target_strikes format: {'buy_ce': 25500, 'buy_pe': 24500, 'sell_ce': 25200,
    'sell_pe': 24800}
    """
    today = datetime.now().date()
    future_expiries = sorted(
        [exp for exp in contract_df["expiry"].unique() if exp >= today]
    )

    if not future_expiries:
        raise ValueError("Koi valid active expiry nahi mili!")

    nearest_expiry = future_expiries[0]
    expiry_slice = contract_df[contract_df["expiry"] == nearest_expiry]

    resolved_keys = {}
    mapping = {
        "buy_ce": (target_strikes["buy_ce"], "CE"),
        "buy_pe": (target_strikes["buy_pe"], "PE"),
        "sell_ce": (target_strikes["sell_ce"], "CE"),
        "sell_pe": (target_strikes["sell_pe"], "PE"),
    }

    for leg_name, (strike, opt_type) in mapping.items():
        matched = expiry_slice[
            (expiry_slice["strike"] == float(strike))
            & (expiry_slice["instrument_type"] == opt_type)
        ]
        if matched.empty:
            raise KeyError(
                f"Instrument nahi mila: Strike {strike} {opt_type} for expiry {nearest_expiry}"
            )

        row = matched.iloc[0]
        resolved_keys[leg_name] = {
            "instrument_key": row["instrument_key"],
            "trading_symbol": row["trading_symbol"],
            "lot_size": int(row["lot_size"]),
            "tick_size": float(row["tick_size"]),
            "expiry": str(nearest_expiry),
            "strike": strike,
            "type": opt_type,
        }

    return resolved_keys


# =====================================================================
# 2. LIVE QUOTE & MARKET DEPTH FETCHER
# =====================================================================
def get_live_market_depth(
    instrument_keys: List[str], access_token: str
) -> Dict[str, dict]:
    """Upstox Market Quote API se real Bid/Ask fetch karta hai taaki limit order reject na ho."""
    config = upstox_client.Configuration()
    config.access_token = access_token
    api_client = upstox_client.ApiClient(config)
    market_api = upstox_client.MarketQuoteApi(api_client)

    joined_keys = ",".join(instrument_keys)
    res = market_api.get_full_market_quote(joined_keys, "2.0")

    depth_data = {}
    for key, quote in res.data.items():
        best_bid = quote.depth.buy[0].price if quote.depth.buy else quote.last_price
        best_ask = (
            quote.depth.sell[0].price if quote.depth.sell else quote.last_price
        )
        depth_data[key] = {
            "ltp": float(quote.last_price),
            "best_bid": float(best_bid),
            "best_ask": float(best_ask),
        }
    return depth_data


# =====================================================================
# 3. VERIFIED ORDER PLACEMENT & STATUS CHECK
# =====================================================================
def place_and_verify_order(
    api_client: upstox_client.ApiClient,
    instrument_key: str,
    symbol: str,
    side: str,
    qty: int,
    limit_price: float,
    max_wait_sec: int = 5,
) -> Tuple[bool, str, float]:
    """Order place karta hai aur order book verify karta hai ki complete fill hua ya nahi."""
    order_api = upstox_client.OrderApi(api_client)

    tick_aligned_price = round(round(limit_price / 0.05) * 0.05, 2)

    payload = upstox_client.PlaceOrderRequest(
        quantity=qty,
        product="I",  # Intraday (MIS)
        validity="DAY",
        price=tick_aligned_price,
        tag="ShieldAlgo",
        instrument_token=instrument_key,
        order_type="LIMIT",
        transaction_type=side.upper(),
        disclosed_quantity=0,
        trigger_price=0.0,
        is_amo=False,
    )

    try:
        resp = order_api.place_order(payload, "2.0")
        order_id = resp.data.order_id
    except ApiException as e:
        return False, f"API_REJECTED: {e.body}", 0.0

    # Verification Loop: Order book check
    start_t = time.time()
    while time.time() - start_t < max_wait_sec:
        time.sleep(0.5)
        history = order_api.get_order_history(order_id=order_id)
        if history and history.data:
            latest_status = history.data[0].status
            if latest_status == "complete":
                avg_price = float(history.data[0].average_price)
                return True, order_id, avg_price
            elif latest_status in ["rejected", "cancelled"]:
                reason = history.data[0].status_message
                return False, f"{latest_status.upper()}: {reason}", 0.0

    return (
        False,
        f"TIMEOUT_PENDING: Order {order_id} limit par open reh gaya",
        tick_aligned_price,
    )


# =====================================================================
# 4. HEDGE-FIRST SEQUENTIAL EXECUTION PIPELINE
# =====================================================================
def execute_shield_strategy_live(
    access_token: str,
    contract_master_df: pd.DataFrame,
    target_strikes: Dict[str, float],
    sets: int,
) -> Tuple[bool, str, dict]:
    """Sequence: 1. Buy CE & Buy PE (Hedge) -> 2. Verify fills -> 3. Sell CE & Sell PE (Shorts)"""
    config = upstox_client.Configuration()
    config.access_token = access_token
    api_client = upstox_client.ApiClient(config)

    # 1. Resolve Instrument Keys
    try:
        contracts = get_target_nifty_contracts(
            contract_master_df, target_strikes
        )
    except Exception as e:
        return False, f"Contract Mapping Error: {str(e)}", {}

    # 2. Get Live Depth for All 4 Contracts
    all_keys = [c["instrument_key"] for c in contracts.values()]
    depths = get_live_market_depth(all_keys, access_token)

    lot_size = contracts["buy_ce"]["lot_size"]
    buy_qty = int(sets * 2 * lot_size)
    sell_qty = int(sets * 1 * lot_size)

    execution_journal = {}

    # -------------------------------------------------------------
    # PHASE A: HEDGE EXECUTION (BUY LEGS FIRST - MARGIN LOCK)
    # -------------------------------------------------------------
    # Market order avoid karne ke liye Best Ask se halka sa aage (0.10 buffer) limit lagate hain taaki fill ho jaye
    buy_legs = [("buy_ce", "BUY", buy_qty), ("buy_pe", "BUY", buy_qty)]

    for leg_id, side, qty in buy_legs:
        meta = contracts[leg_id]
        key = meta["instrument_key"]
        best_ask = depths[key]["best_ask"]
        limit_px = best_ask + 0.15  # Slippage protection buffer

        success, order_info, fill_px = place_and_verify_order(
            api_client, key, meta["trading_symbol"], side, qty, limit_px
        )

        if not success:
            # Agar hedge hi buy nahi hua toh turant abort karo taaki naked position open na ho
            return (
                False,
                f"ABORTED AT HEDGE PHASE ({leg_id}): {order_info}. No shorts were fired.",
                execution_journal,
            )

        execution_journal[leg_id] = {
            "symbol": meta["trading_symbol"],
            "fill_price": fill_px,
            "order_id": order_info,
            "qty": qty,
        }

    # -------------------------------------------------------------
    # PHASE B: SHORT EXECUTION (SELL LEGS ONLY AFTER HEDGES ACTIVE)
    # -------------------------------------------------------------
    sell_legs = [("sell_ce", "SELL", sell_qty), ("sell_pe", "SELL", sell_qty)]

    for leg_id, side, qty in sell_legs:
        meta = contracts[leg_id]
        key = meta["instrument_key"]
        best_bid = depths[key]["best_bid"]
        limit_px = max(0.05, best_bid - 0.15)  # Quick execution buffer

        success, order_info, fill_px = place_and_verify_order(
            api_client, key, meta["trading_symbol"], side, qty, limit_px
        )

        if not success:
            # Short fail hua toh hedge already bought hai (Safe position, no naked short risk)
            return (
                False,
                f"CRITICAL WARNING: Short leg ({leg_id}) failed: {order_info}. Hedges are already open!",
                execution_journal,
            )

        execution_journal[leg_id] = {
            "symbol": meta["trading_symbol"],
            "fill_price": fill_px,
            "order_id": order_info,
            "qty": qty,
        }

    return (
        True,
        "All 4 legs successfully executed in Hedge-First sequence!",
        execution_journal,
              )
                
