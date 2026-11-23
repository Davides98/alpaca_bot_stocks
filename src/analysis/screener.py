"""
Modulo 3 - Screener.

Per ogni ticker della watchlist (più quelli già in portafoglio, per le uscite di regime):
1. calcola il regime (trend + volatilità) sulle barre daily complete e sul prezzo live;
2. sceglie la strategia dalla matrice e ne verifica il segnale di ingresso;
3. applica i filtri di liquidità (prezzo minimo, controvalore medio);
4. assegna un Opportunity Score (0-100).

I filtri che dipendono dal portafoglio (ticker già occupati, pausa dopo uno stop, slot) li applica main.py.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import asdict, dataclass, field

import numpy as np
from loguru import logger

from config.settings import Settings
from src.analysis.regime import MarketRegime, Trend, VolRegime, detect_regime
from src.data.market_data import MarketDataService
from src.strategy.selector import (
    MEAN_REVERSION_STRATEGIES,
    SHORT_STRATEGIES,
    Signal,
    StrategyType,
    choose_strategy,
    evaluate_signal,
)

# Pesi delle componenti dello score
W_SIGNAL = 0.40
W_TREND = 0.35
W_VOL = 0.25


@dataclass
class ScreeningRow:
    ticker: str
    price: float | None = None
    trend: str | None = None
    volatility_regime: str | None = None
    vol_rank: float | None = None
    adx: float | None = None
    rsi: float | None = None
    atr_pct: float | None = None
    opportunity_score: float = 0.0
    selected: bool = False
    strategy: str | None = None
    signal: str | None = None
    reason: str = ""

    def to_record(self) -> dict:
        return asdict(self)


@dataclass
class Candidate:
    regime: MarketRegime
    strategy: StrategyType
    signal: Signal
    score: float
    row: ScreeningRow


@dataclass
class ScreenerOutput:
    ranked: list[Candidate]  # candidati con segnale attivo, ordinati per score decrescente
    rows: list[ScreeningRow]  # solo la watchlist (per Supabase / dashboard)
    regimes: dict[str, MarketRegime] = field(default_factory=dict)  # tutti i ticker analizzati (anche posizioni)


def compute_opportunity_score(
    strategy: StrategyType,
    vol: VolRegime,
    vol_rank: float,
    adx_value: float,
    signal_strength: float,
    adx_threshold: float = 20.0,
) -> float:
    """
    Score 0-100 = 40% qualità del segnale + 35% trend + 25% volatilità.

    - Segnale: forza del trigger (breakout fresco, pullback vicino alla EMA20, eccesso oltre la banda).
    - Trend: per le strategie direzionali premia ADX alto (20 -> 0 punti, 50+ -> 100);
             per la mean reversion premia ADX basso (range più pulito), come l'Iron Condor nel bot opzioni.
    - Volatilità: HIGH_VOL premia Vol Rank alto; LOW_VOL premia Vol Rank basso (compressione prima del breakout).
    """
    if strategy == StrategyType.NO_TRADE:
        return 0.0
    if strategy in MEAN_REVERSION_STRATEGIES:
        trend_signal = (adx_threshold - adx_value) / adx_threshold * 100.0
    else:
        trend_signal = (adx_value - adx_threshold) / 30.0 * 100.0
    vol_signal = vol_rank if vol == VolRegime.HIGH_VOL else 100.0 - vol_rank
    score = (
        W_SIGNAL * float(np.clip(signal_strength, 0, 100))
        + W_TREND * float(np.clip(trend_signal, 0, 100))
        + W_VOL * float(np.clip(vol_signal, 0, 100))
    )
    return round(score, 2)


class Screener:
    def __init__(self, settings: Settings, market: MarketDataService) -> None:
        self.s = settings
        self.market = market

    def _regime(self, ticker: str, bars, price: float | None) -> MarketRegime:
        s = self.s
        return detect_regime(
            ticker, bars, price,
            ema_fast_period=s.EMA_FAST, ema_slow_period=s.EMA_SLOW,
            adx_period=s.ADX_PERIOD, adx_threshold=s.ADX_TREND_THRESHOLD,
            atr_period=s.ATR_PERIOD, rsi_period=s.RSI_PERIOD,
            hv_window=s.HV_WINDOW, vol_rank_threshold=s.VOL_RANK_HIGH_THRESHOLD,
            bb_period=s.BB_PERIOD, bb_std=s.BB_STD, breakout_lookback=s.BREAKOUT_LOOKBACK,
        )

    def run(self, extra_symbols: set[str] | None = None, shortable: Callable[[str], bool] | None = None) -> ScreenerOutput:
        """`shortable(symbol) -> bool` verifica che il titolo sia vendibile allo scoperto (solo per i candidati short)."""
        s = self.s
        watch = list(s.WATCHLIST)
        universe = watch + sorted((extra_symbols or set()) - set(watch))
        logger.info("=== SCREENER: {} ticker in watchlist{} ===", len(watch),
                    f" + {len(universe) - len(watch)} in portafoglio" if len(universe) > len(watch) else "")
        bars_by_ticker = self.market.get_daily_bars(universe)
        prices = self.market.get_reference_prices(universe)
        check_volume = self.market.volumes_reliable and s.MIN_AVG_DOLLAR_VOLUME > 0

        rows: list[ScreeningRow] = []
        eligible: list[Candidate] = []
        regimes: dict[str, MarketRegime] = {}

        for ticker in universe:
            in_watchlist = ticker in watch
            row = ScreeningRow(ticker=ticker)
            if in_watchlist:
                rows.append(row)
            try:
                bars = bars_by_ticker.get(ticker)
                if bars is None or bars.empty:
                    row.reason = "Storico prezzi non disponibile"
                    continue
                live = prices.get(ticker)
                regime = self._regime(ticker, bars, live)
                regimes[ticker] = regime
                if not in_watchlist:
                    continue
                logger.debug(regime.describe())

                strategy = choose_strategy(regime.trend, regime.volatility, regime.price, regime.bb_mid, allow_short=True)
                row.price = round(regime.price, 2)
                row.trend = regime.trend.value
                row.volatility_regime = regime.volatility.value
                row.vol_rank = round(regime.vol_rank, 2)
                row.adx = round(regime.adx, 2)
                row.rsi = round(regime.rsi, 2)
                row.atr_pct = round(regime.atr_pct, 5)
                row.strategy = strategy.value

                if live is None:
                    row.reason = "Prezzo live non disponibile (uso l'ultima chiusura): nessun nuovo trade"
                    continue
                if regime.price < s.MIN_PRICE:
                    row.reason = f"Prezzo sotto il minimo ({s.MIN_PRICE:g}$)"
                    continue
                if check_volume and regime.avg_dollar_volume < s.MIN_AVG_DOLLAR_VOLUME:
                    row.reason = f"Illiquido (controvalore medio ${regime.avg_dollar_volume / 1e6:,.0f}M)"
                    continue
                if strategy == StrategyType.NO_TRADE:
                    row.reason = "SIDEWAYS + LOW_VOL: nessun edge"
                    continue

                signal = evaluate_signal(strategy, regime, s)
                row.signal = signal.text
                row.opportunity_score = compute_opportunity_score(
                    strategy, regime.volatility, regime.vol_rank, regime.adx, signal.strength, s.ADX_TREND_THRESHOLD
                )
                if not signal.triggered:
                    row.reason = "In attesa del segnale di ingresso"
                    continue
                if strategy in SHORT_STRATEGIES:
                    if not s.ALLOW_SHORT:
                        row.reason = "Short disattivati (ALLOW_SHORT=false)"
                        continue
                    if shortable is not None and not shortable(ticker):
                        row.reason = "Titolo non disponibile per lo short (non easy-to-borrow)"
                        continue
                if row.opportunity_score < s.MIN_OPPORTUNITY_SCORE:
                    row.reason = f"Score sotto soglia ({s.MIN_OPPORTUNITY_SCORE:.0f})"
                    continue

                row.reason = "Idoneo"
                eligible.append(Candidate(regime=regime, strategy=strategy, signal=signal, score=row.opportunity_score, row=row))
            except Exception as exc:  # noqa: BLE001 - un ticker problematico non deve fermare lo screening
                logger.warning("{}: errore durante lo screening ({}: {})", ticker, type(exc).__name__, exc)
                row.reason = f"Errore: {type(exc).__name__}"

        eligible.sort(key=lambda c: c.score, reverse=True)
        self._log_table(rows)
        return ScreenerOutput(ranked=eligible, rows=rows, regimes=regimes)

    @staticmethod
    def _log_table(rows: list[ScreeningRow]) -> None:
        header = (f"{'Ticker':<6}| {'Prezzo':>8} | {'Trend':<8}| {'Vol':<8}| {'VRk':>4} | {'ADX':>5} | {'RSI':>4} | "
                  f"{'Score':>5} | Strategia / Motivo")
        lines = [header, "-" * len(header)]
        for r in sorted(rows, key=lambda x: x.opportunity_score, reverse=True):
            def fmt(v, spec):
                return format(v, spec) if v is not None else "n/d"
            lines.append(
                f"{r.ticker:<6}| {fmt(r.price, '8.2f'):>8} | {(r.trend or 'n/d'):<8}| {(r.volatility_regime or 'n/d'):<8}| "
                f"{fmt(r.vol_rank, '.0f'):>4} | {fmt(r.adx, '.1f'):>5} | {fmt(r.rsi, '.0f'):>4} | {r.opportunity_score:>5.1f} | "
                f"{r.strategy or '-'} / {r.reason}{' - ' + r.signal if r.signal else ''}"
            )
        logger.info("Risultati screening:\n{}", "\n".join(lines))
