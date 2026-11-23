"""
Modulo 4 - Strategy Selector, segnali di ingresso e Trade Plan.

Matrice decisionale deterministica (Trend x Volatilità), "gemella" di quella del bot opzioni:

    | Trend     | HIGH_VOL                 | LOW_VOL          |
    |-----------|--------------------------|------------------|
    | SIDEWAYS  | Mean Reversion (bande)   | NO_TRADE         |
    | BULLISH   | Pullback Long            | Breakout Long    |
    | BEARISH   | Rally Short              | Breakdown Short  |

Corrispondenza con le opzioni:
- HIGH_VOL  ~ strategie a CREDITO (Bull Put / Bear Call / Iron Condor): si "vende" l'eccesso, cioè si
  entra contro il movimento di breve (pullback in un trend, estremi del range) e si sfrutta il ritorno.
- LOW_VOL   ~ strategie a DEBITO (Bull Call / Bear Put): si "compra" il movimento, cioè il breakout
  dopo una fase di compressione della volatilità.
- SIDEWAYS + LOW_VOL: nessun edge, come nel bot opzioni.

Ogni strategia ha un trigger preciso: il regime dice COSA fare, il segnale dice QUANDO.
"""

from __future__ import annotations

import math
from enum import Enum

import numpy as np
from loguru import logger
from pydantic import BaseModel

from config.settings import Settings
from src.analysis.regime import MarketRegime, Trend, VolRegime


class StrategyType(str, Enum):
    BREAKOUT_LONG = "BREAKOUT_LONG"
    PULLBACK_LONG = "PULLBACK_LONG"
    BREAKDOWN_SHORT = "BREAKDOWN_SHORT"
    RALLY_SHORT = "RALLY_SHORT"
    MEAN_REVERSION_LONG = "MEAN_REVERSION_LONG"
    MEAN_REVERSION_SHORT = "MEAN_REVERSION_SHORT"
    NO_TRADE = "NO_TRADE"


SHORT_STRATEGIES = {StrategyType.BREAKDOWN_SHORT, StrategyType.RALLY_SHORT, StrategyType.MEAN_REVERSION_SHORT}
TREND_STRATEGIES = {StrategyType.BREAKOUT_LONG, StrategyType.PULLBACK_LONG, StrategyType.BREAKDOWN_SHORT, StrategyType.RALLY_SHORT}
MEAN_REVERSION_STRATEGIES = {StrategyType.MEAN_REVERSION_LONG, StrategyType.MEAN_REVERSION_SHORT}


def is_short(strategy: StrategyType | str) -> bool:
    return StrategyType(strategy) in SHORT_STRATEGIES


def is_trend_strategy(strategy: StrategyType | str) -> bool:
    try:
        return StrategyType(strategy) in TREND_STRATEGIES
    except ValueError:
        return True  # strategia sconosciuta (posizione aperta a mano): regole dei trade di trend


def choose_strategy(trend: Trend, vol: VolRegime, price: float, bb_mid: float, allow_short: bool = True) -> StrategyType:
    """Applica la matrice decisionale. In laterale il lato dipende da dove si trova il prezzo rispetto alla media."""
    if trend == Trend.SIDEWAYS:
        if vol == VolRegime.LOW_VOL:
            return StrategyType.NO_TRADE
        strategy = StrategyType.MEAN_REVERSION_LONG if price <= bb_mid else StrategyType.MEAN_REVERSION_SHORT
    elif trend == Trend.BULLISH:
        strategy = StrategyType.PULLBACK_LONG if vol == VolRegime.HIGH_VOL else StrategyType.BREAKOUT_LONG
    else:
        strategy = StrategyType.RALLY_SHORT if vol == VolRegime.HIGH_VOL else StrategyType.BREAKDOWN_SHORT
    if strategy in SHORT_STRATEGIES and not allow_short:
        return StrategyType.NO_TRADE
    return strategy


# ---------------------------------------------------------------------- #
# Segnali di ingresso
# ---------------------------------------------------------------------- #
class Signal(BaseModel):
    triggered: bool
    strength: float = 0.0  # 0-100: qualità del setup (entra nell'Opportunity Score)
    text: str = ""


def _clip(x: float) -> float:
    return float(np.clip(x, 0.0, 100.0))


def evaluate_signal(strategy: StrategyType, r: MarketRegime, s: Settings) -> Signal:
    """Verifica il trigger della strategia sul prezzo attuale."""
    price, atr = r.price, r.atr
    if atr <= 0:
        return Signal(triggered=False, text="ATR non valido")

    if strategy in (StrategyType.BREAKOUT_LONG, StrategyType.BREAKDOWN_SHORT):
        max_ext = s.BREAKOUT_MAX_EXTENSION_ATR * atr
        if strategy == StrategyType.BREAKOUT_LONG:
            level, ext, word = r.donchian_high, price - r.donchian_high, "massimo"
        else:
            level, ext, word = r.donchian_low, r.donchian_low - price, "minimo"
        if ext <= 0:
            return Signal(triggered=False, text=f"prezzo {price:.2f} non ha rotto il {word} a {s.BREAKOUT_LOOKBACK}g ({level:.2f})")
        if ext > max_ext:
            return Signal(triggered=False, text=f"breakout già esteso ({ext / atr:.2f} ATR oltre {level:.2f}, max {s.BREAKOUT_MAX_EXTENSION_ATR:g})")
        # Un breakout fresco (vicino al livello) vale di più: meno rincorsa, stop più sensato
        return Signal(triggered=True, strength=_clip(100.0 * (1.0 - ext / max_ext) * 0.6 + 40.0),
                      text=f"rottura del {word} a {s.BREAKOUT_LOOKBACK}g ({level:.2f}) di {ext / atr:.2f} ATR")

    if strategy in (StrategyType.PULLBACK_LONG, StrategyType.RALLY_SHORT):
        dist = abs(price - r.ema_fast)
        max_dist = s.PULLBACK_MAX_DIST_ATR * atr
        if strategy == StrategyType.PULLBACK_LONG:
            rsi_ok, rsi_gap = r.rsi <= s.PULLBACK_RSI_MAX, s.PULLBACK_RSI_MAX - r.rsi
            trend_ok = price > r.ema_slow
            rsi_rule, trend_rule = f"RSI {r.rsi:.0f} <= {s.PULLBACK_RSI_MAX:g}", "prezzo sopra la EMA50"
        else:
            rsi_ok, rsi_gap = r.rsi >= 100 - s.PULLBACK_RSI_MAX, r.rsi - (100 - s.PULLBACK_RSI_MAX)
            trend_ok = price < r.ema_slow
            rsi_rule, trend_rule = f"RSI {r.rsi:.0f} >= {100 - s.PULLBACK_RSI_MAX:g}", "prezzo sotto la EMA50"
        if not trend_ok:
            return Signal(triggered=False, text=f"serve {trend_rule}")
        if dist > max_dist:
            return Signal(triggered=False, text=f"lontano dalla EMA20 ({dist / atr:.2f} ATR, max {s.PULLBACK_MAX_DIST_ATR:g}): attendo il ritracciamento")
        if not rsi_ok:
            return Signal(triggered=False, text=f"vicino alla EMA20 ma momentum non ancora scaricato ({rsi_rule} non soddisfatto)")
        strength = 0.6 * _clip(100.0 * (1.0 - dist / max_dist)) + 0.4 * _clip(rsi_gap / 20.0 * 100.0)
        return Signal(triggered=True, strength=round(strength, 2),
                      text=f"ritracciamento sulla EMA20 ({dist / atr:.2f} ATR), {rsi_rule}")

    if strategy in (StrategyType.MEAN_REVERSION_LONG, StrategyType.MEAN_REVERSION_SHORT):
        if strategy == StrategyType.MEAN_REVERSION_LONG:
            depth, band, word = r.bb_lower - price, r.bb_lower, "inferiore"
        else:
            depth, band, word = price - r.bb_upper, r.bb_upper, "superiore"
        if depth < 0:
            return Signal(triggered=False, text=f"prezzo {price:.2f} dentro le bande (banda {word} {band:.2f})")
        # Alla banda = 50 punti, mezzo ATR oltre = 100
        return Signal(triggered=True, strength=_clip(50.0 + depth / (0.5 * atr) * 50.0),
                      text=f"prezzo oltre la banda di Bollinger {word} ({band:.2f}) di {depth / atr:.2f} ATR, target la media {r.bb_mid:.2f}")

    return Signal(triggered=False, text="nessuna strategia")


# ---------------------------------------------------------------------- #
# Sizing (funzione pura)
# ---------------------------------------------------------------------- #
def position_size(
    equity: float,
    price: float,
    risk_per_share: float,
    risk_pct: float,
    max_position_pct: float,
    exposure_room: float,
    buying_power: float | None = None,
) -> int:
    """
    Numero di azioni = min(
        rischio massimo / rischio per azione,            # perdita allo stop <= risk_pct dell'equity
        equity x max_position_pct / prezzo,              # concentrazione massima
        spazio di esposizione lorda residuo / prezzo,    # niente leva oltre il limite
        buying power / prezzo                            # vincolo del broker
    )
    """
    if equity <= 0 or price <= 0 or risk_per_share <= 0:
        return 0
    limits = [
        equity * risk_pct / risk_per_share,
        equity * max_position_pct / price,
        max(0.0, exposure_room) / price,
    ]
    if buying_power is not None:
        limits.append(max(0.0, buying_power) * 0.98 / price)
    return max(0, math.floor(min(limits)))


# ---------------------------------------------------------------------- #
# Trade plan
# ---------------------------------------------------------------------- #
class TradePlan(BaseModel):
    ticker: str
    strategy: StrategyType
    side: str  # "long" | "short"
    qty: int
    ref_price: float  # prezzo al segnale
    stop_price: float
    take_profit: float
    risk_per_share: float  # distanza dello stop (= 1R)
    reward_risk: float
    atr: float
    max_holding_days: int
    rationale: str
    signal_text: str

    @property
    def is_long(self) -> bool:
        return self.side == "long"

    @property
    def notional(self) -> float:
        return self.ref_price * self.qty

    @property
    def max_loss(self) -> float:
        return self.risk_per_share * self.qty

    def levels_from_fill(self, fill_price: float) -> tuple[float, float]:
        """Stop e target ricalcolati sul prezzo REALE di esecuzione, mantenendo le stesse distanze."""
        d_stop = abs(self.ref_price - self.stop_price)
        d_tp = abs(self.take_profit - self.ref_price)
        if self.is_long:
            return round(fill_price - d_stop, 2), round(fill_price + d_tp, 2)
        return round(fill_price + d_stop, 2), round(fill_price - d_tp, 2)

    def describe(self) -> str:
        return (
            f"{self.ticker} {self.strategy.value} {self.side.upper()} x{self.qty} @ ~{self.ref_price:.2f} | "
            f"stop {self.stop_price:.2f} (1R = {self.risk_per_share:.2f}) | target {self.take_profit:.2f} "
            f"({self.reward_risk:.1f}R) | controvalore ${self.notional:,.0f} | rischio ${self.max_loss:,.0f} | "
            f"uscita a tempo {self.max_holding_days} giorni"
        )


def stop_atr_multiple(strategy: StrategyType, s: Settings) -> float:
    if strategy in (StrategyType.BREAKOUT_LONG, StrategyType.BREAKDOWN_SHORT):
        return s.BREAKOUT_STOP_ATR
    if strategy in (StrategyType.PULLBACK_LONG, StrategyType.RALLY_SHORT):
        return s.PULLBACK_STOP_ATR
    return s.MEAN_REVERSION_STOP_ATR


def compute_levels(strategy: StrategyType, r: MarketRegime, s: Settings) -> tuple[float, float, float] | None:
    """(stop, target, rischio per azione) sul prezzo attuale; None se il rapporto rischio/rendimento non va."""
    price = r.price
    d = stop_atr_multiple(strategy, s) * r.atr
    if d <= 0:
        return None
    long_side = not is_short(strategy)
    stop = price - d if long_side else price + d
    if strategy in MEAN_REVERSION_STRATEGIES:
        target = r.bb_mid  # ritorno alla media
        reward = (target - price) if long_side else (price - target)
        if reward < s.MIN_REWARD_RISK * d:
            return None
    else:
        target = price + s.TAKE_PROFIT_R_TREND * d if long_side else price - s.TAKE_PROFIT_R_TREND * d
    if stop <= 0 or target <= 0:
        return None
    return round(stop, 2), round(target, 2), d


class StrategySelector:
    def __init__(self, settings: Settings) -> None:
        self.s = settings

    def build_plan(
        self,
        regime: MarketRegime,
        strategy: StrategyType,
        signal: Signal,
        equity: float,
        exposure_room: float,
        buying_power: float | None = None,
    ) -> TradePlan | None:
        s = self.s
        if strategy == StrategyType.NO_TRADE or not signal.triggered:
            return None
        levels = compute_levels(strategy, regime, s)
        if levels is None:
            logger.warning("{}: target {:.2f} troppo vicino rispetto allo stop (min {:.1f}R), scarto",
                           regime.ticker, regime.bb_mid, s.MIN_REWARD_RISK)
            return None
        stop, target, risk = levels
        qty = position_size(equity, regime.price, risk, s.MAX_RISK_PER_TRADE_PCT, s.MAX_POSITION_PCT, exposure_room, buying_power)
        logger.info(
            "{}: sizing -> rischio/azione {:.2f}, budget {:.1%} equity = ${:,.0f}, max posizione {:.0%}, "
            "spazio esposizione ${:,.0f} -> qty {}",
            regime.ticker, risk, s.MAX_RISK_PER_TRADE_PCT, equity * s.MAX_RISK_PER_TRADE_PCT, s.MAX_POSITION_PCT,
            exposure_room, qty,
        )
        if qty < 1:
            logger.warning("{}: nemmeno 1 azione rientra nei limiti di rischio/esposizione, scarto", regime.ticker)
            return None
        reward = abs(target - regime.price)
        plan = TradePlan(
            ticker=regime.ticker,
            strategy=strategy,
            side="short" if is_short(strategy) else "long",
            qty=qty,
            ref_price=round(regime.price, 2),
            stop_price=stop,
            take_profit=target,
            risk_per_share=round(risk, 4),
            reward_risk=round(reward / risk, 2) if risk else 0.0,
            atr=round(regime.atr, 4),
            max_holding_days=s.MAX_HOLDING_DAYS_TREND if strategy in TREND_STRATEGIES else s.MAX_HOLDING_DAYS_MR,
            rationale=(f"{regime.trend.value}+{regime.volatility.value} (ADX {regime.adx:.1f}, VolRank {regime.vol_rank:.0f}, "
                       f"RSI {regime.rsi:.0f})"),
            signal_text=signal.text,
        )
        logger.success("TradePlan: {}", plan.describe())
        return plan
