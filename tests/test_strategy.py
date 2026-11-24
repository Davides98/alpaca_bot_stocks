"""Unit test: indicatori, regime, matrice strategie, segnali, sizing, stop dinamici, esecuzione (nessuna chiamata di rete)."""

from __future__ import annotations

from datetime import date

import numpy as np
import pandas as pd
import pytest

from config.settings import Settings
from src.analysis import indicators as ind
from src.analysis.regime import MarketRegime, Trend, VolRegime, classify_trend, classify_volatility, detect_regime
from src.analysis.screener import compute_opportunity_score
from src.data.market_data import LiveQuote, drop_incomplete_bar
from src.execution.executor import price_ladder, worst_price
from src.execution.risk_manager import (
    next_stop,
    price_breach,
    r_multiple,
    realized_pnl,
    stop_hit_reason,
    voluntary_exit_reason,
)
from src.strategy.selector import (
    StrategySelector,
    StrategyType,
    choose_strategy,
    compute_levels,
    evaluate_signal,
    is_short,
    position_size,
)
from alpaca.trading.enums import OrderSide


@pytest.fixture
def s() -> Settings:
    return Settings(_env_file=None, ALPACA_API_KEY="x", ALPACA_SECRET_KEY="y")


def make_bars(closes: np.ndarray, spread: float = 1.0) -> pd.DataFrame:
    idx = pd.date_range("2025-01-01", periods=len(closes), freq="B", tz="UTC")
    closes = pd.Series(closes, index=idx)
    return pd.DataFrame({
        "open": closes.shift(1).fillna(closes.iloc[0]),
        "high": closes + spread,
        "low": closes - spread,
        "close": closes,
        "volume": 1_000_000.0,
    }, index=idx)


def regime(**kw) -> MarketRegime:
    base = dict(
        ticker="TEST", price=100.0, last_close=100.0, ema_fast=100.0, ema_slow=95.0, adx=30.0, atr=2.0, rsi=50.0,
        bb_mid=100.0, bb_upper=106.0, bb_lower=94.0, donchian_high=102.0, donchian_low=90.0,
        hv_current=0.2, hv_min=0.1, hv_max=0.4, vol_rank=40.0, avg_dollar_volume=1e9,
        trend=Trend.BULLISH, volatility=VolRegime.LOW_VOL,
    )
    base.update(kw)
    return MarketRegime(**base)


# ---------------------------------------------------------------------- #
# Indicatori
# ---------------------------------------------------------------------- #
def test_rsi_extremes():
    up = pd.Series(np.arange(1, 60, dtype=float))
    down = pd.Series(np.arange(60, 1, -1, dtype=float))
    flat = pd.Series(np.full(30, 10.0))
    assert ind.rsi(up).iloc[-1] == pytest.approx(100.0)
    assert ind.rsi(down).iloc[-1] < 1.0
    assert ind.rsi(flat).iloc[-1] == pytest.approx(50.0)


def test_atr_constant_range():
    n = 60
    close = pd.Series(np.full(n, 100.0))
    assert ind.atr(close + 2, close - 2, close, 14).iloc[-1] == pytest.approx(4.0)


def test_bollinger_and_donchian():
    close = pd.Series([10.0] * 19 + [30.0])
    mid, up, lo = ind.bollinger(close, 20, 2.0)
    assert mid.iloc[-1] == pytest.approx(11.0)
    std = np.std(close.values)  # popolazione
    assert up.iloc[-1] == pytest.approx(11.0 + 2 * std)
    assert lo.iloc[-1] == pytest.approx(11.0 - 2 * std)
    hi, low = ind.donchian(close + 1, close - 1, 5)
    assert (hi, low) == (31.0, 9.0)


def test_vol_rank_bounds():
    hv = pd.Series([0.1, 0.2, 0.3, 0.4])
    assert ind.vol_rank(hv) == pytest.approx(100.0)
    assert ind.vol_rank(pd.Series([0.4, 0.3, 0.1])) == pytest.approx(0.0)
    assert ind.vol_rank(pd.Series([0.2, 0.2])) == 50.0
    assert ind.vol_rank(pd.Series(dtype=float)) == 0.0


# ---------------------------------------------------------------------- #
# Regime
# ---------------------------------------------------------------------- #
def test_classify_trend_and_vol():
    assert classify_trend(110, 105, 100, 30) == Trend.BULLISH
    assert classify_trend(90, 95, 100, 30) == Trend.BEARISH
    assert classify_trend(110, 105, 100, 15) == Trend.SIDEWAYS
    assert classify_trend(101, 105, 100, 30) == Trend.SIDEWAYS  # prezzo sotto la EMA veloce
    assert classify_volatility(50) == VolRegime.HIGH_VOL
    assert classify_volatility(49.9) == VolRegime.LOW_VOL


def test_detect_regime_uptrend():
    rng = np.random.default_rng(1)
    closes = 100 + np.cumsum(0.5 + rng.normal(0, 0.3, 300))
    r = detect_regime("UP", make_bars(closes))
    assert r.trend == Trend.BULLISH
    assert r.ema_fast > r.ema_slow
    assert 0 <= r.vol_rank <= 100
    assert r.atr > 0 and r.bb_upper > r.bb_mid > r.bb_lower


def test_detect_regime_uses_live_price():
    closes = np.linspace(100, 150, 300)
    bars = make_bars(closes)
    r = detect_regime("X", bars, price=200.0)
    assert r.price == 200.0 and r.last_close == pytest.approx(150.0)
    assert r.price > r.donchian_high  # rottura del massimo a 20 giorni con il prezzo live


def test_detect_regime_requires_history():
    with pytest.raises(ValueError):
        detect_regime("X", make_bars(np.linspace(100, 110, 30)))


def test_drop_incomplete_bar():
    idx = pd.DatetimeIndex(["2026-11-20 05:00", "2026-11-23 05:00", "2026-11-24 05:00"], tz="UTC")
    df = pd.DataFrame({"close": [1.0, 2.0, 3.0]}, index=idx)
    out = drop_incomplete_bar(df, today_ny=date(2026, 11, 24))
    assert list(out["close"]) == [1.0, 2.0]


def test_live_quote_reference():
    tight = LiveQuote("A", last=100.3, bid=99.99, ask=100.01)
    wide = LiveQuote("A", last=100.3, bid=95.0, ask=105.0)
    one_side = LiveQuote("A", last=100.3, bid=None, ask=100.5)
    assert tight.reference(0.005) == pytest.approx(100.0)
    assert wide.reference(0.005) == 100.3
    assert one_side.reference(0.005) == 100.3


# ---------------------------------------------------------------------- #
# Matrice strategie
# ---------------------------------------------------------------------- #
@pytest.mark.parametrize("trend,vol,price,expected", [
    (Trend.BULLISH, VolRegime.LOW_VOL, 100, StrategyType.BREAKOUT_LONG),
    (Trend.BULLISH, VolRegime.HIGH_VOL, 100, StrategyType.PULLBACK_LONG),
    (Trend.BEARISH, VolRegime.LOW_VOL, 100, StrategyType.BREAKDOWN_SHORT),
    (Trend.BEARISH, VolRegime.HIGH_VOL, 100, StrategyType.RALLY_SHORT),
    (Trend.SIDEWAYS, VolRegime.HIGH_VOL, 95, StrategyType.MEAN_REVERSION_LONG),
    (Trend.SIDEWAYS, VolRegime.HIGH_VOL, 105, StrategyType.MEAN_REVERSION_SHORT),
    (Trend.SIDEWAYS, VolRegime.LOW_VOL, 95, StrategyType.NO_TRADE),
])
def test_strategy_matrix(trend, vol, price, expected):
    assert choose_strategy(trend, vol, price, bb_mid=100) == expected


def test_no_short_when_disabled():
    assert choose_strategy(Trend.BEARISH, VolRegime.LOW_VOL, 100, 100, allow_short=False) == StrategyType.NO_TRADE
    assert choose_strategy(Trend.BULLISH, VolRegime.LOW_VOL, 100, 100, allow_short=False) == StrategyType.BREAKOUT_LONG


# ---------------------------------------------------------------------- #
# Segnali
# ---------------------------------------------------------------------- #
def test_breakout_signal(s):
    assert not evaluate_signal(StrategyType.BREAKOUT_LONG, regime(price=101.0), s).triggered  # sotto il massimo
    fresh = evaluate_signal(StrategyType.BREAKOUT_LONG, regime(price=102.2), s)
    assert fresh.triggered and fresh.strength > 80
    extended = evaluate_signal(StrategyType.BREAKOUT_LONG, regime(price=104.0), s)  # 1 ATR oltre (max 0.75)
    assert not extended.triggered and "esteso" in extended.text


def test_breakdown_signal(s):
    r = regime(price=89.5, trend=Trend.BEARISH)
    assert evaluate_signal(StrategyType.BREAKDOWN_SHORT, r, s).triggered
    assert not evaluate_signal(StrategyType.BREAKDOWN_SHORT, regime(price=91.0), s).triggered


def test_pullback_signal(s):
    ok = regime(price=100.5, ema_fast=100.0, ema_slow=95.0, rsi=45.0, volatility=VolRegime.HIGH_VOL)
    assert evaluate_signal(StrategyType.PULLBACK_LONG, ok, s).triggered
    far = regime(price=104.0, ema_fast=100.0, rsi=45.0)  # 2 ATR sopra la EMA20
    assert not evaluate_signal(StrategyType.PULLBACK_LONG, far, s).triggered
    hot = regime(price=100.5, ema_fast=100.0, rsi=65.0)  # RSI ancora alto
    assert not evaluate_signal(StrategyType.PULLBACK_LONG, hot, s).triggered
    broken = regime(price=94.0, ema_fast=94.5, ema_slow=95.0, rsi=40.0)  # sotto la EMA50
    assert not evaluate_signal(StrategyType.PULLBACK_LONG, broken, s).triggered


def test_rally_short_signal(s):
    ok = regime(price=99.5, ema_fast=100.0, ema_slow=105.0, rsi=55.0, trend=Trend.BEARISH)
    assert evaluate_signal(StrategyType.RALLY_SHORT, ok, s).triggered
    weak = regime(price=99.5, ema_fast=100.0, ema_slow=105.0, rsi=40.0)
    assert not evaluate_signal(StrategyType.RALLY_SHORT, weak, s).triggered


def test_mean_reversion_signal(s):
    assert evaluate_signal(StrategyType.MEAN_REVERSION_LONG, regime(price=93.5), s).triggered
    assert not evaluate_signal(StrategyType.MEAN_REVERSION_LONG, regime(price=95.0), s).triggered
    assert evaluate_signal(StrategyType.MEAN_REVERSION_SHORT, regime(price=106.5), s).triggered


# ---------------------------------------------------------------------- #
# Score
# ---------------------------------------------------------------------- #
def test_opportunity_score():
    assert compute_opportunity_score(StrategyType.NO_TRADE, VolRegime.LOW_VOL, 10, 30, 100) == 0.0
    # Trend forte, vol compressa, segnale perfetto -> 100
    assert compute_opportunity_score(StrategyType.BREAKOUT_LONG, VolRegime.LOW_VOL, 0, 50, 100) == 100.0
    # Mean reversion: ADX basso è un vantaggio
    low_adx = compute_opportunity_score(StrategyType.MEAN_REVERSION_LONG, VolRegime.HIGH_VOL, 80, 10, 60)
    high_adx = compute_opportunity_score(StrategyType.MEAN_REVERSION_LONG, VolRegime.HIGH_VOL, 80, 19, 60)
    assert low_adx > high_adx


# ---------------------------------------------------------------------- #
# Livelli, sizing e piano
# ---------------------------------------------------------------------- #
def test_levels_trend_and_mean_reversion(s):
    stop, target, risk = compute_levels(StrategyType.BREAKOUT_LONG, regime(price=102.0), s)
    assert risk == pytest.approx(4.0) and stop == 98.0 and target == 114.0  # 2 ATR, 3R
    stop, target, risk = compute_levels(StrategyType.RALLY_SHORT, regime(price=100.0), s)
    assert stop == 103.0 and target == 91.0  # 1.5 ATR, 3R
    stop, target, _ = compute_levels(StrategyType.MEAN_REVERSION_LONG, regime(price=93.0, bb_mid=100.0), s)
    assert stop == 90.5 and target == 100.0
    # Media troppo vicina: reward 1 < 0.8 x 2.5 -> scartato
    assert compute_levels(StrategyType.MEAN_REVERSION_LONG, regime(price=99.0, bb_mid=100.0), s) is None


def test_position_size_limits():
    # rischio: 100k x 1% / 4 = 250 azioni; concentrazione: 20k / 100 = 200 -> 200
    assert position_size(100_000, 100, 4.0, 0.01, 0.20, 100_000) == 200
    # stop largo: 1000 / 20 = 50
    assert position_size(100_000, 100, 20.0, 0.01, 0.20, 100_000) == 50
    # poco spazio di esposizione: 5000 / 100 = 50
    assert position_size(100_000, 100, 4.0, 0.01, 0.20, 5_000) == 50
    # buying power
    assert position_size(100_000, 100, 4.0, 0.01, 0.20, 100_000, buying_power=1_000) == 9
    assert position_size(100_000, 100, 0.0, 0.01, 0.20, 100_000) == 0


def test_build_plan_and_fill_levels(s):
    r = regime(price=102.2)
    sig = evaluate_signal(StrategyType.BREAKOUT_LONG, r, s)
    plan = StrategySelector(s).build_plan(r, StrategyType.BREAKOUT_LONG, sig, 100_000, 100_000, 200_000)
    assert plan is not None and plan.side == "long" and plan.qty == 195  # 20k / 102.2
    assert plan.max_loss <= 100_000 * s.MAX_RISK_PER_TRADE_PCT + 1e-6
    stop, target = plan.levels_from_fill(102.5)
    assert stop == pytest.approx(98.5) and target == pytest.approx(114.5)
    assert is_short(StrategyType.RALLY_SHORT) and not is_short(StrategyType.PULLBACK_LONG)


# ---------------------------------------------------------------------- #
# Gestione posizioni
# ---------------------------------------------------------------------- #
def test_r_multiple_and_pnl():
    assert r_multiple(100, 104, 2, True) == 2.0
    assert r_multiple(100, 104, 2, False) == -2.0
    assert realized_pnl(100, 110, 10, True) == 100
    assert realized_pnl(100, 110, 10, False) == -100


def test_next_stop_breakeven_and_trailing(s):
    # +0.5R: nessun cambio
    assert next_stop(True, 100, 96, 102, 4, 2, True, s) is None
    # +1R: stop a breakeven
    assert next_stop(True, 100, 96, 104, 4, 2, True, s) == 100.0
    # +2.5R: trailing a 2.5 ATR sotto il prezzo (110 - 5 = 105)
    assert next_stop(True, 100, 100, 110, 4, 2, True, s) == 105.0
    # Mean reversion: niente trailing, solo breakeven
    assert next_stop(True, 100, 100, 110, 4, 2, False, s) is None
    # Mai indietro
    assert next_stop(True, 100, 106, 110, 4, 2, True, s) is None
    # Short: speculare
    assert next_stop(False, 100, 104, 96, 4, 2, True, s) == 100.0
    assert next_stop(False, 100, 100, 90, 4, 2, True, s) == 95.0


def test_exit_reasons(s):
    assert voluntary_exit_reason(True, 31, 30, None, s) == "TIME_STOP"
    assert voluntary_exit_reason(True, 3, 30, Trend.BEARISH, s) == "TREND_REVERSAL"
    assert voluntary_exit_reason(False, 3, 30, Trend.BULLISH, s) == "TREND_REVERSAL"
    assert voluntary_exit_reason(True, 3, 30, Trend.SIDEWAYS, s) is None
    assert price_breach(True, 95, 96, 110) == "STOP_LOSS"
    assert price_breach(True, 111, 96, 110) == "TAKE_PROFIT"
    assert price_breach(False, 105, 104, 90) == "STOP_LOSS"
    assert price_breach(True, 100, 96, 110) is None
    assert stop_hit_reason(True, 100, 99) == "STOP_LOSS"
    assert stop_hit_reason(True, 100, 100) == "TRAILING_STOP"
    assert stop_hit_reason(False, 100, 97) == "TRAILING_STOP"


# ---------------------------------------------------------------------- #
# Esecuzione
# ---------------------------------------------------------------------- #
def test_price_ladder_buy_and_sell():
    assert worst_price(100, OrderSide.BUY, 0.002) == 100.2
    assert worst_price(100, OrderSide.SELL, 0.002) == 99.8
    assert price_ladder(100, 100.2, 3) == [100.0, 100.1, 100.2]
    assert price_ladder(100, 99.8, 3) == [100.0, 99.9, 99.8]
    assert price_ladder(100, 100.01, 3) == [100.0, 100.01]  # niente duplicati
    assert price_ladder(100, 100.2, 1) == [100.0]


# ---------------------------------------------------------------------- #
# Configurazione
# ---------------------------------------------------------------------- #
def test_settings_parsing():
    st = Settings(_env_file=None, WATCHLIST="spy, qqq,SPY", MAX_RISK_PER_TRADE_PCT="1%", MAX_POSITION_PCT="25%")
    assert st.WATCHLIST == ["SPY", "QQQ"]
    assert st.MAX_RISK_PER_TRADE_PCT == pytest.approx(0.01)
    assert st.MAX_POSITION_PCT == pytest.approx(0.25)
    with pytest.raises(ValueError):
        Settings(_env_file=None, MAX_RISK_PER_TRADE_PCT=0.2)


def test_position_size_fractional():
    # 1.000 $: rischio 10 $ / 10,18 = 0,98 azioni; concentrazione 200 / 314,56 = 0,6358 -> 0,6358
    assert position_size(1_000, 314.56, 10.18, 0.01, 0.20, 1_000, 1_000, fractional=True) == pytest.approx(0.6358)
    assert position_size(1_000, 314.56, 10.18, 0.01, 0.20, 1_000, 1_000) == 0  # intere: nemmeno 1 azione
    assert position_size(1_000, 500.0, 10.0, 0.01, 0.20, 0.4, fractional=True) == 0  # sotto 1 $ di controvalore


def test_dynamic_targets(s):
    from src.execution.risk_manager import mr_dynamic_target, should_remove_target, target_changed
    # Long: la media scende da 100 a 97 -> target 97; mai sotto ingresso + 0,25R (90 + 0,75 = 90,75)
    assert mr_dynamic_target(True, 90.0, 3.0, 97.0, 0.25) == 97.0
    assert mr_dynamic_target(True, 90.0, 3.0, 89.0, 0.25) == 90.75
    # Short speculare: mai sopra ingresso - 0,25R
    assert mr_dynamic_target(False, 110.0, 3.0, 104.0, 0.25) == 104.0
    assert mr_dynamic_target(False, 110.0, 3.0, 111.0, 0.25) == 109.25
    # Soglia minima di spostamento: 5% dell'ATR
    assert not target_changed(100.0, 100.05, 2.0) and target_changed(100.0, 100.10, 2.0)
    # Trend: target tolto da +1,5R; mai per la mean reversion
    assert should_remove_target(True, 1.6, s) and not should_remove_target(True, 1.2, s)
    assert not should_remove_target(False, 3.0, s)
