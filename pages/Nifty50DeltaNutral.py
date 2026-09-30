import math
import numpy as np
from scipy.stats import norm
from scipy.optimize import brentq
from dataclasses import dataclass
from typing import Dict, Optional
import logging

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")


class RobustBlack76:
    @staticmethod
    def calc_d1_d2(F: float, K: float, T: float, sigma: float):
        if T <= 1e-5 or sigma <= 1e-4:
            return 0.0, 0.0
        vt = sigma * math.sqrt(T)
        d1 = (math.log(F / K) + 0.5 * (sigma ** 2) * T) / vt
        d2 = d1 - vt
        return d1, d2

    @classmethod
    def price(cls, F: float, K: float, T: float, r: float, sigma: float, option_type: str) -> float:
        if T <= 1e-5:
            return max(0.0, (F - K) if option_type.upper() == "CE" else (K - F))
        d1, d2 = cls.calc_d1_d2(F, K, T, sigma)
        df = math.exp(-r * T)
        if option_type.upper() == "CE":
            return df * (F * norm.cdf(d1) - K * norm.cdf(d2))
        else:
            return df * (K * norm.cdf(-d2) - F * norm.cdf(-d1))

    @classmethod
    def implied_volatility(cls, ltp: float, F: float, K: float, T: float, r: float, option_type: str, fallback_iv: float = 0.16) -> float:
        if T <= 1e-5:
            return 0.0
        df = math.exp(-r * T)
        intrinsic = max(0.0, (F - K) if option_type.upper() == "CE" else (K - F)) * df
        
        # आर्बिट्राज/डर्टी टिक प्रोटेक्शन
        if ltp <= intrinsic:
            return max(0.05, fallback_iv)

        def objective(sigma):
            return cls.price(F, K, T, r, sigma, option_type) - ltp

        try:
            # ब्रॉड बाउंड्स [1%, 400%]
            return brentq(objective, 0.01, 4.0, maxiter=80, xtol=1e-4)
        except (ValueError, RuntimeError):
            return fallback_iv  # पिछले लेग का IV इस्तेमाल करें

    @classmethod
    def delta(cls, F: float, K: float, T: float, r: float, sigma: float, option_type: str) -> float:
        if T <= 1e-5 or sigma <= 1e-4:
            # एक्सपायरी पर बाइनरी डेल्टा
            if option_type.upper() == "CE":
                return 1.0 if F > K else 0.0
            else:
                return -1.0 if F < K else 0.0
        d1, _ = cls.calc_d1_d2(F, K, T, sigma)
        df = math.exp(-r * T)
        return (df * norm.cdf(d1)) if option_type.upper() == "CE" else (-df * norm.cdf(-d1))


@dataclass
class PositionLeg:
    symbol: str
    strike: float
    option_type: str       # 'CE', 'PE'
    expiry_years: float    # T = DTE / 365.25
    quantity: int          # +ve for Buy, -ve for Sell
    entry_price: float
    current_iv: float = 0.16
    current_delta: float = 0.0


class InstitutionalDeltaEngine:
    def __init__(
        self,
        lot_size: int = 75,
        risk_free_rate: float = 0.065,
        delta_band_lots: float = 0.5,     # 0.5 लॉट से ज्यादा डेल्टा ड्रिफ्ट होने पर हेज
        max_drawdown_limit: float = 50000.0,
        exchange_freeze_qty: int = 1800   # NSE Slice Limit
    ):
        self.lot_size = lot_size
        self.r = risk_free_rate
        self.delta_band = delta_band_lots * lot_size
        self.max_dd = max_drawdown_limit
        self.freeze_limit = exchange_freeze_qty

        self.legs: Dict[str, PositionLeg] = {}
        self.futures_hedge_qty: int = 0
        self.is_halted: bool = False

    def register_leg(self, leg: PositionLeg):
        self.legs[leg.symbol] = leg
        logging.info(f"Registered Leg: {leg.symbol} | Qty: {leg.quantity} | Strike: {leg.strike}")

    def sync_tick(self, future_ltp: float, ticks: Dict[str, float]) -> float:
        """
        लाइव टिक्स प्रोसेस करता है और पोर्टफोलियो ग्रीक्स व पीएनएल अपडेट करता है।
        """
        if self.is_halted:
            logging.error("Execution Engine HALTED due to Risk Trigger.")
            return 0.0

        net_delta = float(self.futures_hedge_qty)  # फ्यूचर का डेल्टा 1.0
        total_pnl = 0.0

        for symbol, leg in self.legs.items():
            ltp = ticks.get(symbol, leg.entry_price)
            total_pnl += (ltp - leg.entry_price) * leg.quantity

            # 1. डायनामिक IV कैलकुलेशन (विथ फॉलबैक)
            leg.current_iv = RobustBlack76.implied_volatility(
                ltp=ltp, F=future_ltp, K=leg.strike, T=leg.expiry_years,
                r=self.r, option_type=leg.option_type, fallback_iv=leg.current_iv
            )

            # 2. सटीक डेल्टा कैलकुलेशन
            unit_delta = RobustBlack76.delta(
                F=future_ltp, K=leg.strike, T=leg.expiry_years,
                r=self.r, sigma=leg.current_iv, option_type=leg.option_type
            )
            leg.current_delta = unit_delta
            net_delta += unit_delta * leg.quantity

        # रिस्क चेक (ड्रॉडाउन सर्किट ब्रेकर)
        if total_pnl <= -self.max_dd:
            self._emergency_kill_switch(total_pnl)
            return 0.0

        # डेल्टा बैंड वायलेशन चेक
        self._check_and_hedge(net_delta, future_ltp)
        return net_delta

    def _check_and_hedge(self, net_delta: float, current_future_price: float):
        """
        बैंड-बेस्ड हिस्टेरेसिस हेजिंग और स्लाइसिंग इंजन।
        """
        if abs(net_delta) > self.delta_band:
            # नियरेस्ट लॉट साइज में राउंडिंग
            needed_hedge = -int(round(net_delta / self.lot_size) * self.lot_size)
            
            if needed_hedge != 0:
                self._route_smart_order(needed_hedge, current_future_price)

    def _route_smart_order(self, total_qty: int, price: float):
        side = "BUY" if total_qty > 0 else "SELL"
        remaining = abs(total_qty)

        logging.warning(f"--- HEDGE TRIGGERED: {side} {remaining} NIFTY FUTURES @ ~{price:.2f} ---")

        # NSE Freeze Limit Slicing
        while remaining > 0:
            slice_qty = min(remaining, self.freeze_limit)
            self._execute_sliced_limit_order(side, slice_qty, price)
            remaining -= slice_qty

        self.futures_hedge_qty += total_qty
        logging.info(f"Hedge Complete. New Net Futures Position: {self.futures_hedge_qty}")

    def _execute_sliced_limit_order(self, side: str, qty: int, benchmark_price: float):
        # प्रोडक्शन में यहाँ Broker API (जैसे KiteConnect.place_order) कॉल होगी
        logging.info(f"Executed Order Slice: {side} {qty} Qty with Limit IOC at {benchmark_price:.2f}")

    def _emergency_kill_switch(self, pnl: float):
        self.is_halted = True
        logging.critical(f"CIRCUIT BREAKER HIT: PnL reached ₹{pnl:.2f}. Canceling all orders and squaring off!")


# =====================================================================
# VERIFICATION HARNESS
# =====================================================================
if __name__ == "__main__":
    nifty_fut = 24800.0
    dte = 3 / 365.25  # 3 दिन एक्सपायरी
    lot = 75

    engine = InstitutionalDeltaEngine(lot_size=lot, delta_band_lots=0.5, max_drawdown_limit=40000)

    # Short Straddle: 24800 CE & PE (150-150 Qty)
    ce = PositionLeg("NIFTY24800CE", 24800, "CE", dte, -150, entry_price=120.0)
    pe = PositionLeg("NIFTY24800PE", 24800, "PE", dte, -150, entry_price=118.0)
    engine.register_leg(ce)
    engine.register_leg(pe)

    # टिक 1: स्टेबल
    ticks_normal = {"NIFTY24800CE": 120.0, "NIFTY24800PE": 118.0}
    d1 = engine.sync_tick(nifty_fut, ticks_normal)
    print(f"Tick 1 Net Delta: {d1:.2f}")

    # टिक 2: 120 पॉइंट्स का मार्केट गैप-अप
    ticks_spiked = {"NIFTY24800CE": 195.0, "NIFTY24800PE": 55.0}
    d2 = engine.sync_tick(nifty_fut + 120, ticks_spiked)
    print(f"Tick 2 Net Delta (Post-Rebalance Trigger): {d2:.2f}")
      
