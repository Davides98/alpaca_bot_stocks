"""
Modulo 5a - Esecuzione ordini (azioni).

Regole:
- MAI ordini Market: ingressi e uscite volontarie sono Limit DAY con price chasing, con un tetto di slippage.
- Appena un ingresso viene eseguito, la posizione viene protetta LATO BROKER:
  * quantità intera -> OCO GTC: take profit (limit) + stop loss (stop). Se il bot si ferma, Alpaca chiude
    comunque la posizione. Se l'OCO viene rifiutato si ripiega su un semplice stop GTC;
  * quantità frazionaria -> Alpaca accetta solo ordini DAY senza OCO: stop DAY lato broker (il risk manager
    lo ricrea ogni giorno all'apertura) e take profit eseguito dal bot al raggiungimento del target.
- Chiusure volontarie (time stop, inversione di trend): prima si cancellano gli ordini di protezione
  (che "bloccano" le azioni), poi si invia la chiusura; se non viene eseguita, la protezione viene ripristinata.

Price chasing: si parte dal prezzo di riferimento (mid o ultimo trade) e ogni CHASE_INTERVAL_SEC il limite
si sposta verso il mercato fino al prezzo peggiore accettabile (riferimento ± MAX_*_SLIPPAGE_PCT), poi si cancella.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field

from alpaca.common.exceptions import APIError
from alpaca.trading.client import TradingClient
from alpaca.trading.enums import OrderClass, OrderSide, OrderStatus, OrderType, QueryOrderStatus, TimeInForce
from alpaca.trading.models import Order
from alpaca.trading.requests import (
    GetOrdersRequest,
    LimitOrderRequest,
    ReplaceOrderRequest,
    StopLossRequest,
    StopOrderRequest,
    TakeProfitRequest,
)
from loguru import logger

from config.settings import Settings

TERMINAL_STATUSES = {
    OrderStatus.FILLED,
    OrderStatus.CANCELED,
    OrderStatus.EXPIRED,
    OrderStatus.REJECTED,
    OrderStatus.DONE_FOR_DAY,
    OrderStatus.REPLACED,
}

# Prefissi del client_order_id: permettono di riconoscere gli ordini del bot
PREFIX_ENTRY = "stk-in"
PREFIX_OCO = "stk-oco"
PREFIX_STOP = "stk-sl"
PREFIX_EXIT = "stk-out"


def new_client_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:18]}"


def worst_price(ref: float, side: OrderSide, slippage_pct: float) -> float:
    """Prezzo limite peggiore accettabile: chi compra paga al massimo ref x (1 + slip), chi vende incassa almeno ref x (1 - slip)."""
    return round(ref * (1 + slippage_pct), 2) if side == OrderSide.BUY else round(ref * (1 - slippage_pct), 2)


def price_ladder(ref: float, worst: float, attempts: int) -> list[float]:
    """
    Prezzi limite del chasing, distribuiti in modo uniforme dal riferimento al prezzo peggiore accettabile.
    Per un acquisto la scala è crescente, per una vendita decrescente; mai duplicati.
    """
    ref, worst = round(ref, 2), round(worst, 2)
    if attempts <= 1 or ref == worst:
        return [ref]
    ladder: list[float] = []
    for i in range(attempts):
        p = round(ref + (worst - ref) * i / (attempts - 1), 2)
        if not ladder or p != ladder[-1]:
            ladder.append(p)
    return ladder


@dataclass
class ExecutionResult:
    requested_qty: float
    filled_qty: float = 0.0
    avg_price: float | None = None
    order_id: str | None = None
    status: str = ""
    message: str = ""
    dry_run: bool = False
    ladder: list[float] = field(default_factory=list)

    @property
    def filled(self) -> bool:
        return self.requested_qty > 0 and self.filled_qty >= self.requested_qty - 1e-9


@dataclass
class Protection:
    """Ordini di protezione aperti su un simbolo (OCO o solo stop)."""

    stop_order_id: str | None = None
    stop_price: float | None = None
    tp_order_id: str | None = None
    tp_price: float | None = None
    parent_id: str | None = None

    @property
    def active(self) -> bool:
        return self.stop_price is not None and (self.stop_order_id is not None or self.parent_id is not None)


class OrderExecutor:
    def __init__(self, trading_client: TradingClient, settings: Settings) -> None:
        self.client = trading_client
        self.s = settings
        self._shortable: dict[str, bool] = {}
        self._fractionable: dict[str, bool] = {}
        self._broker_stop_rejected: set[str] = set()  # simboli su cui Alpaca rifiuta lo stop frazionario (per la sessione)

    # ------------------------------------------------------------------ #
    # Informazioni sugli asset
    # ------------------------------------------------------------------ #
    def is_shortable(self, symbol: str) -> bool:
        if symbol not in self._shortable:
            try:
                asset = self.client.get_asset(symbol)
                self._shortable[symbol] = bool(asset.tradable and asset.shortable and asset.easy_to_borrow)
            except Exception as exc:  # noqa: BLE001
                logger.warning("{}: impossibile verificare lo short ({})", symbol, exc)
                self._shortable[symbol] = False
        return self._shortable[symbol]

    def is_fractionable(self, symbol: str) -> bool:
        if symbol not in self._fractionable:
            try:
                self._fractionable[symbol] = bool(getattr(self.client.get_asset(symbol), "fractionable", False))
            except Exception as exc:  # noqa: BLE001
                logger.warning("{}: impossibile verificare le frazioni ({})", symbol, exc)
                self._fractionable[symbol] = False
        return self._fractionable[symbol]

    # ------------------------------------------------------------------ #
    # Ingressi e uscite con price chasing
    # ------------------------------------------------------------------ #
    def execute(self, symbol: str, side: OrderSide, qty: float, ref_price: float, label: str,
                slippage_pct: float, prefix: str = PREFIX_ENTRY) -> ExecutionResult:
        result = ExecutionResult(requested_qty=qty)
        if qty <= 0 or ref_price <= 0:
            result.message = "Ordine vuoto (qty nulla o prezzo non valido)"
            return result
        worst = worst_price(ref_price, side, slippage_pct)
        ladder = price_ladder(ref_price, worst, self.s.MAX_CHASE_ATTEMPTS)
        result.ladder = ladder
        logger.info(
            "[{}] Ordine LIMIT {} {} x{:g} | riferimento {:.2f} | chasing ({} tentativi ogni {}s): {} | peggiore {:.2f}",
            label, side.value.upper(), symbol, qty, ref_price, len(ladder), self.s.CHASE_INTERVAL_SEC,
            " -> ".join(f"{p:.2f}" for p in ladder), worst,
        )
        if self.s.DRY_RUN:
            result.dry_run = True
            result.avg_price = ladder[0]
            result.message = f"DRY_RUN: ordine NON inviato (chasing {ladder[0]:.2f} -> {ladder[-1]:.2f})"
            logger.warning("[{}] {}", label, result.message)
            return result
        return self._chase(symbol, side, qty, ladder, label, prefix, result)

    def _limit_request(self, symbol: str, side: OrderSide, qty: float, price: float, prefix: str) -> LimitOrderRequest:
        return LimitOrderRequest(
            symbol=symbol, qty=qty, side=side, type=OrderType.LIMIT,
            time_in_force=TimeInForce.DAY, limit_price=round(price, 2),
            client_order_id=new_client_id(prefix),
        )

    def _chase(self, symbol: str, side: OrderSide, qty: float, ladder: list[float], label: str, prefix: str,
               result: ExecutionResult) -> ExecutionResult:
        filled_total, fill_value = 0.0, 0.0  # esecuzioni di ordini precedenti (cancel + nuovo ordine)
        price = ladder[0]
        try:
            order = self.client.submit_order(self._limit_request(symbol, side, qty, price, prefix))
        except APIError as exc:
            result.status, result.message = "rejected", f"Ordine RIFIUTATO da Alpaca: {str(exc)[:200]}"
            logger.error("[{}] {}", label, result.message)
            return result

        order_alive: Order | None = order
        for attempt in range(len(ladder)):
            assert order_alive is not None
            order_alive = self._wait(order_alive, self.s.CHASE_INTERVAL_SEC)
            if order_alive.status == OrderStatus.FILLED:
                break
            if order_alive.status in (OrderStatus.REJECTED, OrderStatus.CANCELED, OrderStatus.EXPIRED):
                logger.error("[{}] Ordine {} terminato con stato {}", label, order_alive.id, order_alive.status.value)
                result.status = "rejected" if order_alive.status == OrderStatus.REJECTED else order_alive.status.value
                break
            if attempt == len(ladder) - 1:
                logger.warning("[{}] Ultimo prezzo ({:.2f}) non eseguito: cancello l'ordine", label, price)
                order_alive = self._cancel_and_wait(order_alive)
                break
            new_price = ladder[attempt + 1]
            logger.info("[{}] Non eseguito in {}s. Chase #{}: {:.2f} -> {:.2f}", label, self.s.CHASE_INTERVAL_SEC,
                        attempt + 1, price, new_price)
            price = new_price
            try:
                order_alive = self.client.replace_order_by_id(order_alive.id, ReplaceOrderRequest(limit_price=round(price, 2)))
            except APIError as exc:
                logger.info("[{}] Replace non disponibile ({}), uso cancel + nuovo ordine", label, str(exc)[:120])
                old = self._cancel_and_wait(order_alive)
                old_filled = float(old.filled_qty or 0)
                if old_filled:
                    filled_total += old_filled
                    fill_value += float(old.filled_avg_price or price) * old_filled
                remaining = round(qty - filled_total, 6)
                if remaining <= 1e-9:
                    order_alive = None
                    break
                try:
                    order_alive = self.client.submit_order(self._limit_request(symbol, side, remaining, price, prefix))
                except APIError as exc2:
                    logger.error("[{}] Nuovo ordine rifiutato: {}", label, exc2)
                    order_alive = None
                    break

        if order_alive is not None:
            n = float(order_alive.filled_qty or 0)
            if n:
                filled_total += n
                fill_value += float(order_alive.filled_avg_price or price) * n
            result.order_id = str(order_alive.id)
            result.status = result.status or order_alive.status.value

        result.filled_qty = filled_total
        result.avg_price = round(fill_value / filled_total, 4) if filled_total else None
        if result.filled:
            result.status = "filled"
            result.message = f"Eseguito {filled_total:g}/{qty:g} @ {result.avg_price:.2f}"
            logger.success("[{}] {}", label, result.message)
        elif filled_total:
            result.message = f"Esecuzione PARZIALE {filled_total:g}/{qty:g} @ {result.avg_price:.2f}"
            logger.warning("[{}] {}", label, result.message)
        elif result.status == "rejected":
            result.message = "Ordine RIFIUTATO da Alpaca (verifica buying power / shortabilità)"
        else:
            result.message = f"Nessuna esecuzione: provati {ladder[0]:.2f} -> {price:.2f}, poi cancellato"
            logger.warning("[{}] {}", label, result.message)
        return result

    # ------------------------------------------------------------------ #
    # Protezione lato broker
    # ------------------------------------------------------------------ #
    def protect(self, symbol: str, is_long: bool, qty: float, stop: float, take_profit: float | None, label: str) -> Protection:
        """
        Quantità intera: OCO GTC (take profit + stop); se rifiutato, solo stop GTC.
        Quantità frazionaria: stop DAY (Alpaca non accetta GTC né OCO sulle frazioni); il target lo esegue il bot.
        """
        exit_side = OrderSide.SELL if is_long else OrderSide.BUY
        if self.s.DRY_RUN:
            logger.warning("[{}] DRY_RUN: protezione NON inviata (stop {:.2f}, target {})", label, stop,
                           f"{take_profit:.2f}" if take_profit else "-")
            return Protection()
        if not float(qty).is_integer():
            if symbol in self._broker_stop_rejected:
                return Protection()  # già rifiutato in questa sessione: stop gestito dal bot
            try:
                order = self.client.submit_order(StopOrderRequest(
                    symbol=symbol, qty=qty, side=exit_side, time_in_force=TimeInForce.DAY,
                    stop_price=round(stop, 2), client_order_id=new_client_id(PREFIX_STOP),
                ))
                logger.success("[{}] Stop DAY lato broker a {:.2f} (qty frazionaria {:g}); target {} gestito dal bot",
                               label, stop, qty, f"{take_profit:.2f}" if take_profit else "-")
                return Protection(stop_order_id=str(order.id), stop_price=stop)
            except APIError as exc:
                logger.warning("[{}] Stop frazionario rifiutato ({}): stop e target li esegue il bot ogni minuto",
                               label, str(exc)[:200])
                self._broker_stop_rejected.add(symbol)
                return Protection()
        if take_profit:
            try:
                order = self.client.submit_order(LimitOrderRequest(
                    symbol=symbol, qty=qty, side=exit_side, type=OrderType.LIMIT,
                    time_in_force=TimeInForce.GTC, order_class=OrderClass.OCO,
                    take_profit=TakeProfitRequest(limit_price=round(take_profit, 2)),
                    stop_loss=StopLossRequest(stop_price=round(stop, 2)),
                    client_order_id=new_client_id(PREFIX_OCO),
                ))
                prot = self.protection_from_orders([order], symbol)
                if not prot.stop_order_id:  # la risposta potrebbe non includere le gambe: le rileggo
                    try:
                        prot = self.protection_from_orders(self.open_orders([symbol]), symbol)
                    except Exception:  # noqa: BLE001
                        pass
                if not prot.active:
                    prot = Protection(parent_id=str(order.id), tp_order_id=str(order.id), tp_price=take_profit, stop_price=stop)
                logger.success("[{}] Protezione OCO attiva: stop {:.2f} / target {:.2f}", label, stop, take_profit)
                return prot
            except APIError as exc:
                logger.warning("[{}] OCO rifiutato ({}): ripiego su un semplice stop GTC", label, str(exc)[:200])
        try:
            order = self.client.submit_order(StopOrderRequest(
                symbol=symbol, qty=qty, side=exit_side, time_in_force=TimeInForce.GTC,
                stop_price=round(stop, 2), client_order_id=new_client_id(PREFIX_STOP),
            ))
            logger.success("[{}] Stop GTC attivo a {:.2f}", label, stop)
            return Protection(stop_order_id=str(order.id), stop_price=stop)
        except APIError as exc:
            logger.error("[{}] Impossibile proteggere la posizione ({}): intervento manuale consigliato", label, str(exc)[:200])
            return Protection()

    @staticmethod
    def protection_from_orders(orders: list[Order], symbol: str) -> Protection:
        """Riconosce stop e take profit tra gli ordini aperti di un simbolo (OCO annidati inclusi)."""
        prot = Protection()
        flat: list[tuple[Order, str | None]] = []
        for o in orders:
            if o.symbol != symbol:
                continue
            flat.append((o, None))
            for leg in o.legs or []:
                flat.append((leg, str(o.id)))
        for o, parent in flat:
            if o.status in TERMINAL_STATUSES:
                continue
            otype = o.order_type or o.type
            if otype in (OrderType.STOP, OrderType.STOP_LIMIT, OrderType.TRAILING_STOP) and o.stop_price is not None:
                prot.stop_order_id, prot.stop_price = str(o.id), float(o.stop_price)
                prot.parent_id = prot.parent_id or parent or str(o.id)
            elif otype == OrderType.LIMIT and (o.order_class == OrderClass.OCO or parent is not None) and o.limit_price is not None:
                prot.tp_order_id, prot.tp_price = str(o.id), float(o.limit_price)
                prot.parent_id = parent or str(o.id)
        return prot

    def open_orders(self, symbols: list[str] | None = None) -> list[Order]:
        req = GetOrdersRequest(status=QueryOrderStatus.OPEN, nested=True, symbols=symbols or None, limit=500)
        return list(self.client.get_orders(req))

    def replace_stop(self, prot: Protection, new_stop: float, symbol: str, is_long: bool, qty: float, label: str) -> Protection:
        """Sposta lo stop (solo in avanti). Prova il replace della gamba stop; se non riesce, ricrea la protezione."""
        if self.s.DRY_RUN:
            logger.warning("[{}] DRY_RUN: stop NON spostato ({} -> {:.2f})", label, prot.stop_price, new_stop)
            return prot
        if prot.stop_order_id:
            try:
                new = self.client.replace_order_by_id(prot.stop_order_id, ReplaceOrderRequest(stop_price=round(new_stop, 2)))
                logger.success("[{}] Stop spostato {:.2f} -> {:.2f}", label, prot.stop_price or 0, new_stop)
                return Protection(stop_order_id=str(new.id), stop_price=new_stop, tp_order_id=prot.tp_order_id,
                                  tp_price=prot.tp_price, parent_id=prot.parent_id)
            except APIError as exc:
                logger.info("[{}] Replace dello stop non disponibile ({}): ricreo la protezione", label, str(exc)[:160])
        self.cancel_symbol_orders(symbol, label)
        return self.protect(symbol, is_long, qty, new_stop, prot.tp_price, label)

    def replace_target(self, prot: Protection, new_tp: float, symbol: str, is_long: bool, qty: float, label: str) -> Protection:
        """
        Sposta il target. Con un OCO prova il replace dell'ordine limit; se non riesce ricrea la protezione.
        Senza ordine target lato broker (posizioni frazionarie) non c'è nulla da modificare: il bot usa il
        nuovo target salvato su Supabase.
        """
        if self.s.DRY_RUN:
            logger.warning("[{}] DRY_RUN: target NON spostato ({} -> {:.2f})", label, prot.tp_price, new_tp)
            return prot
        if not prot.tp_order_id:
            return prot
        try:
            new = self.client.replace_order_by_id(prot.tp_order_id, ReplaceOrderRequest(limit_price=round(new_tp, 2)))
            logger.success("[{}] Target lato broker spostato {:.2f} -> {:.2f}", label, prot.tp_price or 0, new_tp)
            return Protection(stop_order_id=prot.stop_order_id, stop_price=prot.stop_price, tp_order_id=str(new.id),
                              tp_price=new_tp, parent_id=prot.parent_id)
        except APIError as exc:
            logger.info("[{}] Replace del target non disponibile ({}): ricreo la protezione", label, str(exc)[:160])
        if prot.stop_price is None:
            return prot
        self.cancel_symbol_orders(symbol, label)
        return self.protect(symbol, is_long, qty, prot.stop_price, new_tp, label)

    def remove_target(self, prot: Protection, symbol: str, is_long: bool, qty: float, label: str) -> Protection:
        """Toglie il target lasciando solo lo stop (l'OCO diventa uno stop GTC, o DAY se la quantità è frazionaria)."""
        if self.s.DRY_RUN:
            logger.warning("[{}] DRY_RUN: target NON rimosso", label)
            return prot
        if not prot.tp_order_id or prot.stop_price is None:
            return prot  # nessun ordine target lato broker: basta toglierlo su Supabase
        self.cancel_symbol_orders(symbol, label)
        return self.protect(symbol, is_long, qty, prot.stop_price, None, label)

    # ------------------------------------------------------------------ #
    # Chiusura volontaria
    # ------------------------------------------------------------------ #
    def cancel_symbol_orders(self, symbol: str, label: str, timeout: float = 15.0) -> bool:
        """Cancella tutti gli ordini aperti del simbolo e attende la conferma. True se non ne resta nessuno."""
        if self.s.DRY_RUN:
            return True
        try:
            orders = self.open_orders([symbol])
        except Exception as exc:  # noqa: BLE001
            logger.warning("[{}] Impossibile leggere gli ordini aperti ({})", label, exc)
            return False
        ids = {str(o.id) for o in orders} | {str(leg.id) for o in orders for leg in (o.legs or [])}
        for oid in ids:
            try:
                self.client.cancel_order_by_id(oid)
            except APIError as exc:
                logger.debug("Cancel {}: {}", oid, exc)  # tipicamente già cancellato (gamba di un OCO)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if not self.open_orders([symbol]):
                return True
            time.sleep(1.0)
        logger.warning("[{}] Alcuni ordini su {} risultano ancora aperti dopo la cancellazione", label, symbol)
        return False

    def close_position(self, symbol: str, is_long: bool, qty: float, ref_price: float, label: str) -> ExecutionResult:
        side = OrderSide.SELL if is_long else OrderSide.BUY
        if not self.s.DRY_RUN and not self.cancel_symbol_orders(symbol, label):
            return ExecutionResult(requested_qty=qty, status="blocked", message="ordini di protezione non cancellati")
        return self.execute(symbol, side, qty, ref_price, label, self.s.MAX_EXIT_SLIPPAGE_PCT, PREFIX_EXIT)

    # ------------------------------------------------------------------ #
    # Primitive
    # ------------------------------------------------------------------ #
    def _wait(self, order: Order, seconds: float) -> Order:
        deadline = time.monotonic() + seconds
        while True:
            order = self.client.get_order_by_id(order.id)
            if order.status in TERMINAL_STATUSES or time.monotonic() >= deadline:
                return order
            time.sleep(self.s.ORDER_POLL_SEC)

    def _cancel_and_wait(self, order: Order, timeout: float = 10.0) -> Order:
        try:
            self.client.cancel_order_by_id(order.id)
        except APIError as exc:
            logger.debug("Cancel ordine {}: {}", order.id, exc)  # tipicamente già eseguito/cancellato
        deadline = time.monotonic() + timeout
        while True:
            order = self.client.get_order_by_id(order.id)
            if order.status in TERMINAL_STATUSES or time.monotonic() >= deadline:
                return order
            time.sleep(1.0)
