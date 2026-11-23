"""
Modulo 1 - Data Ingestion.

- Barre daily COMPLETE (la barra di oggi, ancora in formazione, viene scartata) via `StockHistoricalDataClient`.
  Di default feed SIP con 16 minuti di ritardo: è incluso nel piano gratuito e ha i volumi completi.
  Se l'account non lo consente, si ripiega automaticamente su IEX.
- Prezzi in tempo reale (ultimo trade + quote) via snapshot, in un'unica chiamata per tutti i ticker.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pandas as pd
from alpaca.data.enums import Adjustment, DataFeed
from alpaca.data.historical.stock import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest, StockSnapshotRequest
from alpaca.data.timeframe import TimeFrame
from loguru import logger

from config.settings import Settings

NEW_YORK = ZoneInfo("America/New_York")


@dataclass(slots=True)
class LiveQuote:
    symbol: str
    last: float | None  # ultimo trade
    bid: float | None
    ask: float | None
    timestamp: datetime | None = None

    @property
    def mid(self) -> float | None:
        if self.bid and self.ask and self.ask >= self.bid > 0:
            return (self.bid + self.ask) / 2.0
        return None

    @property
    def spread_pct(self) -> float | None:
        mid = self.mid
        if mid is None or self.bid is None or self.ask is None:
            return None
        return (self.ask - self.bid) / mid

    def reference(self, max_spread_pct: float) -> float | None:
        """
        Prezzo di riferimento: il mid se la quote è stretta e affidabile, altrimenti l'ultimo trade.
        (Le quote IEX dei titoli liquidi sono buone, ma a volte una delle due parti manca o è lontana.)
        """
        sp = self.spread_pct
        if sp is not None and sp <= max_spread_pct:
            return round(self.mid, 4)  # type: ignore[arg-type]
        return self.last


def drop_incomplete_bar(df: pd.DataFrame, today_ny: date | None = None) -> pd.DataFrame:
    """Scarta la barra daily di oggi (ancora in formazione): gli indicatori usano solo barre complete."""
    if df.empty:
        return df
    today_ny = today_ny or datetime.now(NEW_YORK).date()
    idx = pd.DatetimeIndex(df.index)
    if idx.tz is None:
        idx = idx.tz_localize("UTC")
    ny_dates = idx.tz_convert(NEW_YORK).date
    return df[ny_dates < today_ny]


class MarketDataService:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        key = settings.ALPACA_API_KEY.get_secret_value()
        secret = settings.ALPACA_SECRET_KEY.get_secret_value()
        self.stock_client = StockHistoricalDataClient(api_key=key, secret_key=secret)
        self.live_feed = DataFeed(settings.STOCK_DATA_FEED.lower())
        self.bars_feed = DataFeed(settings.BARS_DATA_FEED.lower())
        self._sip_failed = False
        self._bars_cache: dict[str, pd.DataFrame] = {}
        self._bars_day: date | None = None

    @property
    def bars_feed_used(self) -> DataFeed:
        return DataFeed.IEX if self._sip_failed else self.bars_feed

    @property
    def volumes_reliable(self) -> bool:
        """I volumi IEX sono solo una frazione del totale: il filtro di liquidità vale solo con SIP."""
        return self.bars_feed_used == DataFeed.SIP

    # ---------------- Barre daily ---------------- #
    def _bars_request(self, symbols: list[str], feed: DataFeed) -> StockBarsRequest:
        n_days = self.settings.HISTORY_TRADING_DAYS
        now = datetime.now(timezone.utc)
        start = now - timedelta(days=int(n_days * 1.5) + 10)
        end = now - timedelta(minutes=self.settings.SIP_DELAY_MIN) if feed == DataFeed.SIP else None
        return StockBarsRequest(
            symbol_or_symbols=symbols,
            timeframe=TimeFrame.Day,
            start=start,
            end=end,
            adjustment=Adjustment.ALL,  # prezzi rettificati per split e dividendi
            feed=feed,
        )

    def get_daily_bars(self, symbols: list[str]) -> dict[str, pd.DataFrame]:
        """
        Barre daily complete per ogni simbolo, con cache giornaliera: le barre complete cambiano solo
        una volta al giorno, quindi lo screening ogni minuto non le riscarica (solo i simboli nuovi).
        """
        today = datetime.now(NEW_YORK).date()
        if self._bars_day != today:
            self._bars_cache.clear()
            self._bars_day = today
        missing = [s for s in symbols if s not in self._bars_cache]
        if missing:
            fetched = self._fetch_daily_bars(missing)
            for s in missing:
                self._bars_cache[s] = fetched.get(s, pd.DataFrame())  # vuoto = non disponibile (non riprovo oggi)
        return {s: self._bars_cache[s] for s in symbols if not self._bars_cache[s].empty}

    def _fetch_daily_bars(self, symbols: list[str]) -> dict[str, pd.DataFrame]:
        """Barre daily complete (open/high/low/close/volume) per ogni simbolo, ultime HISTORY_TRADING_DAYS."""
        if not symbols:
            return {}
        feed = self.bars_feed_used
        try:
            bars = self.stock_client.get_stock_bars(self._bars_request(symbols, feed))
        except Exception as exc:  # noqa: BLE001
            if feed != DataFeed.SIP:
                raise
            logger.warning("Barre SIP non disponibili ({}): ripiego sul feed IEX (volumi parziali)", str(exc)[:160])
            self._sip_failed = True
            feed = DataFeed.IEX
            bars = self.stock_client.get_stock_bars(self._bars_request(symbols, feed))

        df = bars.df
        result: dict[str, pd.DataFrame] = {}
        if df.empty:
            logger.warning("Nessuna barra ricevuta da Alpaca")
            return result
        available = set(df.index.get_level_values(0))
        for symbol in symbols:
            if symbol not in available:
                logger.warning("{}: nessuna barra disponibile", symbol)
                continue
            sym_df = df.xs(symbol, level=0)[["open", "high", "low", "close", "volume"]].sort_index()
            sym_df = drop_incomplete_bar(sym_df).tail(self.settings.HISTORY_TRADING_DAYS)
            if not sym_df.empty:
                result[symbol] = sym_df
        logger.info("Barre daily: {} ticker su {} (feed {})", len(result), len(symbols), feed.value)
        return result

    # ---------------- Prezzi live ---------------- #
    def get_live_quotes(self, symbols: list[str]) -> dict[str, LiveQuote]:
        """Ultimo trade e quote per una lista di simboli (una sola chiamata)."""
        if not symbols:
            return {}
        try:
            snaps = self.stock_client.get_stock_snapshot(
                StockSnapshotRequest(symbol_or_symbols=symbols, feed=self.live_feed)
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("Impossibile leggere i prezzi live ({})", str(exc)[:200])
            return {}
        out: dict[str, LiveQuote] = {}
        for symbol, snap in snaps.items():
            if snap is None:
                continue
            trade, quote = snap.latest_trade, snap.latest_quote
            out[symbol] = LiveQuote(
                symbol=symbol,
                last=float(trade.price) if trade and trade.price else None,
                bid=float(quote.bid_price) if quote and quote.bid_price else None,
                ask=float(quote.ask_price) if quote and quote.ask_price else None,
                timestamp=trade.timestamp if trade else None,
            )
        return out

    def get_reference_prices(self, symbols: list[str]) -> dict[str, float]:
        """{simbolo: prezzo di riferimento} (mid se la quote è stretta, altrimenti ultimo trade)."""
        prices: dict[str, float] = {}
        for sym, q in self.get_live_quotes(symbols).items():
            ref = q.reference(self.settings.MAX_QUOTE_SPREAD_PCT)
            if ref:
                prices[sym] = float(ref)
        return prices
