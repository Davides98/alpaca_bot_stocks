"""
Modulo 6 - Persistenza su Supabase (PostgreSQL).

Tutte le tabelle hanno il prefisso `stock_`: il bot azionario può usare lo STESSO progetto Supabase
del bot opzioni senza conflitti (piano Free: un solo progetto basta per entrambi).

Usa la SERVICE ROLE key (bypassa la RLS) ed è quindi l'unico componente autorizzato a scrivere.
Salvataggio resiliente: se Supabase non è configurato o una chiamata fallisce, l'errore viene
loggato e il bot prosegue normalmente.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any, TypeVar

from loguru import logger

from config.settings import Settings

T = TypeVar("T")

T_EQUITY = "stock_equity_snapshots"
T_TRADES = "stock_trades"
T_SCREENING = "stock_screening_logs"
T_RUNS = "stock_bot_runs"
T_STATUS = "stock_bot_status"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class SupabaseStorage:
    def __init__(self, settings: Settings) -> None:
        self.s = settings
        self.client: Any = None
        self.error_count = 0
        self._status_disabled = False
        if not settings.supabase_configured:
            logger.warning("Supabase non configurato (SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY): nessun dato verrà salvato")
            return
        if not settings.SUPABASE_URL.startswith("https://"):
            hint = " (sembra una chiave API, non un URL)" if settings.SUPABASE_URL.startswith(("sb_", "eyJ")) else ""
            logger.error("SUPABASE_URL non valido{}: deve essere il 'Project URL', es. https://abcdefgh.supabase.co. "
                         "Nessun dato verrà salvato.", hint)
            return
        if settings.SUPABASE_SERVICE_ROLE_KEY.get_secret_value().startswith("sb_publishable_"):
            logger.error("SUPABASE_SERVICE_ROLE_KEY contiene la chiave publishable: serve la chiave secret (sb_secret_...) per scrivere")
            return
        try:
            from supabase import create_client

            self.client = create_client(settings.SUPABASE_URL, settings.SUPABASE_SERVICE_ROLE_KEY.get_secret_value())
            logger.info("Connesso a Supabase")
        except Exception as exc:  # noqa: BLE001
            logger.error("Connessione a Supabase fallita: {}", type(exc).__name__)
            self.client = None

    @property
    def enabled(self) -> bool:
        return self.client is not None

    def _safe(self, op: str, fn: Callable[[], T], default: T) -> T:
        if not self.enabled:
            return default
        try:
            return fn()
        except Exception as exc:  # noqa: BLE001 - mai far crashare il bot per il DB
            self.error_count += 1
            logger.error("Supabase [{}] fallito: {}: {}", op, type(exc).__name__, str(exc)[:300])
            return default

    # ------------------------------------------------------------------ #
    # stock_bot_runs: un record per OGNI run, anche a mercato chiuso o in errore
    # ------------------------------------------------------------------ #
    def log_run(self, run_id: str, status: str, message: str, dry_run: bool) -> None:
        row = {
            "timestamp": _now_iso(),
            "run_id": run_id,
            "status": status,
            "message": message[:500],
            "dry_run": dry_run,
            "trigger": os.getenv("BOT_TRIGGER") or os.getenv("GITHUB_EVENT_NAME", "local"),
        }
        self._safe("bot_runs.insert", lambda: self.client.table(T_RUNS).insert(row).execute(), None)

    # ------------------------------------------------------------------ #
    # stock_bot_status: UNA sola riga (id = 1) con lo stato attuale del bot (contatori della dashboard)
    # ------------------------------------------------------------------ #
    def update_bot_status(self, fields: dict[str, Any]) -> None:
        if not self.enabled or self._status_disabled:
            return
        row = {"id": 1, "updated_at": _now_iso(), **fields}
        try:
            self.client.table(T_STATUS).upsert(row, on_conflict="id").execute()
        except Exception as exc:  # noqa: BLE001
            self._status_disabled = True
            logger.warning("Supabase [{}] non disponibile ({}): esegui supabase/schema.sql. Il bot prosegue.",
                           T_STATUS, str(exc)[:200])

    # ------------------------------------------------------------------ #
    # stock_equity_snapshots
    # ------------------------------------------------------------------ #
    def save_equity_snapshot(self, equity: float, cash: float, last_equity: float,
                             long_value: float = 0.0, short_value: float = 0.0, positions: int = 0) -> None:
        initial = self.s.INITIAL_CAPITAL
        total_usd = equity - initial
        row = {
            "timestamp": _now_iso(),
            "equity": round(equity, 2),
            "cash": round(cash, 2),
            "initial_capital": round(initial, 2),
            "total_return_usd": round(total_usd, 2),
            "total_return_pct": round(total_usd / initial * 100.0, 4) if initial else 0.0,
            "daily_pnl": round(equity - last_equity, 2),
            "long_market_value": round(long_value, 2),
            "short_market_value": round(short_value, 2),
            "positions_count": positions,
        }
        logger.info(
            "Equity ${:,.2f} | cash ${:,.2f} | long ${:,.0f} / short ${:,.0f} | rendimento ${:+,.2f} ({:+.2f}%) | "
            "P&L giornaliero ${:+,.2f}",
            row["equity"], row["cash"], long_value, abs(short_value), row["total_return_usd"], row["total_return_pct"],
            row["daily_pnl"],
        )
        self._safe("equity_snapshots.insert", lambda: self.client.table(T_EQUITY).insert(row).execute(), None)

    # ------------------------------------------------------------------ #
    # stock_screening_logs
    # ------------------------------------------------------------------ #
    def save_screening_logs(self, rows: list[dict[str, Any]], run_id: str) -> None:
        if not rows:
            return
        ts = _now_iso()
        payload = [{**r, "timestamp": ts, "run_id": run_id} for r in rows]
        if self._safe("screening_logs.insert", lambda: self.client.table(T_SCREENING).insert(payload).execute(), None):
            logger.info("Salvati {} record di screening (run {})", len(payload), run_id)

    # ------------------------------------------------------------------ #
    # stock_trades
    # ------------------------------------------------------------------ #
    def insert_trade(self, trade: dict[str, Any]) -> int | None:
        row = {"created_at": _now_iso(), "updated_at": _now_iso(), "status": "OPEN", **trade}

        def _do() -> int | None:
            res = self.client.table(T_TRADES).insert(row).execute()
            return res.data[0]["id"] if res.data else None

        trade_id = self._safe("trades.insert", _do, None)
        if trade_id is not None:
            logger.info("Trade #{} salvato su Supabase", trade_id)
        return trade_id

    def get_open_trades(self) -> list[dict[str, Any]]:
        return self._safe(
            "trades.select_open",
            lambda: self.client.table(T_TRADES).select("*").eq("status", "OPEN").execute().data or [],
            [],
        )

    def get_recent_stop_tickers(self, since: datetime) -> list[str]:
        """Ticker dei trade chiusi in STOP_LOSS dopo `since` (per la pausa post stop loss)."""
        def _do() -> list[str]:
            res = (self.client.table(T_TRADES).select("ticker")
                   .eq("close_reason", "STOP_LOSS").gte("closed_at", since.isoformat()).execute())
            return [r["ticker"] for r in (res.data or [])]

        return self._safe("trades.select_recent_stops", _do, [])

    def update_trade(self, trade_id: int, fields: dict[str, Any]) -> None:
        payload = {**fields, "updated_at": _now_iso()}
        self._safe("trades.update", lambda: self.client.table(T_TRADES).update(payload).eq("id", trade_id).execute(), None)

    def close_trade(self, trade_id: int, exit_price: float | None, pnl: float | None, pnl_pct: float | None, reason: str) -> None:
        self.update_trade(trade_id, {
            "status": "CLOSED",
            "closed_at": _now_iso(),
            "exit_price": exit_price,
            "pnl": pnl,
            "pnl_pct": pnl_pct,
            "unrealized_pnl": None,
            "close_reason": reason,
        })
        logger.info("Trade #{} marcato CLOSED ({})", trade_id, reason)
