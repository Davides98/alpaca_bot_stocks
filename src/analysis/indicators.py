"""
Indicatori tecnici (funzioni pure: pandas in -> pandas/numero out, facilmente testabili).

Smoothing di Wilder (alpha = 1/period) per ADX, ATR e RSI, come nelle piattaforme di trading.
"""

from __future__ import annotations

import numpy as np
import pandas as pd


def ema(series: pd.Series, period: int) -> pd.Series:
    return series.ewm(span=period, adjust=False).mean()


def sma(series: pd.Series, period: int) -> pd.Series:
    return series.rolling(period).mean()


def true_range(high: pd.Series, low: pd.Series, close: pd.Series) -> pd.Series:
    prev_close = close.shift(1)
    return pd.concat([high - low, (high - prev_close).abs(), (low - prev_close).abs()], axis=1).max(axis=1)


def atr(high: pd.Series, low: pd.Series, close: pd.Series, period: int = 14) -> pd.Series:
    """Average True Range di Wilder."""
    return true_range(high, low, close).ewm(alpha=1.0 / period, adjust=False).mean()


def adx(high: pd.Series, low: pd.Series, close: pd.Series, period: int = 14) -> pd.Series:
    """Average Directional Index con smoothing di Wilder."""
    up_move = high.diff()
    down_move = -low.diff()
    plus_dm = pd.Series(np.where((up_move > down_move) & (up_move > 0), up_move, 0.0), index=high.index)
    minus_dm = pd.Series(np.where((down_move > up_move) & (down_move > 0), down_move, 0.0), index=high.index)

    alpha = 1.0 / period
    atr_s = true_range(high, low, close).ewm(alpha=alpha, adjust=False).mean()
    plus_di = 100.0 * plus_dm.ewm(alpha=alpha, adjust=False).mean() / atr_s.replace(0, np.nan)
    minus_di = 100.0 * minus_dm.ewm(alpha=alpha, adjust=False).mean() / atr_s.replace(0, np.nan)
    di_sum = (plus_di + minus_di).replace(0, np.nan)
    dx = 100.0 * (plus_di - minus_di).abs() / di_sum
    return dx.ewm(alpha=alpha, adjust=False).mean().fillna(0.0)


def rsi(close: pd.Series, period: int = 14) -> pd.Series:
    """Relative Strength Index di Wilder (0-100)."""
    delta = close.diff()
    gain = delta.clip(lower=0.0).ewm(alpha=1.0 / period, adjust=False).mean()
    loss = (-delta.clip(upper=0.0)).ewm(alpha=1.0 / period, adjust=False).mean()
    rs = gain / loss.replace(0, np.nan)
    out = 100.0 - 100.0 / (1.0 + rs)
    # Nessuna perdita nel periodo -> RSI 100; nessun movimento -> 50
    out = out.where(loss != 0, np.where(gain > 0, 100.0, 50.0))
    return out.fillna(50.0)


def bollinger(close: pd.Series, period: int = 20, n_std: float = 2.0) -> tuple[pd.Series, pd.Series, pd.Series]:
    """Bande di Bollinger: (media, banda superiore, banda inferiore). Deviazione standard di popolazione."""
    mid = close.rolling(period).mean()
    std = close.rolling(period).std(ddof=0)
    return mid, mid + n_std * std, mid - n_std * std


def donchian(high: pd.Series, low: pd.Series, lookback: int = 20) -> tuple[float, float]:
    """Massimo e minimo delle ultime `lookback` barre (canale di Donchian)."""
    return float(high.tail(lookback).max()), float(low.tail(lookback).min())


def historical_volatility(close: pd.Series, window: int = 20, periods_per_year: int = 252) -> pd.Series:
    """Volatilità realizzata annualizzata (deviazione standard dei log-rendimenti)."""
    log_returns = np.log(close / close.shift(1))
    return log_returns.rolling(window).std() * np.sqrt(periods_per_year)


def vol_rank(hv_series: pd.Series, lookback: int = 252) -> float:
    """
    Volatility Rank (0-100): posizione della HV attuale nel range [min, max] dell'ultimo anno.
    È l'equivalente azionario dell'IV Rank usato dal bot opzioni, calcolato però su dati reali
    (la volatilità realizzata), quindi senza approssimazioni.
    """
    hv = hv_series.dropna().tail(lookback)
    if hv.empty:
        return 0.0
    lo, hi, now = float(hv.min()), float(hv.max()), float(hv.iloc[-1])
    if hi - lo <= 1e-12:
        return 50.0
    return float(np.clip((now - lo) / (hi - lo) * 100.0, 0.0, 100.0))
