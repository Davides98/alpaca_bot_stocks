"""
Modulo 2 - Market Regime Detector (stesso schema del bot opzioni).

Classifica un titolo su due assi:
- Trend: BULLISH / BEARISH / SIDEWAYS (EMA20, EMA50, ADX14 di Wilder)
- Volatilità: HIGH_VOL / LOW_VOL (Vol Rank: HV20 attuale nel range dell'ultimo anno)

Gli indicatori sono calcolati sulle barre daily COMPLETE; il prezzo usato per i segnali è quello
in tempo reale (ultimo trade), se disponibile.
"""

from __future__ import annotations

from enum import Enum

import numpy as np
import pandas as pd
from pydantic import BaseModel

from src.analysis import indicators as ind


class Trend(str, Enum):
    BULLISH = "BULLISH"
    BEARISH = "BEARISH"
    SIDEWAYS = "SIDEWAYS"


class VolRegime(str, Enum):
    HIGH_VOL = "HIGH_VOL"
    LOW_VOL = "LOW_VOL"


class MarketRegime(BaseModel):
    ticker: str
    price: float  # prezzo attuale (ultimo trade o ultima chiusura)
    last_close: float
    ema_fast: float
    ema_slow: float
    adx: float
    atr: float
    rsi: float
    bb_mid: float
    bb_upper: float
    bb_lower: float
    donchian_high: float  # massimo delle ultime N barre complete
    donchian_low: float
    hv_current: float
    hv_min: float
    hv_max: float
    vol_rank: float
    avg_dollar_volume: float
    trend: Trend
    volatility: VolRegime

    @property
    def atr_pct(self) -> float:
        return self.atr / self.price if self.price > 0 else 0.0

    def describe(self) -> str:
        return (
            f"{self.ticker}: prezzo {self.price:.2f} | EMA20 {self.ema_fast:.2f} vs EMA50 {self.ema_slow:.2f} | "
            f"ADX {self.adx:.1f} -> {self.trend.value} | HV20 {self.hv_current:.1%} in "
            f"[{self.hv_min:.1%}-{self.hv_max:.1%}] -> VolRank {self.vol_rank:.0f} -> {self.volatility.value} | "
            f"ATR {self.atr:.2f} ({self.atr_pct:.1%}) | RSI {self.rsi:.0f} | "
            f"BB [{self.bb_lower:.2f} / {self.bb_mid:.2f} / {self.bb_upper:.2f}] | "
            f"Donchian [{self.donchian_low:.2f} - {self.donchian_high:.2f}]"
        )


def classify_trend(price: float, ema_fast: float, ema_slow: float, adx_value: float, adx_threshold: float = 20.0) -> Trend:
    if adx_value < adx_threshold:
        return Trend.SIDEWAYS
    if ema_fast > ema_slow and price > ema_fast:
        return Trend.BULLISH
    if ema_fast < ema_slow and price < ema_fast:
        return Trend.BEARISH
    # ADX alto ma segnali EMA/prezzo contrastanti: nessun trend affidabile
    return Trend.SIDEWAYS


def classify_volatility(vol_rank_value: float, threshold: float = 50.0) -> VolRegime:
    return VolRegime.HIGH_VOL if vol_rank_value >= threshold else VolRegime.LOW_VOL


def detect_regime(
    ticker: str,
    bars: pd.DataFrame,
    price: float | None = None,
    *,
    ema_fast_period: int = 20,
    ema_slow_period: int = 50,
    adx_period: int = 14,
    adx_threshold: float = 20.0,
    atr_period: int = 14,
    rsi_period: int = 14,
    hv_window: int = 20,
    vol_rank_threshold: float = 50.0,
    bb_period: int = 20,
    bb_std: float = 2.0,
    breakout_lookback: int = 20,
) -> MarketRegime:
    """Calcola il regime dalle barre daily complete (colonne high/low/close/volume) e dal prezzo attuale."""
    min_bars = max(ema_slow_period + adx_period, bb_period, breakout_lookback, hv_window + 2)
    if len(bars) < min_bars:
        raise ValueError(f"{ticker}: storico insufficiente ({len(bars)} barre, servono almeno {min_bars})")

    close, high, low = bars["close"].astype(float), bars["high"].astype(float), bars["low"].astype(float)
    last_close = float(close.iloc[-1])
    px = float(price) if price and price > 0 else last_close

    ema_f = float(ind.ema(close, ema_fast_period).iloc[-1])
    ema_s = float(ind.ema(close, ema_slow_period).iloc[-1])
    adx_value = float(ind.adx(high, low, close, adx_period).iloc[-1])
    atr_value = float(ind.atr(high, low, close, atr_period).iloc[-1])
    # RSI "live": l'ultima chiusura è sostituita dal prezzo attuale
    rsi_value = float(ind.rsi(pd.concat([close, pd.Series([px])], ignore_index=True), rsi_period).iloc[-1])
    mid, upper, lower = ind.bollinger(close, bb_period, bb_std)
    d_high, d_low = ind.donchian(high, low, breakout_lookback)

    hv = ind.historical_volatility(close, hv_window)
    hv_year = hv.dropna().tail(252)
    rank = ind.vol_rank(hv)

    if "volume" in bars:
        dollar_vol = float((bars["close"].astype(float) * bars["volume"].astype(float)).tail(20).mean())
    else:
        dollar_vol = 0.0

    return MarketRegime(
        ticker=ticker,
        price=px,
        last_close=last_close,
        ema_fast=ema_f,
        ema_slow=ema_s,
        adx=adx_value,
        atr=atr_value,
        rsi=rsi_value,
        bb_mid=float(mid.iloc[-1]),
        bb_upper=float(upper.iloc[-1]),
        bb_lower=float(lower.iloc[-1]),
        donchian_high=d_high,
        donchian_low=d_low,
        hv_current=float(hv_year.iloc[-1]) if not hv_year.empty else 0.0,
        hv_min=float(hv_year.min()) if not hv_year.empty else 0.0,
        hv_max=float(hv_year.max()) if not hv_year.empty else 0.0,
        vol_rank=rank,
        avg_dollar_volume=0.0 if np.isnan(dollar_vol) else dollar_vol,
        trend=classify_trend(px, ema_f, ema_s, adx_value, adx_threshold),
        volatility=classify_volatility(rank, vol_rank_threshold),
    )
