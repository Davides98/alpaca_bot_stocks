"""
Modulo 5b - Risk Management (azioni).

Ad ogni controllo, per ogni posizione azionaria su Alpaca:
1. Protezione: deve esistere un ordine di protezione lato broker (OCO stop + target, o almeno uno stop).
   Se manca (es. cancellato a mano, ordine scaduto) viene ricreato; se il prezzo ha già superato lo stop
   la posizione viene chiusa subito.
2. Stop dinamico (solo in avanti, mai indietro):
     - breakeven: a +BREAKEVEN_AT_R lo stop sale al prezzo di ingresso (il trade non può più andare in perdita);
     - trailing (solo strategie di trend): da +TRAIL_START_R lo stop segue il prezzo a TRAIL_ATR_MULT x ATR.
3. Uscite volontarie (ordine limit con tetto di slippage):
     - TIME_STOP: posizione aperta da più di MAX_HOLDING_DAYS (trend / mean reversion);
     - TREND_REVERSAL: il regime si è girato contro la posizione (long in BEARISH, short in BULLISH).
4. Riconciliazione: un trade OPEN su Supabase senza posizione su Alpaca è stato chiuso lato broker
   (take profit / stop eseguiti mentre il bot era fermo, o chiusura manuale): il bot recupera il prezzo
   di uscita dagli ordini eseguiti e registra il P&L.

Take profit e stop loss "statici" li esegue Alpaca: il bot non li duplica finché la protezione è attiva.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any

from alpaca.trading.client import TradingClient
from alpaca.trading.enums import AssetClass, OrderSide, OrderStatus, OrderType, PositionSide, QueryOrderStatus
from alpaca.trading.models import Order, Position
from alpaca.trading.requests import GetOrdersRequest
from loguru import logger

from config.settings import Settings
from src.analysis.regime import MarketRegime, Trend
from src.execution.executor import PREFIX_EXIT, ExecutionResult, Protection
from src.strategy.selector import is_trend_strategy

if TYPE_CHECKING:  # evita import circolari a runtime
    from src.data.market_data import MarketDataService
    from src.execution.executor import OrderExecutor
    from src.storage.supabase_client import SupabaseStorage


# ---------------------------------------------------------------------- #
# Funzioni pure
# ---------------------------------------------------------------------- #
def r_multiple(entry: float, price: float, risk_per_share: float, is_long: bool) -> float:
    """Profitto attuale in unità di rischio iniziale (1R = distanza dello stop iniziale)."""
    if risk_per_share <= 0:
        return 0.0
    return ((price - entry) if is_long else (entry - price)) / risk_per_share


def realized_pnl(entry: float, exit_price: float, qty: int, is_long: bool) -> float:
    return ((exit_price - entry) if is_long else (entry - exit_price)) * qty


def next_stop(
    is_long: bool, entry: float, current_stop: float, price: float, risk_per_share: float,
    atr: float | None, trend_strategy: bool, settings: Settings,
) -> float | None:
    """Nuovo livello di stop (breakeven / trailing) se migliora quello attuale; None se resta invariato."""
    if risk_per_share <= 0:
        return None
    r = r_multiple(entry, price, risk_per_share, is_long)
    candidate = current_stop
    better = max if is_long else min
    if settings.BREAKEVEN_AT_R > 0 and r >= settings.BREAKEVEN_AT_R:
        candidate = better(candidate, entry)
    if trend_strategy and atr and settings.TRAIL_START_R > 0 and r >= settings.TRAIL_START_R:
        trail = price - settings.TRAIL_ATR_MULT * atr if is_long else price + settings.TRAIL_ATR_MULT * atr
        candidate = better(candidate, trail)
    candidate = round(candidate, 2)
    # Lo stop deve restare dal lato giusto del prezzo, e lo spostamento deve valere la pena
    if (is_long and candidate >= price) or (not is_long and candidate <= price):
        return None
    min_step = max(0.01, 0.1 * atr) if atr else 0.01
    improvement = (candidate - current_stop) if is_long else (current_stop - candidate)
    return candidate if improvement >= min_step - 1e-9 else None


def stop_hit_reason(is_long: bool, entry: float, stop_fill: float) -> str:
    """Uno stop eseguito in profitto (o in pari) è un trailing stop, altrimenti uno stop loss."""
    pnl = (stop_fill - entry) if is_long else (entry - stop_fill)
    return "TRAILING_STOP" if pnl >= -1e-6 else "STOP_LOSS"


def voluntary_exit_reason(
    is_long: bool, holding_days: int | None, max_days: int | None, trend_now: Trend | None, settings: Settings,
) -> str | None:
    """Uscite decise dal bot (le soglie di prezzo le gestisce l'ordine OCO lato broker)."""
    if holding_days is not None and max_days and holding_days >= max_days:
        return "TIME_STOP"
    if settings.EXIT_ON_TREND_REVERSAL and trend_now is not None:
        if (is_long and trend_now == Trend.BEARISH) or (not is_long and trend_now == Trend.BULLISH):
            return "TREND_REVERSAL"
    return None


def price_breach(is_long: bool, price: float, stop: float | None, target: float | None) -> str | None:
    """Prezzo già oltre lo stop o il target (serve quando la protezione lato broker manca)."""
    if stop is not None and ((is_long and price <= stop) or (not is_long and price >= stop)):
        return "STOP_LOSS"
    if target is not None and ((is_long and price >= target) or (not is_long and price <= target)):
        return "TAKE_PROFIT"
    return None


# ---------------------------------------------------------------------- #
# Modelli
# ---------------------------------------------------------------------- #
def _parse_ts(value: Any) -> datetime | None:
    if not value:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


@dataclass
class ManagedPosition:
    symbol: str
    is_long: bool
    qty: int
    entry: float  # prezzo medio di carico (Alpaca)
    price: float  # prezzo attuale
    market_value: float
    protection: Protection
    trade: dict[str, Any] | None = None  # riga Supabase abbinata

    @property
    def details(self) -> dict[str, Any]:
        return (self.trade or {}).get("details_json") or {}

    @property
    def strategy(self) -> str:
        return (self.trade or {}).get("strategy") or "UNKNOWN"

    @property
    def stop(self) -> float | None:
        if self.protection.stop_price is not None:
            return self.protection.stop_price
        v = (self.trade or {}).get("stop_price")
        return float(v) if v is not None else None

    @property
    def target(self) -> float | None:
        if self.protection.tp_price is not None:
            return self.protection.tp_price
        v = (self.trade or {}).get("take_profit")
        return float(v) if v is not None else None

    @property
    def risk_per_share(self) -> float:
        v = (self.trade or {}).get("risk_per_share")
        if v:
            return float(v)
        init = (self.trade or {}).get("initial_stop")
        ref = float(init) if init is not None else self.stop
        return abs(self.entry - ref) if ref is not None else 0.0

    @property
    def atr(self) -> float | None:
        v = self.details.get("atr")
        return float(v) if v else None

    @property
    def opened_at(self) -> datetime | None:
        return _parse_ts((self.trade or {}).get("created_at"))

    @property
    def holding_days(self) -> int | None:
        opened = self.opened_at
        return (datetime.now(timezone.utc) - opened).days if opened else None

    @property
    def max_days(self) -> int | None:
        v = self.details.get("max_holding_days")
        return int(v) if v else None

    @property
    def unrealized(self) -> float:
        return realized_pnl(self.entry, self.price, self.qty, self.is_long)

    @property
    def label(self) -> str:
        return f"{self.symbol} {self.strategy} {'LONG' if self.is_long else 'SHORT'}"


@dataclass
class RiskReport:
    open_positions: list[ManagedPosition] = field(default_factory=list)
    closed: list[str] = field(default_factory=list)
    occupied: set[str] = field(default_factory=set)  # posizioni + ordini pendenti
    cooldown: set[str] = field(default_factory=set)  # in pausa dopo uno stop loss recente
    gross_exposure: float = 0.0

    @property
    def open_count(self) -> int:
        return len(self.open_positions)


# ---------------------------------------------------------------------- #
# Risk manager
# ---------------------------------------------------------------------- #
class RiskManager:
    def __init__(
        self,
        settings: Settings,
        trading_client: TradingClient,
        market: "MarketDataService",
        executor: "OrderExecutor",
        storage: "SupabaseStorage",
    ) -> None:
        self.s = settings
        self.client = trading_client
        self.market = market
        self.executor = executor
        self.storage = storage

    # ---------------- Lettura stato ---------------- #
    def equity_positions(self) -> list[Position]:
        return [p for p in self.client.get_all_positions() if p.asset_class == AssetClass.US_EQUITY]

    def held_symbols(self) -> set[str]:
        try:
            return {p.symbol for p in self.equity_positions()}
        except Exception as exc:  # noqa: BLE001
            logger.warning("Impossibile leggere le posizioni ({})", exc)
            return set()

    # ---------------- Riconciliazione ---------------- #
    def find_exit_fill(self, symbol: str, is_long: bool, since: datetime | None) -> tuple[float, str] | None:
        """Ultimo ordine di uscita eseguito per il simbolo dopo `since`: (prezzo medio, motivo)."""
        try:
            orders = self.client.get_orders(GetOrdersRequest(
                status=QueryOrderStatus.CLOSED, symbols=[symbol], nested=True, limit=200,
                after=since - timedelta(minutes=1) if since else None,
            ))
        except Exception as exc:  # noqa: BLE001
            logger.warning("{}: impossibile leggere lo storico ordini ({})", symbol, exc)
            return None
        exit_side = OrderSide.SELL if is_long else OrderSide.BUY
        flat: list[Order] = []
        for o in orders:
            flat.append(o)
            flat.extend(o.legs or [])
        fills = [o for o in flat if o.side == exit_side and o.status == OrderStatus.FILLED and o.filled_avg_price]
        if not fills:
            return None
        last = max(fills, key=lambda o: o.filled_at or o.updated_at or datetime.min.replace(tzinfo=timezone.utc))
        price = float(last.filled_avg_price)
        otype = last.order_type or last.type
        if otype in (OrderType.STOP, OrderType.STOP_LIMIT, OrderType.TRAILING_STOP):
            reason = "STOP"
        elif otype == OrderType.LIMIT and not str(last.client_order_id or "").startswith(PREFIX_EXIT):
            reason = "TAKE_PROFIT"
        else:
            reason = "MANUAL_CLOSE"
        return price, reason

    def _reconcile_orphans(self, open_trades: list[dict[str, Any]], held: set[str]) -> None:
        for trade in open_trades:
            if trade["ticker"] in held:
                continue
            is_long = str(trade.get("side", "LONG")).upper() == "LONG"
            entry = float(trade["entry_price"])
            qty = int(trade.get("qty") or 0)
            found = self.find_exit_fill(trade["ticker"], is_long, _parse_ts(trade.get("created_at")))
            if found is None:
                logger.warning("Trade #{} {} OPEN su Supabase ma non su Alpaca e senza uscita trovata: CLOSED (NOT_FOUND_IN_BROKER)",
                               trade["id"], trade["ticker"])
                if not self.s.DRY_RUN:
                    self.storage.close_trade(trade["id"], None, None, None, "NOT_FOUND_IN_BROKER")
                continue
            exit_price, kind = found
            reason = stop_hit_reason(is_long, entry, exit_price) if kind == "STOP" else kind
            pnl = realized_pnl(entry, exit_price, qty, is_long)
            logger.success("Trade #{} {} chiuso lato broker ({}) @ {:.2f}: P&L ${:+,.2f}",
                           trade["id"], trade["ticker"], reason, exit_price, pnl)
            if not self.s.DRY_RUN:
                self.storage.close_trade(trade["id"], round(exit_price, 4), round(pnl, 2),
                                         round(pnl / (entry * qty) * 100, 4) if entry and qty else None, reason)

    # ---------------- Gestione ---------------- #
    def manage_open_positions(self, regimes: dict[str, MarketRegime] | None = None, allow_dynamic: bool = True) -> RiskReport:
        """
        regimes: regimi aggiornati (solo nei cicli di trading) per le uscite TREND_REVERSAL.
        allow_dynamic: False nei primi minuti dopo l'apertura (niente stop dinamici né uscite volontarie).
        """
        report = RiskReport()
        logger.info("=== RISK MANAGER: controllo posizioni azionarie ===")
        positions = self.equity_positions()
        try:
            orders = self.executor.open_orders()
        except Exception as exc:  # noqa: BLE001
            logger.warning("Impossibile leggere gli ordini aperti ({}): salto la gestione in questo ciclo", exc)
            report.occupied = {p.symbol for p in positions}
            report.open_positions = []
            report.gross_exposure = sum(abs(float(p.market_value or 0)) for p in positions)
            return report
        open_trades = self.storage.get_open_trades()
        held = {p.symbol for p in positions}
        self._reconcile_orphans(open_trades, held)
        trades_by_ticker = {t["ticker"]: t for t in open_trades}
        prices = self.market.get_reference_prices(sorted(held)) if held else {}

        if not positions:
            logger.info("Nessuna posizione azionaria aperta")

        for p in positions:
            qty = abs(int(float(p.qty)))
            price = prices.get(p.symbol) or float(p.current_price or p.avg_entry_price)
            mp = ManagedPosition(
                symbol=p.symbol,
                is_long=p.side == PositionSide.LONG,
                qty=qty,
                entry=float(p.avg_entry_price),
                price=price,
                market_value=abs(float(p.market_value or price * qty)),
                protection=self.executor.protection_from_orders(orders, p.symbol),
                trade=trades_by_ticker.get(p.symbol),
            )
            if self._manage_one(mp, regimes, allow_dynamic, report):
                report.open_positions.append(mp)

        report.gross_exposure = sum(mp.market_value for mp in report.open_positions)
        pending = {o.symbol for o in orders if o.symbol}
        report.occupied = {mp.symbol for mp in report.open_positions} | pending
        report.cooldown = self.cooldown_symbols()
        logger.info(
            "Posizioni aperte: {} / max {} | esposizione lorda ${:,.0f} | ticker occupati: {}",
            report.open_count, self.s.MAX_OPEN_POSITIONS, report.gross_exposure, sorted(report.occupied) or "-",
        )
        return report

    def _manage_one(self, mp: ManagedPosition, regimes: dict[str, MarketRegime] | None, allow_dynamic: bool,
                    report: RiskReport) -> bool:
        """Gestisce una posizione. Ritorna True se resta aperta."""
        s = self.s
        r = r_multiple(mp.entry, mp.price, mp.risk_per_share, mp.is_long)
        logger.info(
            "[{}] x{} | entrata {:.2f} | attuale {:.2f} | P&L ${:+,.2f} ({:+.2f}R) | stop {} | target {} | "
            "protezione {} | giorni {}",
            mp.label, mp.qty, mp.entry, mp.price, mp.unrealized, r,
            f"{mp.stop:.2f}" if mp.stop is not None else "-", f"{mp.target:.2f}" if mp.target is not None else "-",
            "attiva" if mp.protection.active else "ASSENTE", mp.holding_days if mp.holding_days is not None else "?",
        )

        # 1) Uscite volontarie (tempo / inversione di trend)
        regime = (regimes or {}).get(mp.symbol)
        if allow_dynamic:
            reason = voluntary_exit_reason(mp.is_long, mp.holding_days, mp.max_days,
                                           regime.trend if regime else None, s)
            if reason:
                logger.warning("[{}] condizione di uscita {}: chiudo la posizione", mp.label, reason)
                return not self._close(mp, reason, report)

        # 2) Protezione lato broker
        if not mp.protection.active:
            stop, target = mp.stop, mp.target
            if stop is None and regime is not None:
                # Posizione senza stop noto (es. aperta a mano): stop di emergenza a BREAKOUT_STOP_ATR x ATR
                d = s.BREAKOUT_STOP_ATR * regime.atr
                stop = round(mp.price - d if mp.is_long else mp.price + d, 2)
                logger.warning("[{}] nessuno stop noto: imposto uno stop di emergenza a {:.2f}", mp.label, stop)
            if stop is None:
                logger.warning("[{}] posizione senza protezione e senza stop noto: la proteggo al prossimo ciclo di trading",
                               mp.label)
            else:
                breach = price_breach(mp.is_long, mp.price, stop, target)
                if breach:
                    logger.warning("[{}] protezione assente e prezzo già oltre {} : chiudo", mp.label, breach)
                    if breach == "STOP_LOSS":
                        breach = stop_hit_reason(mp.is_long, mp.entry, mp.price)
                    return not self._close(mp, breach, report)
                logger.warning("[{}] protezione assente: la ricreo (stop {:.2f}, target {})", mp.label, stop,
                               f"{target:.2f}" if target else "-")
                mp.protection = self.executor.protect(mp.symbol, mp.is_long, mp.qty, stop, target, mp.label)

        # 3) Stop dinamico
        if allow_dynamic and mp.protection.active and mp.stop is not None:
            new = next_stop(mp.is_long, mp.entry, mp.stop, mp.price, mp.risk_per_share, mp.atr,
                            is_trend_strategy(mp.strategy), s)
            if new is not None:
                kind = "breakeven" if abs(new - mp.entry) < 0.011 else "trailing"
                logger.info("[{}] stop {} : {:.2f} -> {:.2f} ({:+.2f}R)", mp.label, kind, mp.stop, new, r)
                old = mp.stop
                mp.protection = self.executor.replace_stop(mp.protection, new, mp.symbol, mp.is_long, mp.qty, mp.label)
                if mp.trade and not s.DRY_RUN:
                    history = list(mp.details.get("stop_history") or [])
                    history.append({"at": datetime.now(timezone.utc).isoformat(), "from": old, "to": new, "kind": kind})
                    self.storage.update_trade(mp.trade["id"], {
                        "stop_price": new, "details_json": {**mp.details, "stop_history": history[-20:]},
                    })
                    mp.trade["stop_price"] = new
                    mp.trade["details_json"] = {**mp.details, "stop_history": history[-20:]}

        # 4) Aggiornamento su Supabase
        if mp.trade and not s.DRY_RUN:
            self.storage.update_trade(mp.trade["id"], {
                "current_price": round(mp.price, 4),
                "unrealized_pnl": round(mp.unrealized, 2),
                "r_multiple": round(r, 3),
                "stop_price": mp.stop,
                "qty": mp.qty,
            })
        return True

    def _close(self, mp: ManagedPosition, reason: str, report: RiskReport) -> bool:
        """Chiusura volontaria. True se la posizione è stata chiusa completamente."""
        result: ExecutionResult = self.executor.close_position(mp.symbol, mp.is_long, mp.qty, mp.price, f"CLOSE {mp.label}")
        if result.dry_run:
            report.closed.append(f"{mp.label} (DRY_RUN)")
            return False
        if result.filled and result.avg_price is not None:
            pnl = realized_pnl(mp.entry, result.avg_price, mp.qty, mp.is_long)
            logger.success("[{}] CHIUSA ({}) @ {:.2f}: P&L realizzato ${:+,.2f}", mp.label, reason, result.avg_price, pnl)
            report.closed.append(mp.label)
            if reason == "STOP_LOSS":
                report.cooldown.add(mp.symbol)
            if mp.trade:
                cost = mp.entry * mp.qty
                self.storage.close_trade(mp.trade["id"], round(result.avg_price, 4), round(pnl, 2),
                                         round(pnl / cost * 100, 4) if cost else None, reason)
            return True
        # Non eseguita (o parziale): la posizione residua va subito protetta di nuovo
        remaining = mp.qty - result.filled_qty
        logger.warning("[{}] chiusura non completata ({}): ripristino la protezione su {} azioni",
                       mp.label, result.message, remaining)
        if remaining > 0 and mp.stop is not None:
            mp.qty = remaining
            mp.protection = self.executor.protect(mp.symbol, mp.is_long, remaining, mp.stop, mp.target, mp.label)
        return False

    def cooldown_symbols(self) -> set[str]:
        """Ticker chiusi in stop loss nelle ultime STOP_LOSS_COOLDOWN_HOURS ore (letti da Supabase)."""
        if self.s.STOP_LOSS_COOLDOWN_HOURS <= 0:
            return set()
        since = datetime.now(timezone.utc) - timedelta(hours=self.s.STOP_LOSS_COOLDOWN_HOURS)
        tickers = set(self.storage.get_recent_stop_tickers(since))
        if tickers:
            logger.info("Ticker in pausa dopo uno stop loss recente: {}", sorted(tickers))
        return tickers
