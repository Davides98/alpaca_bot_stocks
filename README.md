# Alpaca Stock Bot (didattico, solo Paper Trading)

Bot autonomo di trading **azionario** che opera **esclusivamente su Alpaca Paper Trading**. È il "gemello" del
[bot opzioni](https://github.com/Davides98/alpaca_bot): stessa architettura (GitHub Actions + Supabase + dashboard su GitHub Pages),
stesso regime detector e strategie azionarie corrispondenti a quelle in opzioni.

Ad ogni ciclo:

1. controlla che il mercato USA sia aperto e salva uno snapshot dell'equity su Supabase;
2. calcola il **regime di mercato** (trend + volatilità) di ogni titolo della watchlist;
3. sceglie la **strategia** dalla matrice e ne verifica il **segnale di ingresso**;
4. dimensiona la posizione sul **rischio** (stop basato su ATR) e invia un ordine **Limit** con *price chasing*;
5. protegge subito la posizione **lato broker** con un ordine **OCO** (stop loss + take profit, GTC);
6. gestisce le posizioni aperte: stop a breakeven, trailing stop, time stop, uscita se il trend si inverte;
7. salva trade e log di screening su Supabase, visibili nella **dashboard web**.

> ⚠️ Progetto a scopo **didattico**. Non è un consiglio finanziario. Il codice forza `paper=True`
> e non può operare su un conto reale.

---

## Architettura

```text
.
├── .github/workflows/run_bot.yml   # sessione continua (avviata dallo scheduler Supabase) + trigger manuale
├── config/settings.py              # tutti i parametri (pydantic-settings, sovrascrivibili da env)
├── supabase/schema.sql             # tabelle stock_* + indici + RLS (anon = sola lettura) + realtime
├── supabase/scheduler.sql          # pg_cron + pg_net: riavvia il bot se è fermo durante la sessione
├── docs/index.html                 # dashboard (Tailwind + Chart.js + supabase-js via CDN)
├── src/
│   ├── data/market_data.py         # barre daily complete (SIP ritardato, gratuito) + prezzi live IEX
│   ├── analysis/indicators.py      # EMA, ADX, ATR, RSI, Bollinger, Donchian, HV, Vol Rank
│   ├── analysis/regime.py          # trend (EMA20/50 + ADX) x volatilità (Vol Rank) -> MarketRegime
│   ├── analysis/screener.py        # segnali + Opportunity Score 0-100 + filtri di liquidità
│   ├── strategy/selector.py        # matrice strategie, trigger, stop/target, sizing -> TradePlan
│   ├── execution/executor.py       # ordini Limit con chasing, protezione OCO, chiusure
│   ├── execution/risk_manager.py   # protezione, breakeven/trailing, time stop, riconciliazione
│   ├── storage/supabase_client.py  # scritture resilienti su Supabase
│   └── main.py                     # orchestratore (run singolo o sessione continua)
└── tests/                          # unit test + test end-to-end con broker simulato (nessuna rete)
```

---

## Logica di trading

### Regime di mercato (`regime.py`)

| Dimensione | Regola |
|---|---|
| **Trend** | identica al bot opzioni: `ADX(14) < 20` → `SIDEWAYS`; `ADX >= 20`, `EMA20 > EMA50` e prezzo > EMA20 → `BULLISH`; speculare → `BEARISH`; segnali contrastanti → `SIDEWAYS` |
| **Volatilità** | **Vol Rank** = posizione della volatilità realizzata a 20 giorni nel suo range min–max dell'ultimo anno. `>= 50` → `HIGH_VOL`, altrimenti `LOW_VOL`. È l'equivalente azionario dell'IV Rank, ma calcolato su dati reali (nessuna approssimazione). |

Gli indicatori usano solo **barre daily complete** (la barra di oggi, ancora in formazione, viene scartata);
il prezzo dei segnali è quello **live** (mid della quote se stretta, altrimenti ultimo trade).

### Matrice delle strategie (`selector.py`)

| Trend \ Vol | HIGH_VOL — "compra la debolezza" | LOW_VOL — "compra il movimento" |
|---|---|---|
| **BULLISH** | **Pullback Long** · gemella: Bull Put Spread | **Breakout Long** · gemella: Bull Call Spread |
| **BEARISH** | **Rally Short** · gemella: Bear Call Spread | **Breakdown Short** · gemella: Bear Put Spread |
| **SIDEWAYS** | **Mean Reversion** (long/short alle bande) · gemella: Iron Condor | NO_TRADE |

Il parallelo con le opzioni: con volatilità alta il bot opzioni **vende premio** (strategie a credito, cioè scommette che
l'eccesso rientri); il bot azioni fa lo stesso entrando **contro il movimento di breve** (pullback, estremi del range).
Con volatilità bassa il bot opzioni **compra** direzionalità (debito); il bot azioni compra il **breakout** che segue una compressione.

Il regime dice *cosa* fare, il **segnale** dice *quando*:

| Strategia | Trigger | Stop | Target |
|---|---|---|---|
| Breakout Long / Breakdown Short | prezzo oltre il massimo / minimo a 20 giorni, ma non più di 0,75 ATR (niente rincorse) | 2 ATR | 3R |
| Pullback Long / Rally Short | prezzo entro 1 ATR dalla EMA20, dal lato giusto della EMA50, RSI ≤ 50 (long) / ≥ 50 (short) | 1,5 ATR | 3R |
| Mean Reversion Long / Short | prezzo oltre la banda di Bollinger (20, 2σ) inferiore / superiore | 1,25 ATR | la media a 20 giorni (scartato se < 0,8R) |

`R` = distanza dello stop iniziale (il rischio per azione).

### Opportunity Score (`screener.py`)

`Score = 40% qualità del segnale + 35% trend + 25% volatilità`

- **Segnale:** breakout fresco (vicino al livello), pullback vicino alla EMA20 con RSI scarico, eccesso oltre la banda.
- **Trend:** strategie direzionali → ADX alto (20 → 0 punti, 50 → 100); mean reversion → ADX basso.
- **Volatilità:** `HIGH_VOL` premia Vol Rank alto, `LOW_VOL` premia Vol Rank basso.

Vengono scartati: `SIDEWAYS + LOW_VOL`, ticker senza segnale attivo, prezzo < 10 $, controvalore medio < 50 M$/giorno
(solo con barre SIP), titoli non shortabili (per gli short), ticker già in portafoglio o in pausa dopo uno stop,
score < `MIN_OPPORTUNITY_SCORE`.

### Rischio ed esecuzione

- **Sizing:** `azioni = min(equity × 1% / rischio per azione, equity × 20% / prezzo, esposizione residua / prezzo, buying power / prezzo)`.
  Ogni trade perde al massimo l'1% del capitale allo stop; nessuna posizione supera il 20%; long + short ≤ 100% dell'equity (niente leva).
- **Ingresso:** Limit `DAY`, si parte dal prezzo di riferimento e in 3 tentativi (ogni 10 s) ci si avvicina al mercato
  fino a +0,2% (`MAX_ENTRY_SLIPPAGE_PCT`), poi l'ordine viene cancellato e si riprova al ciclo dopo.
  Il segnale viene ricontrollato sul prezzo aggiornato subito prima dell'ordine.
- **Protezione lato broker:** appena l'ingresso è eseguito, stop e target vengono ricalcolati sul prezzo reale di esecuzione
  e inviati come **OCO GTC**. Se il bot si ferma, Alpaca chiude comunque la posizione. Se l'OCO viene rifiutato, si ripiega su uno stop GTC.
- **Gestione dinamica** (ogni 60 s in sessione, solo in avanti):
  - **breakeven:** a +1R lo stop sale al prezzo di ingresso;
  - **trailing stop** (solo trade di trend): da +1,5R lo stop segue il prezzo a 2,5 ATR;
  - **time stop:** chiusura dopo 30 giorni (trend) o 10 giorni (mean reversion);
  - **inversione di trend:** un long viene chiuso se il regime diventa `BEARISH`, uno short se diventa `BULLISH`;
  - **protezione mancante** (ordine cancellato a mano, scaduto…): viene ricreata; se il prezzo ha già superato lo stop, la posizione viene chiusa.
- **Riconciliazione:** se un trade è `OPEN` su Supabase ma la posizione non c'è più su Alpaca (stop o target eseguiti mentre il bot era fermo),
  il bot recupera il prezzo di uscita dallo storico ordini e registra P&L e motivo (`TAKE_PROFIT`, `STOP_LOSS`, `TRAILING_STOP`, `MANUAL_CLOSE`).
- **Finestre di mercato:** nei primi 15 minuti dopo l'apertura niente nuovi trade né stop dinamici (la protezione OCO resta attiva);
  negli ultimi 15 minuti niente nuovi trade.
- **Pausa dopo uno stop loss:** il ticker non viene ritradato per 24 ore.

---

## 1. Setup locale e test in DRY_RUN

Prerequisiti: Python 3.11+ e un account [Alpaca](https://alpaca.markets) con chiavi **Paper Trading**.

> 💡 **Consigliato: un account paper dedicato.** Alpaca permette più account paper (dashboard → menu account → *Open new paper account*).
> Usane uno per questo bot e uno per il bot opzioni: così equity, buying power, posizioni e grafici restano separati.
> Il bot azioni gestisce solo le posizioni azionarie (`us_equity`), ma sullo stesso account le due strategie si "ruberebbero" il capitale.

```bash
python -m venv .venv
.\.venv\Scripts\Activate.ps1        # Windows (PowerShell)
pip install -r requirements.txt
copy .env.example .env              # poi compila ALPACA_API_KEY e ALPACA_SECRET_KEY
python -m pytest -q                 # test, nessuna chiamata di rete
python -m src.main                  # un ciclo completo
```

Il `.env.example` parte con `DRY_RUN=true`: pipeline completa (screening, regime, segnali, sizing, piano di chasing) senza inviare ordini.
Per provare a mercato chiuso aggiungi `IGNORE_MARKET_HOURS=true` (solo con `DRY_RUN=true`).

Esempio di output dello screener:

```text
Ticker| Prezzo  | Trend   | Vol     | VRk |   ADX |  RSI | Score | Strategia / Motivo
--------------------------------------------------------------------------------------
AAPL  |  231.40 | BULLISH | HIGH_VOL|  64 |  27.0 |   46 |  58.0 | PULLBACK_LONG / Idoneo - ritracciamento sulla EMA20 (0.40 ATR), RSI 46 <= 50
NVDA  |  190.20 | BULLISH | LOW_VOL |  22 |  31.0 |   64 |  31.2 | BREAKOUT_LONG / In attesa del segnale di ingresso - breakout già esteso (1.10 ATR oltre 180.10, max 0.75)
TLT   |   88.10 | SIDEWAYS| LOW_VOL |  12 |  14.0 |   51 |   0.0 | NO_TRADE / SIDEWAYS + LOW_VOL: nessun edge
```

---

## 2. Supabase

Puoi usare **lo stesso progetto Supabase del bot opzioni**: tutte le tabelle di questo bot hanno il prefisso `stock_`.

1. Supabase → **SQL Editor → New query** → incolla tutto [`supabase/schema.sql`](supabase/schema.sql) → **Run**.
   Crea `stock_equity_snapshots`, `stock_trades`, `stock_screening_logs`, `stock_bot_runs`, `stock_bot_status`,
   con RLS (anon = sola lettura) e Realtime. Lo script è idempotente.
2. `SUPABASE_URL` e `SUPABASE_SERVICE_ROLE_KEY` sono gli stessi del bot opzioni (Project Settings → API).

### Scheduler (avvio affidabile e gratuito)

Come nel bot opzioni: ogni 5 minuti un job `pg_cron` controlla se il bot ha dato segni di vita negli ultimi 12 minuti e, se no,
avvia il workflow tramite l'API di GitHub.

1. **Token GitHub:** puoi riusare quello del bot opzioni aggiungendo questo repository al suo *Repository access*
   (GitHub → Settings → Developer settings → Fine-grained tokens → modifica il token). Oppure creane uno nuovo
   (solo questo repository, permesso *Actions: Read and write*).
2. Controlla `c_repo` in [`supabase/scheduler.sql`](supabase/scheduler.sql) (`Davides98/alpaca_bot_stocks`), poi incolla il file nel SQL Editor → **Run**.
3. Solo se usi un token nuovo, in una **nuova** query:
   ```sql
   select vault.create_secret('IL_TUO_TOKEN', 'github_dispatch_token_stocks');
   ```
   Senza questo secret lo scheduler usa `github_dispatch_token` (quello del bot opzioni).
4. Prova: `select private.dispatch_stock_bot_if_idle();` → "avvio richiesto" e su GitHub → Actions compare un run.

I due scheduler convivono: `select jobname from cron.job;` mostra `alpaca-bot-watchdog` e `alpaca-stock-bot-watchdog`.

---

## 3. GitHub

1. Crea un repository **pubblico** `alpaca_bot_stocks` (i minuti di Actions sono gratuiti solo per i repo pubblici: la sessione dura ~6,5 h al giorno) e fai il push.
2. **Settings → Secrets and variables → Actions → New repository secret:**

   | Nome | Valore |
   |---|---|
   | `ALPACA_API_KEY` | key paper Alpaca (dell'account dedicato) |
   | `ALPACA_SECRET_KEY` | secret paper Alpaca |
   | `SUPABASE_URL` | Project URL di Supabase |
   | `SUPABASE_SERVICE_ROLE_KEY` | chiave service_role / secret |

3. **Test manuale:** Actions → *Run Stock Bot* → *Run workflow* con `dry_run` (e `ignore_market_hours` se il mercato è chiuso).
4. **Dashboard:** Settings → Pages → *Deploy from a branch* → `main` / `docs`. In [`docs/index.html`](docs/index.html) `SUPABASE_URL`
   e `SUPABASE_ANON_KEY` sono già quelli del progetto del bot opzioni (chiave *publishable*, pubblica per design); cambiali se usi un altro progetto.

I run automatici partono in **modalità sessione**: aggiornamento ogni 60 s (equity, P&L, protezione, stop dinamici),
screening e nuovi trade ogni 5 minuti, chiusura automatica dopo 340 minuti o a mercato chiuso. Il cron orario di GitHub
(minuto :23, diverso dal bot opzioni) resta come riserva.

### Parametri configurabili da GitHub

Crea una **Variable** (o un Secret) con uno di questi nomi; se non esiste vale il default.

| Nome | Default | Significato |
|---|---|---|
| `DRY_RUN` | `false` | `true` = i run automatici non inviano ordini |
| `WATCHLIST` | 20 ticker (ETF + large cap) | ticker separati da virgola |
| `MAX_RISK_PER_TRADE_PCT` | `0.01` | perdita massima allo stop per trade (max `0.05`) |
| `MAX_POSITION_PCT` | `0.20` | controvalore massimo di una posizione |
| `MAX_GROSS_EXPOSURE_PCT` | `1.0` | long + short massimi rispetto all'equity |
| `MAX_OPEN_POSITIONS` | `5` | posizioni aperte al massimo |
| `MAX_NEW_TRADES_PER_RUN` | `2` | nuovi trade al massimo per ciclo |
| `MIN_OPPORTUNITY_SCORE` | `40` | score minimo per aprire |
| `ALLOW_SHORT` | `true` | `false` = solo strategie long |
| `TAKE_PROFIT_R_TREND` | `3.0` | target dei trade di trend in multipli di R |
| `BREAKEVEN_AT_R` | `1.0` | profitto (in R) oltre cui lo stop va a breakeven (`0` = mai) |
| `TRAIL_START_R` / `TRAIL_ATR_MULT` | `1.5` / `2.5` | avvio e distanza del trailing stop (`0` = mai) |
| `MAX_HOLDING_DAYS_TREND` / `MAX_HOLDING_DAYS_MR` | `30` / `10` | time stop in giorni |
| `EXIT_ON_TREND_REVERSAL` | `true` | chiude se il regime si gira contro la posizione |
| `STOP_LOSS_COOLDOWN_HOURS` | `24` | pausa su un ticker dopo uno stop loss |
| `NO_TRADE_AFTER_OPEN_MIN` / `NO_NEW_TRADES_BEFORE_CLOSE_MIN` | `15` / `15` | finestre senza nuovi trade |
| `MAX_ENTRY_SLIPPAGE_PCT` / `MAX_EXIT_SLIPPAGE_PCT` | `0.002` / `0.005` | tetto di slippage del chasing |
| `BARS_DATA_FEED` | `sip` | `iex` se l'account non ha accesso alle barre SIP |
| `REFRESH_INTERVAL_SEC` / `TRADE_INTERVAL_MIN` | `60` / `5` | cadenze della sessione |

Tutti gli altri parametri di [`config/settings.py`](config/settings.py) sono sovrascrivibili via `.env` in locale.

---

## Sicurezza

- `paper=True` è **fisso nel codice**; non esiste un parametro per passare al live.
- Credenziali solo da variabili d'ambiente / `.env` (`SecretStr`), mascherate nei log; `diagnose=False` nei traceback.
- Supabase: il bot scrive con la service_role key (solo nei secret); la dashboard legge con la chiave anon, limitata dalla RLS.

## Limiti noti e note didattiche

- **Gap:** gli stop lato broker scattano solo in orario regolare. Un'apertura in gap oltre lo stop viene eseguita al primo prezzo disponibile, con perdita superiore a 1R.
- **Short:** perdita teorica illimitata (lo stop la limita, salvo gap). Nei conti reali ci sono costi di prestito titoli e possibili richiami.
- **Feed:** prezzi live via IEX (gratuito): per le large cap è affidabile, ma vede solo una parte degli scambi. Le barre daily usano il SIP con 16 minuti di ritardo (gratuito).
- **PDT:** sotto i 25.000 $ di equity le regole sul pattern day trading limitano i round trip in giornata (con 100.000 $ paper non è un problema).
- **Paper trading ≠ realtà:** i fill simulati di Alpaca sono più ottimistici di quelli reali.
