-- =====================================================================
-- Alpaca Stock Bot - Schema Supabase (PostgreSQL)
-- Incolla l'intero file nel SQL Editor di Supabase ed esegui ("Run").
-- Lo script è idempotente: può essere rieseguito senza errori.
--
-- Tutte le tabelle hanno il prefisso stock_: puoi usare lo STESSO progetto
-- Supabase del bot opzioni (le sue tabelle non vengono toccate).
--
-- Sicurezza:
--   * RLS attiva su tutte le tabelle.
--   * Ruolo `anon` (chiave pubblica della dashboard): SOLO lettura.
--   * Nessuna policy INSERT/UPDATE/DELETE per `anon`: scrive solo il bot
--     con la SERVICE ROLE key (che bypassa la RLS).
-- =====================================================================

-- ---------------------------------------------------------------------
-- 1) stock_equity_snapshots: una riga per ogni aggiornamento del bot
-- ---------------------------------------------------------------------
create table if not exists public.stock_equity_snapshots (
    id                  bigserial primary key,
    "timestamp"         timestamptz not null default now(),
    equity              numeric(14, 2) not null,
    cash                numeric(14, 2) not null,
    initial_capital     numeric(14, 2) not null,
    total_return_usd    numeric(14, 2) not null,
    total_return_pct    numeric(10, 4) not null,
    daily_pnl           numeric(14, 2) not null default 0,
    long_market_value   numeric(14, 2) not null default 0,
    short_market_value  numeric(14, 2) not null default 0,   -- negativo per le posizioni short (convenzione Alpaca)
    positions_count     integer not null default 0
);

create index if not exists idx_stock_equity_ts on public.stock_equity_snapshots ("timestamp" desc);

-- ---------------------------------------------------------------------
-- 2) stock_trades: un record per posizione aperta dal bot
--    entry_price / exit_price = prezzo medio di esecuzione per azione.
--    pnl = (exit - entry) x qty per i LONG, (entry - exit) x qty per gli SHORT.
--    risk_per_share = |entry - initial_stop| = 1R; r_multiple = profitto attuale in R.
-- ---------------------------------------------------------------------
create table if not exists public.stock_trades (
    id                   bigserial primary key,
    created_at           timestamptz not null default now(),
    updated_at           timestamptz not null default now(),
    closed_at            timestamptz,
    ticker               text not null,
    strategy             text not null,
    side                 text not null default 'LONG' check (side in ('LONG', 'SHORT')),
    status               text not null default 'OPEN' check (status in ('OPEN', 'CLOSED')),
    entry_price          numeric(14, 4) not null,
    exit_price           numeric(14, 4),
    qty                  integer not null,
    initial_stop         numeric(14, 4),
    stop_price           numeric(14, 4),
    take_profit          numeric(14, 4),
    risk_per_share       numeric(14, 4),
    current_price        numeric(14, 4),
    unrealized_pnl       numeric(14, 2),
    r_multiple           numeric(10, 3),
    pnl                  numeric(14, 2),
    pnl_pct              numeric(10, 4),
    close_reason         text,          -- TAKE_PROFIT | STOP_LOSS | TRAILING_STOP | TIME_STOP | TREND_REVERSAL | MANUAL_CLOSE | NOT_FOUND_IN_BROKER
    details_json         jsonb not null default '{}'::jsonb,
    alpaca_order_id      text,
    protection_order_id  text
);

create index if not exists idx_stock_trades_status on public.stock_trades (status);
create index if not exists idx_stock_trades_created_at on public.stock_trades (created_at desc);
create index if not exists idx_stock_trades_ticker on public.stock_trades (ticker);

-- ---------------------------------------------------------------------
-- 3) stock_screening_logs: una riga per ticker per ogni ciclo di trading
-- ---------------------------------------------------------------------
create table if not exists public.stock_screening_logs (
    id                 bigserial primary key,
    "timestamp"        timestamptz not null default now(),
    run_id             text,
    ticker             text not null,
    price              numeric(14, 4),
    trend              text,
    volatility_regime  text,
    vol_rank           numeric(8, 2),
    adx                numeric(8, 2),
    rsi                numeric(8, 2),
    atr_pct            numeric(10, 5),
    opportunity_score  numeric(8, 2),
    selected           boolean not null default false,
    strategy           text,
    signal             text,
    reason             text
);

create index if not exists idx_stock_screening_ts on public.stock_screening_logs ("timestamp" desc);
create index if not exists idx_stock_screening_run on public.stock_screening_logs (run_id);

-- ---------------------------------------------------------------------
-- 4) stock_bot_runs: "battito cardiaco" del bot, una riga per OGNI run
-- ---------------------------------------------------------------------
create table if not exists public.stock_bot_runs (
    id           bigserial primary key,
    "timestamp"  timestamptz not null default now(),
    run_id       text not null,
    status       text not null,          -- OK | MARKET_CLOSED | CONFIG_ERROR | AUTH_ERROR | ERROR
    message      text,
    dry_run      boolean not null default true,
    trigger      text                    -- supabase-scheduler | github-cron | manual | local
);

create index if not exists idx_stock_bot_runs_ts on public.stock_bot_runs ("timestamp" desc);

-- ---------------------------------------------------------------------
-- 5) stock_bot_status: UNA sola riga (id = 1) con lo stato attuale del bot
-- ---------------------------------------------------------------------
create table if not exists public.stock_bot_status (
    id                    smallint primary key default 1 check (id = 1),
    updated_at            timestamptz not null default now(),
    state                 text,          -- WAITING_OPEN | RUNNING | STOPPED
    message               text,
    run_id                text,
    trigger               text,
    dry_run               boolean,
    session_mode          boolean,
    refresh_interval_sec  integer,
    trade_interval_min    integer,
    session_max_minutes   integer,
    session_started_at    timestamptz,
    session_deadline      timestamptz,
    last_trade_cycle_at   timestamptz,
    next_trade_cycle_at   timestamptz,
    trading_cycles        integer,
    market_is_open        boolean,
    market_next_open      timestamptz,
    market_next_close     timestamptz,
    clock_at              timestamptz
);

-- ---------------------------------------------------------------------
-- Row Level Security + privilegi
-- ---------------------------------------------------------------------
do $$
declare
    t text;
begin
    foreach t in array array['stock_equity_snapshots', 'stock_trades', 'stock_screening_logs', 'stock_bot_runs', 'stock_bot_status'] loop
        execute format('alter table public.%I enable row level security', t);
        execute format('drop policy if exists "Allow public read" on public.%I', t);
        execute format('create policy "Allow public read" on public.%I for select to anon using (true)', t);
        execute format('grant select on public.%I to anon', t);
        execute format('revoke insert, update, delete, truncate on public.%I from anon', t);
        execute format('grant all on public.%I to service_role', t);
    end loop;
end $$;

grant usage on schema public to anon, service_role;
grant usage, select on all sequences in schema public to service_role;

-- ---------------------------------------------------------------------
-- Realtime: la dashboard si aggiorna appena il bot scrive.
-- La RLS si applica anche al realtime: anon riceve solo ciò che può leggere.
-- ---------------------------------------------------------------------
do $$
declare
    t text;
begin
    if exists (select 1 from pg_publication where pubname = 'supabase_realtime') then
        foreach t in array array['stock_equity_snapshots', 'stock_trades', 'stock_screening_logs', 'stock_bot_runs', 'stock_bot_status'] loop
            if not exists (
                select 1 from pg_publication_tables
                where pubname = 'supabase_realtime' and schemaname = 'public' and tablename = t
            ) then
                execute format('alter publication supabase_realtime add table public.%I', t);
            end if;
        end loop;
    end if;
end $$;
