"""
Orchestratore principale del bot azionario (pensato per GitHub Actions, come il bot opzioni).

Flusso di un ciclo di trading:
  1. Snapshot equity su Supabase
  2. Screener della watchlist (+ titoli in portafoglio) -> regimi e candidati
  3. Risk manager: protezione lato broker, stop dinamici, time stop, uscite per inversione di trend
  4. Nuovi trade: Trade Plan -> ordine Limit con price chasing -> protezione OCO (stop + target)
  5. Salvataggio log di screening

Modalità sessione (run automatici): un job resta attivo fino alla chiusura di Wall Street,
aggiorna equity e posizioni ogni REFRESH_INTERVAL_SEC e fa trading ogni TRADE_INTERVAL_MIN.

Esecuzione:  python -m src.main
"""

from __future__ import annotations

import os
import sys
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

from alpaca.common.exceptions import APIError
from alpaca.trading.client import TradingClient
from alpaca.trading.enums import OrderSide
from loguru import logger
from pydantic import ValidationError

from config.settings import PAPER_TRADING, Settings, get_settings
from src.analysis.regime import MarketRegime
from src.analysis.screener import Candidate, Screener
from src.data.market_data import MarketDataService
from src.execution.executor import OrderExecutor, Protection
from src.execution.risk_manager import RiskManager
from src.storage.supabase_client import SupabaseStorage
from src.strategy.selector import StrategySelector, TradePlan, evaluate_signal

NEW_YORK = ZoneInfo("America/New_York")


# ---------------------------------------------------------------------- #
# Logging sicuro
# ---------------------------------------------------------------------- #
def setup_logging(settings: Settings) -> None:
    secrets = settings.secret_values()
    try:  # console Windows: evita UnicodeEncodeError
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    except (AttributeError, ValueError):
        pass

    def redacting_sink(message: Any) -> None:
        text = str(message)
        for secret in secrets:
            text = text.replace(secret, "***REDACTED***")
        sys.stderr.write(text)

    logger.remove()
    logger.add(
        redacting_sink,
        level=settings.LOG_LEVEL.upper(),
        format="<green>{time:YYYY-MM-DD HH:mm:ss}</green> | <level>{level: <8}</level> | <cyan>{name}</cyan> - <level>{message}</level>",
        colorize=sys.stderr.isatty(),
        backtrace=False,
        diagnose=False,  # NON stampare i valori delle variabili locali nei traceback (potrebbero contenere chiavi)
    )


# ---------------------------------------------------------------------- #
# Helper
# ---------------------------------------------------------------------- #
def exit_rules(settings: Settings) -> dict[str, Any]:
    """Regole di uscita in vigore all'apertura del trade (la dashboard le mostra nella scheda della strategia)."""
    return {
        "breakeven_at_r": settings.BREAKEVEN_AT_R,
        "trail_start_r": settings.TRAIL_START_R,
        "trail_atr_mult": settings.TRAIL_ATR_MULT,
        "take_profit_r_trend": settings.TAKE_PROFIT_R_TREND,
        "exit_on_trend_reversal": settings.EXIT_ON_TREND_REVERSAL,
        "pause_after_open_min": settings.NO_TRADE_AFTER_OPEN_MIN,
    }


def regime_snapshot(r: MarketRegime) -> dict[str, Any]:
    keys = ("trend", "volatility", "adx", "vol_rank", "rsi", "atr", "ema_fast", "ema_slow",
            "bb_mid", "bb_upper", "bb_lower", "donchian_high", "donchian_low", "hv_current")
    out: dict[str, Any] = {}
    for k in keys:
        v = getattr(r, k)
        out[k] = v.value if hasattr(v, "value") else round(float(v), 4)
    return out


def plan_to_trade_row(plan: TradePlan, regime: MarketRegime, fill: float, qty: int, stop: float, target: float,
                      order_id: str | None, prot: Protection, settings: Settings) -> dict[str, Any]:
    risk = abs(fill - stop)
    return {
        "ticker": plan.ticker,
        "strategy": plan.strategy.value,
        "side": "LONG" if plan.is_long else "SHORT",
        "entry_price": round(fill, 4),
        "qty": qty,
        "initial_stop": stop,
        "stop_price": stop,
        "take_profit": target,
        "risk_per_share": round(risk, 4),
        "current_price": round(fill, 4),
        "unrealized_pnl": 0,
        "r_multiple": 0,
        "alpaca_order_id": order_id,
        "protection_order_id": prot.parent_id or prot.stop_order_id,
        "details_json": {
            "ref_price": plan.ref_price,
            "atr": plan.atr,
            "reward_risk": plan.reward_risk,
            "max_holding_days": plan.max_holding_days,
            "rationale": plan.rationale,
            "signal": plan.signal_text,
            "protection": "OCO" if prot.tp_order_id else ("STOP" if prot.active else "NONE"),
            "regime": regime_snapshot(regime),
            "exit_rules": exit_rules(settings),
            "stop_history": [],
        },
    }


# ---------------------------------------------------------------------- #
# Contesto del run
# ---------------------------------------------------------------------- #
def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat()


@dataclass
class BotStatus:
    """Stato attuale del bot (tabella stock_bot_status, una riga): alimenta i contatori della dashboard."""

    storage: SupabaseStorage
    settings: Settings
    run_id: str
    session_started_at: datetime | None = None
    session_deadline: datetime | None = None
    last_trade_cycle_at: datetime | None = None
    next_trade_cycle_at: datetime | None = None
    trading_cycles: int = 0

    def row(self, state: str, clock: Any = None, message: str = "") -> dict[str, Any]:
        s = self.settings
        row: dict[str, Any] = {
            "state": state,
            "message": message[:300],
            "run_id": self.run_id,
            "trigger": os.getenv("BOT_TRIGGER") or os.getenv("GITHUB_EVENT_NAME", "local"),
            "dry_run": s.DRY_RUN,
            "session_mode": s.SESSION_MODE,
            "refresh_interval_sec": s.REFRESH_INTERVAL_SEC,
            "trade_interval_min": s.TRADE_INTERVAL_MIN,
            "session_max_minutes": s.SESSION_MAX_MINUTES,
            "session_started_at": _iso(self.session_started_at),
            "session_deadline": _iso(self.session_deadline),
            "last_trade_cycle_at": _iso(self.last_trade_cycle_at),
            "next_trade_cycle_at": _iso(self.next_trade_cycle_at),
            "trading_cycles": self.trading_cycles,
        }
        if clock is not None:  # orari ufficiali di Alpaca (festività e chiusure anticipate incluse)
            row.update(
                market_is_open=bool(clock.is_open),
                market_next_open=_iso(clock.next_open),
                market_next_close=_iso(clock.next_close),
                clock_at=_iso(clock.timestamp),
            )
        return row

    def publish(self, state: str, clock: Any = None, message: str = "") -> None:
        self.storage.update_bot_status(self.row(state, clock, message))


@dataclass
class RunContext:
    """Esito del run: viene sempre registrato su Supabase (stock_bot_runs) e su GitHub Actions."""

    run_id: str
    storage: SupabaseStorage
    status: str = "STARTED"  # OK | MARKET_CLOSED | CONFIG_ERROR | AUTH_ERROR | ERROR
    message: str = ""
    bot_status: BotStatus | None = None
    last_clock: Any = None

    def finish(self, status: str, message: str) -> None:
        self.status, self.message = status, message


def gh_annotation(level: str, title: str, message: str) -> None:
    """Annotazione visibile nella pagina del run su GitHub Actions (::notice / ::warning / ::error)."""
    if os.getenv("GITHUB_ACTIONS") == "true":
        clean = message.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")
        print(f"::{level} title={title}::{clean}", flush=True)


def write_step_summary(ctx: RunContext, settings: Settings) -> None:
    path = os.getenv("GITHUB_STEP_SUMMARY")
    if not path:
        return
    icon = {"OK": "✅", "MARKET_CLOSED": "🌙"}.get(ctx.status, "❌")
    db = "attivo" if ctx.storage.enabled and not ctx.storage.error_count else (
        f"{ctx.storage.error_count} scritture fallite" if ctx.storage.enabled else "NON attivo: nessun dato salvato"
    )
    lines = [
        f"### {icon} Stock Bot - {ctx.status}", "", "| | |", "|---|---|",
        f"| Run | `{ctx.run_id}` |",
        f"| Modalità | {'DRY_RUN' if settings.DRY_RUN else 'PAPER TRADING'} |",
        f"| Esito | {ctx.message} |",
        f"| Supabase | {db} |", "",
    ]
    try:
        with open(path, "a", encoding="utf-8") as fh:
            fh.write("\n".join(lines))
    except OSError:
        pass


@dataclass
class Services:
    trading: TradingClient
    market: MarketDataService
    executor: OrderExecutor
    risk: RiskManager
    selector: StrategySelector
    storage: SupabaseStorage


@dataclass
class SessionStats:
    trading_cycles: int = 0
    refresh_cycles: int = 0
    orders_sent: int = 0
    orders_filled: int = 0
    positions_closed: int = 0
    errors: int = 0


def account_snapshot(svc: Services, verbose: bool = False) -> tuple[float, float]:
    """Legge l'account Alpaca, salva lo snapshot dell'equity e ritorna (equity, buying power)."""
    account = svc.trading.get_account()
    equity = float(account.equity or 0)
    cash = float(account.cash or 0)
    last_equity = float(account.last_equity or equity)
    buying_power = float(account.buying_power or 0)
    long_value = float(account.long_market_value or 0)
    short_value = float(account.short_market_value or 0)
    if verbose:
        logger.info("Account {} | buying power ${:,.2f} | shorting {} | PDT {}",
                    account.status.value if account.status else "?", buying_power,
                    "abilitato" if account.shorting_enabled else "NON abilitato",
                    "sì" if account.pattern_day_trader else "no")
    svc.storage.save_equity_snapshot(equity, cash, last_equity, long_value, short_value)
    return equity, buying_power


@dataclass
class TradingWindow:
    dynamic_allowed: bool  # stop dinamici e uscite volontarie
    new_trades_allowed: bool
    reason: str = ""
    resume_at: datetime | None = None  # quando finisce la pausa (UTC)


def trading_window(now: datetime, next_close: datetime | None, settings: Settings) -> TradingWindow:
    """
    Finestre in cui il bot NON apre trade, anche a mercato aperto:
      - primi NO_TRADE_AFTER_OPEN_MIN minuti dopo l'apertura (9:30 New York): spread larghi e prezzi
        nervosi -> niente nuovi trade, stop dinamici o uscite volontarie (la protezione OCO resta attiva);
      - ultimi NO_NEW_TRADES_BEFORE_CLOSE_MIN minuti prima della chiusura -> niente nuovi trade.
    """
    ny = now.astimezone(NEW_YORK)
    opening = ny.replace(hour=9, minute=30, second=0, microsecond=0)
    resume = opening + timedelta(minutes=settings.NO_TRADE_AFTER_OPEN_MIN)
    if opening <= ny < resume:
        return TradingWindow(
            False, False,
            f"primi {settings.NO_TRADE_AFTER_OPEN_MIN} min dopo l'apertura: si opera dalle {resume:%H:%M} di New York",
            resume.astimezone(timezone.utc),
        )
    if next_close is not None:
        to_close_min = (next_close - now).total_seconds() / 60
        if to_close_min <= settings.NO_NEW_TRADES_BEFORE_CLOSE_MIN:
            return TradingWindow(True, False,
                                 f"ultimi {settings.NO_NEW_TRADES_BEFORE_CLOSE_MIN} min prima della chiusura: niente nuovi trade")
    return TradingWindow(True, True)


# ---------------------------------------------------------------------- #
# Cicli
# ---------------------------------------------------------------------- #
def refresh_cycle(svc: Services, stats: SessionStats, allow_dynamic: bool) -> None:
    """Ciclo leggero (ogni REFRESH_INTERVAL_SEC): P&L, protezione, stop dinamici, time stop."""
    report = svc.risk.manage_open_positions(regimes=None, allow_dynamic=allow_dynamic)
    stats.positions_closed += len(report.closed)
    stats.refresh_cycles += 1


def try_open(settings: Settings, svc: Services, cand: Candidate, equity: float, buying_power: float,
             exposure_room: float) -> tuple[str, float, bool]:
    """Prova ad aprire un candidato. Ritorna (esito testuale, controvalore impegnato, eseguito)."""
    ticker = cand.regime.ticker
    # Il prezzo dello screening può avere qualche minuto: lo aggiorno e ricontrollo il segnale
    fresh = svc.market.get_reference_prices([ticker]).get(ticker)
    regime = cand.regime
    if fresh and abs(fresh - regime.price) > 1e-9:
        logger.info("{}: prezzo aggiornato {:.2f} -> {:.2f}", ticker, regime.price, fresh)
        regime = regime.model_copy(update={"price": fresh})
    signal = evaluate_signal(cand.strategy, regime, settings)
    if not signal.triggered:
        return f"segnale svanito al prezzo aggiornato ({signal.text})", 0.0, False

    plan = svc.selector.build_plan(regime, cand.strategy, signal, equity, exposure_room, buying_power)
    if plan is None:
        return "nessun piano valido (rischio/rendimento o limiti di esposizione)", 0.0, False

    label = f"OPEN {ticker} {plan.strategy.value}"
    side = OrderSide.BUY if plan.is_long else OrderSide.SELL
    result = svc.executor.execute(ticker, side, plan.qty, plan.ref_price, label, settings.MAX_ENTRY_SLIPPAGE_PCT)
    if result.dry_run:
        return (f"{plan.side.upper()} x{plan.qty} (DRY_RUN, non inviato) | stop {plan.stop_price:.2f} | "
                f"target {plan.take_profit:.2f} ({plan.reward_risk:.1f}R)"), 0.0, False
    if result.filled_qty <= 0 or result.avg_price is None:
        return f"NON eseguito: {result.message}", 0.0, False

    stop, target = plan.levels_from_fill(result.avg_price)
    prot = svc.executor.protect(ticker, plan.is_long, result.filled_qty, stop, target, label)
    if not prot.active:
        logger.error("[{}] posizione aperta SENZA protezione: il risk manager riproverà al prossimo controllo", label)
    svc.storage.insert_trade(plan_to_trade_row(plan, regime, result.avg_price, result.filled_qty, stop, target,
                                               result.order_id, prot, settings))
    return (f"ESEGUITO {plan.side.upper()} {result.filled_qty} @ {result.avg_price:.2f} | stop {stop:.2f} | "
            f"target {target:.2f}"), result.avg_price * result.filled_qty, True


def trading_cycle(settings: Settings, svc: Services, run_id: str, equity: float, buying_power: float, stats: SessionStats,
                  allow_new_trades: bool = True, pause_reason: str = "") -> str:
    """Ciclo completo: screening + gestione posizioni + nuovi trade. Ritorna il riepilogo testuale."""
    held = svc.risk.held_symbols()
    output = Screener(settings, svc.market).run(extra_symbols=held, shortable=svc.executor.is_shortable)
    report = svc.risk.manage_open_positions(regimes=output.regimes, allow_dynamic=True)
    stats.positions_closed += len(report.closed)

    # Filtri di portafoglio
    candidates: list[Candidate] = []
    for cand in output.ranked:
        t = cand.regime.ticker
        if t in report.occupied:
            cand.row.reason = "Posizione/ordine già aperto sul ticker"
        elif t in report.cooldown:
            cand.row.reason = f"In pausa dopo uno stop loss (per {settings.STOP_LOSS_COOLDOWN_HOURS} ore)"
        else:
            candidates.append(cand)

    slots = min(settings.MAX_NEW_TRADES_PER_RUN, settings.MAX_OPEN_POSITIONS - report.open_count)
    exposure_room = settings.MAX_GROSS_EXPOSURE_PCT * equity - report.gross_exposure
    attempted = filled = 0
    if not allow_new_trades:
        logger.info("Nessun nuovo trade in questo ciclo: {}", pause_reason)
        for cand in candidates:
            cand.row.reason = f"Idoneo, ma {pause_reason}"
    elif slots <= 0:
        logger.info("Nessuno slot libero ({} posizioni aperte, max {})", report.open_count, settings.MAX_OPEN_POSITIONS)
        for cand in candidates:
            cand.row.reason = "Idoneo ma limite posizioni raggiunto"
    elif not candidates:
        logger.info("Nessun candidato idoneo in questo ciclo: il bot resta fermo")
    else:
        logger.info("=== NUOVI TRADE: {} candidati, {} slot, spazio di esposizione ${:,.0f} ===",
                    len(candidates), slots, exposure_room)
        for cand in candidates:
            if attempted >= slots:
                cand.row.reason = "Idoneo, non selezionato (slot esauriti)"
                continue
            outcome, committed, ok = try_open(settings, svc, cand, equity, buying_power, exposure_room)
            if outcome.startswith(("segnale svanito", "nessun piano")):
                cand.row.reason = f"Idoneo, ma {outcome}"
                continue
            attempted += 1
            cand.row.selected = True
            cand.row.reason = f"Selezionato: {outcome}"
            if ok:
                filled += 1
                exposure_room -= committed
                buying_power -= committed

    svc.storage.save_screening_logs([r.to_record() for r in output.rows], run_id)
    stats.orders_sent += attempted
    stats.orders_filled += filled
    stats.trading_cycles += 1
    return (
        f"Equity ${equity:,.0f} | {len(output.rows)} ticker analizzati, {len(output.ranked)} con segnale | "
        f"posizioni chiuse {len(report.closed)} | "
        + (f"ordini simulati {attempted}" if settings.DRY_RUN else f"ordini inviati {attempted}, eseguiti {filled}")
        + f" | posizioni aperte {report.open_count + filled}"
    )


def wait_for_open(settings: Settings, trading: TradingClient, ctx: RunContext | None = None) -> bool:
    """Modalità sessione: se l'apertura è vicina (<= SESSION_WAIT_FOR_OPEN_MIN) la aspetta. True se aperto."""
    clock = trading.get_clock()
    if ctx is not None:
        ctx.last_clock = clock
    if clock.is_open:
        return True
    wait_s = (clock.next_open - clock.timestamp).total_seconds()
    if 0 < wait_s <= settings.SESSION_WAIT_FOR_OPEN_MIN * 60:
        logger.info("Mercato chiuso, apre tra {:.0f} minuti: attendo l'apertura", wait_s / 60)
        end = time.monotonic() + wait_s + 15
        while (left := end - time.monotonic()) > 0:
            if ctx is not None and ctx.bot_status is not None:
                ctx.bot_status.publish("WAITING_OPEN", clock, f"In attesa dell'apertura ({left / 60:.0f} min)")
            time.sleep(min(60.0, left))
        clock = trading.get_clock()
        if ctx is not None:
            ctx.last_clock = clock
        return bool(clock.is_open)
    return False


def run_session(settings: Settings, ctx: RunContext, svc: Services) -> str:
    """
    Modalità sessione: un solo job resta attivo per tutta la sessione di mercato.
      - ogni REFRESH_INTERVAL_SEC: snapshot equity + posizioni (protezione, stop dinamici, time stop)
      - ogni TRADE_INTERVAL_MIN:  ciclo completo (screening + uscite di regime + nuovi trade)
    Termina a mercato chiuso o dopo SESSION_MAX_MINUTES (limite di durata dei job GitHub: 6 ore).
    """
    stats = SessionStats()
    deadline = time.monotonic() + settings.SESSION_MAX_MINUTES * 60
    status = ctx.bot_status or BotStatus(ctx.storage, settings, ctx.run_id)
    started = datetime.now(timezone.utc)
    status.session_started_at = started
    status.session_deadline = started + timedelta(minutes=settings.SESSION_MAX_MINUTES)
    next_trading = 0.0
    consecutive_errors = 0
    end_reason = ""
    pause_logged = False
    logger.info("Modalità SESSIONE: aggiornamento ogni {}s, trading ogni {} min, durata massima {} min",
                settings.REFRESH_INTERVAL_SEC, settings.TRADE_INTERVAL_MIN, settings.SESSION_MAX_MINUTES)

    while True:
        tick = time.monotonic()
        if tick >= deadline:
            end_reason = "durata massima raggiunta, il prossimo run schedulato riprende"
            break
        try:
            clock = svc.trading.get_clock()
            ctx.last_clock = clock
            test_mode = settings.DRY_RUN and settings.IGNORE_MARKET_HOURS and stats.trading_cycles == 0
            if not clock.is_open and not test_mode:
                end_reason = "mercato chiuso"
                break
            equity, buying_power = account_snapshot(svc, verbose=stats.trading_cycles == 0)
            window = TradingWindow(True, True) if test_mode else trading_window(
                datetime.now(timezone.utc), clock.next_close, settings)
            if not window.dynamic_allowed:
                # Pausa dopo l'apertura: solo equity e verifica che la protezione lato broker ci sia
                if not pause_logged:
                    logger.info("Pausa operativa: {}", window.reason)
                    pause_logged = True
                refresh_cycle(svc, stats, allow_dynamic=False)
                status.next_trade_cycle_at = window.resume_at
                status.publish("RUNNING", clock, f"Pausa: {window.reason}")
            elif tick >= next_trading:
                logger.info("----- Ciclo di trading #{} -----", stats.trading_cycles + 1)
                summary = trading_cycle(settings, svc, ctx.run_id, equity, buying_power, stats,
                                        window.new_trades_allowed, window.reason)
                logger.info("Ciclo di trading #{}: {}", stats.trading_cycles, summary)
                svc.storage.log_run(ctx.run_id, "OK", f"[ciclo {stats.trading_cycles}] {summary}", settings.DRY_RUN)
                next_trading = tick + settings.TRADE_INTERVAL_MIN * 60
                status.trading_cycles = stats.trading_cycles
                status.last_trade_cycle_at = datetime.now(timezone.utc)
                status.next_trade_cycle_at = status.last_trade_cycle_at + timedelta(
                    seconds=max(0.0, next_trading - time.monotonic()))
                if test_mode:
                    end_reason = "test a mercato chiuso (DRY_RUN + IGNORE_MARKET_HOURS), un solo ciclo"
                    break
            else:
                refresh_cycle(svc, stats, allow_dynamic=True)
            if window.dynamic_allowed:
                status.publish("RUNNING", clock, f"Sessione in corso: {stats.trading_cycles} cicli di trading")
            consecutive_errors = 0
        except APIError as exc:
            if getattr(exc, "status_code", None) in (401, 403):
                raise
            consecutive_errors += 1
            stats.errors += 1
            logger.error("Errore API Alpaca nel ciclo ({}/5 consecutivi): {}", consecutive_errors, str(exc)[:300])
        except Exception as exc:  # noqa: BLE001 - un ciclo fallito non deve fermare la sessione
            consecutive_errors += 1
            stats.errors += 1
            logger.exception("Errore nel ciclo ({}/5 consecutivi): {}: {}", consecutive_errors, type(exc).__name__, exc)
        if consecutive_errors >= 5:
            raise RuntimeError("5 errori consecutivi: sessione interrotta")

        time.sleep(max(1.0, settings.REFRESH_INTERVAL_SEC - (time.monotonic() - tick)))

    trades_txt = (f"ordini simulati {stats.orders_sent}" if settings.DRY_RUN
                  else f"ordini inviati {stats.orders_sent}, eseguiti {stats.orders_filled}")
    return (f"Sessione terminata ({end_reason}): {stats.trading_cycles} cicli di trading, "
            f"{stats.refresh_cycles} aggiornamenti | {trades_txt} | posizioni chiuse {stats.positions_closed} | "
            f"errori {stats.errors}")


def run(settings: Settings, ctx: RunContext) -> int:
    mode = "DRY_RUN (nessun ordine verrà inviato)" if settings.DRY_RUN else "PAPER TRADING (ordini reali sull'account paper)"
    logger.info("=" * 70)
    logger.info("Alpaca Stock Bot | run {} | modalità: {} | {}", ctx.run_id, mode,
                "SESSIONE continua" if settings.SESSION_MODE else "run singolo")
    logger.info("=" * 70)

    if not PAPER_TRADING:  # salvaguardia: il bot non deve MAI operare su un conto reale
        logger.critical("PAPER_TRADING disattivato: esecuzione bloccata")
        ctx.finish("CONFIG_ERROR", "PAPER_TRADING disattivato")
        return 1
    if not settings.alpaca_configured:
        logger.error("ALPACA_API_KEY / ALPACA_SECRET_KEY mancanti: configura .env o i GitHub Secrets")
        ctx.finish("CONFIG_ERROR", "Chiavi Alpaca mancanti")
        return 1

    trading = TradingClient(
        api_key=settings.ALPACA_API_KEY.get_secret_value(),
        secret_key=settings.ALPACA_SECRET_KEY.get_secret_value(),
        paper=True,  # forzato: mai live
    )

    if settings.SESSION_MODE:
        is_open = wait_for_open(settings, trading, ctx)
    else:
        ctx.last_clock = trading.get_clock()
        is_open = ctx.last_clock.is_open
    if not is_open:
        if settings.DRY_RUN and settings.IGNORE_MARKET_HOURS:
            logger.warning("Mercato CHIUSO, ma DRY_RUN + IGNORE_MARKET_HOURS: proseguo a scopo di test")
        else:
            clock = trading.get_clock()
            ctx.last_clock = clock
            next_open = clock.next_open.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
            logger.info("Mercato USA chiuso (prossima apertura: {}). Nessuna azione, esco.", next_open)
            ctx.finish("MARKET_CLOSED", f"Mercato chiuso, prossima apertura {next_open}. Nessuna operazione.")
            return 0
    else:
        logger.info("Mercato aperto")

    market = MarketDataService(settings)
    executor = OrderExecutor(trading, settings)
    storage = ctx.storage
    svc = Services(
        trading=trading, market=market, executor=executor,
        risk=RiskManager(settings, trading, market, executor, storage),
        selector=StrategySelector(settings), storage=storage,
    )

    if settings.SESSION_MODE:
        summary = run_session(settings, ctx, svc)
    else:
        equity, buying_power = account_snapshot(svc, verbose=True)
        test_mode = settings.DRY_RUN and settings.IGNORE_MARKET_HOURS
        clock = ctx.last_clock
        window = TradingWindow(True, True) if test_mode or clock is None else trading_window(
            datetime.now(timezone.utc), clock.next_close, settings)
        if not window.dynamic_allowed:
            logger.info("Pausa operativa: {}. Controllo solo la protezione delle posizioni.", window.reason)
            refresh_cycle(svc, SessionStats(), allow_dynamic=False)
            ctx.finish("OK", f"Pausa operativa: {window.reason}")
            return 0
        summary = trading_cycle(settings, svc, ctx.run_id, equity, buying_power, SessionStats(),
                                window.new_trades_allowed, window.reason)
    logger.info("=" * 70)
    logger.info("Fine run {} | {}", ctx.run_id, summary)
    logger.info("=" * 70)
    ctx.finish("OK", summary)
    return 0


def main() -> int:
    try:
        settings = get_settings()
    except ValidationError as exc:
        problems = "; ".join(f"{'.'.join(str(p) for p in e['loc'])}: {e['msg']}" for e in exc.errors(include_input=False))
        print(f"Configurazione non valida -> {problems}", file=sys.stderr)
        gh_annotation("error", "Configurazione non valida", problems)
        return 1
    setup_logging(settings)
    logger.info(
        "Parametri: MAX_NEW_TRADES_PER_RUN={} | MAX_OPEN_POSITIONS={} | MAX_RISK_PER_TRADE_PCT={:.2%} | "
        "MAX_POSITION_PCT={:.0%} | MAX_GROSS_EXPOSURE_PCT={:.0%} | MIN_OPPORTUNITY_SCORE={:g} | ALLOW_SHORT={} | WATCHLIST={}",
        settings.MAX_NEW_TRADES_PER_RUN, settings.MAX_OPEN_POSITIONS, settings.MAX_RISK_PER_TRADE_PCT,
        settings.MAX_POSITION_PCT, settings.MAX_GROSS_EXPOSURE_PCT, settings.MIN_OPPORTUNITY_SCORE,
        settings.ALLOW_SHORT, ",".join(settings.WATCHLIST),
    )
    logger.info(
        "Uscite: target trend {:g}R | breakeven a {:g}R | trailing da {:g}R a {:g} ATR | time stop {} / {} giorni | "
        "inversione di trend {}",
        settings.TAKE_PROFIT_R_TREND, settings.BREAKEVEN_AT_R, settings.TRAIL_START_R, settings.TRAIL_ATR_MULT,
        settings.MAX_HOLDING_DAYS_TREND, settings.MAX_HOLDING_DAYS_MR, "sì" if settings.EXIT_ON_TREND_REVERSAL else "no",
    )
    ctx = RunContext(run_id=uuid.uuid4().hex[:12], storage=SupabaseStorage(settings))
    ctx.bot_status = BotStatus(ctx.storage, settings, ctx.run_id)
    code = 1
    try:
        code = run(settings, ctx)
    except APIError as exc:
        status = getattr(exc, "status_code", None)
        if status in (401, 403):
            logger.error("Alpaca ha rifiutato le credenziali (HTTP {}). Usa le chiavi dell'account PAPER "
                         "(iniziano con 'PK') e rigenerale dopo un reset dell'account.", status)
            ctx.finish("AUTH_ERROR", f"Credenziali Alpaca rifiutate (HTTP {status})")
        else:
            logger.error("Errore API Alpaca (HTTP {}): {}", status, str(exc)[:300])
            ctx.finish("ERROR", f"Errore API Alpaca (HTTP {status})")
    except Exception as exc:  # noqa: BLE001
        logger.exception("Errore non gestito: {}: {}", type(exc).__name__, exc)
        ctx.finish("ERROR", f"{type(exc).__name__}: {str(exc)[:200]}")
    finally:
        ctx.storage.log_run(ctx.run_id, ctx.status, ctx.message, settings.DRY_RUN)
        ctx.bot_status.next_trade_cycle_at = None
        ctx.bot_status.publish("STOPPED", ctx.last_clock, f"{ctx.status}: {ctx.message}")
        level = {"OK": "notice", "MARKET_CLOSED": "notice"}.get(ctx.status, "error")
        gh_annotation(level, f"Bot: {ctx.status}", ctx.message)
        if not ctx.storage.enabled:
            gh_annotation("warning", "Supabase non attivo",
                          "Nessun dato salvato: controlla i secret SUPABASE_URL (https://xxxx.supabase.co) e SUPABASE_SERVICE_ROLE_KEY")
        elif ctx.storage.error_count:
            gh_annotation("warning", "Supabase", f"{ctx.storage.error_count} scritture fallite: vedi il log dello step 'Run bot'")
        write_step_summary(ctx, settings)
    return code


if __name__ == "__main__":
    sys.exit(main())
