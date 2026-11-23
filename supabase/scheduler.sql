-- =====================================================================
-- Scheduler affidabile e gratuito: Supabase (pg_cron + pg_net) avvia il bot azionario su GitHub Actions.
-- Stesso meccanismo del bot opzioni, con nomi distinti: i due scheduler convivono nello stesso progetto.
--
-- OGNI MINUTO (lun-ven, 13:00-20:59 UTC) controlla se il bot ha dato segni di vita negli ultimi
-- 5 minuti (stock_equity_snapshots, stock_bot_runs o stock_bot_status). Se no, avvia il workflow via API GitHub.
-- Durante la sessione il bot scrive ogni minuto, quindi lo scheduler interviene solo se il bot si è fermato.
--
-- PREREQUISITI
--   1. Aver eseguito supabase/schema.sql.
--   2. Un token GitHub "fine-grained" con accesso a QUESTO repository, permesso Actions: Read and write.
--      Puoi anche riusare il token del bot opzioni, aggiungendo questo repository al suo "Repository access".
--
-- COME SI USA
--   0. Dashboard Supabase -> Database -> Extensions: "pg_cron" e "pg_net" attivi (già fatto se usi lo
--      scheduler del bot opzioni nello stesso progetto).
--   1. Controlla c_repo qui sotto (<proprietario>/<repository>), poi incolla tutto il file nel SQL Editor e premi Run.
--   2. Token nel Vault cifrato, in una NUOVA query (non in questo file!):
--        select vault.create_secret('IL_TUO_TOKEN', 'github_dispatch_token_stocks');
--      Se non lo crei, lo scheduler usa 'github_dispatch_token' (quello del bot opzioni), se esiste
--      e se ha accesso anche a questo repository.
--   NON scrivere mai il token in un file del repository: il repo è pubblico.
-- =====================================================================

do $$
begin
    if not exists (select 1 from pg_extension where extname = 'pg_cron') then
        raise exception 'pg_cron non attivo: Dashboard -> Database -> Extensions -> attiva "pg_cron", poi riesegui questo file';
    end if;
    if not exists (select 1 from pg_extension where extname = 'pg_net') then
        raise exception 'pg_net non attivo: Dashboard -> Database -> Extensions -> attiva "pg_net", poi riesegui questo file';
    end if;
end $$;

create schema if not exists private;
revoke all on schema private from public, anon, authenticated;

create or replace function private.dispatch_stock_bot_if_idle()
returns text
language plpgsql
security definer
set search_path = public, extensions, pg_temp
as $$
declare
    c_repo        constant text := 'Davides98/alpaca_bot_stocks';   -- <proprietario>/<repository>
    c_workflow    constant text := 'run_bot.yml';
    c_branch      constant text := 'main';
    -- Il bot scrive un segnale ogni minuto; 5 minuti lasciano il tempo a un job GitHub appena avviato
    -- (setup + test ~1-2 min) di dare il primo segno di vita senza richieste di avvio doppie.
    c_idle        constant interval := interval '5 minutes';
    v_last        timestamptz;
    v_token       text;
    v_request_id  bigint;
begin
    select greatest(
        coalesce((select max("timestamp") from public.stock_equity_snapshots), 'epoch'::timestamptz),
        coalesce((select max("timestamp") from public.stock_bot_runs), 'epoch'::timestamptz),
        coalesce((select max(updated_at) from public.stock_bot_status), 'epoch'::timestamptz)
    ) into v_last;

    if v_last > now() - c_idle then
        return format('stock bot attivo (ultimo segnale %s): nessuna azione', v_last);
    end if;

    select coalesce(
        (select decrypted_secret from vault.decrypted_secrets where name = 'github_dispatch_token_stocks'),
        (select decrypted_secret from vault.decrypted_secrets where name = 'github_dispatch_token')
    ) into v_token;
    if v_token is null then
        raise warning 'Token GitHub mancante nel Vault (github_dispatch_token_stocks)';
        return 'token mancante';
    end if;

    select net.http_post(
        url     := format('https://api.github.com/repos/%s/actions/workflows/%s/dispatches', c_repo, c_workflow),
        body    := jsonb_build_object('ref', c_branch, 'inputs', jsonb_build_object('scheduler', 'true')),
        headers := jsonb_build_object(
            'Authorization', 'Bearer ' || v_token,
            'Accept', 'application/vnd.github+json',
            'X-GitHub-Api-Version', '2022-11-28',
            'User-Agent', 'alpaca-stock-bot-supabase-scheduler',
            'Content-Type', 'application/json'
        ),
        timeout_milliseconds := 10000
    ) into v_request_id;

    return format('stock bot inattivo da %s: avvio richiesto (pg_net request %s)', v_last, v_request_id);
end;
$$;

revoke all on function private.dispatch_stock_bot_if_idle() from public, anon, authenticated;

-- Job pg_cron: OGNI MINUTO, lun-ven, 13:00-20:59 UTC (festivi e orari esatti li gestisce il bot)
select cron.unschedule(jobid) from cron.job where jobname = 'alpaca-stock-bot-watchdog';
select cron.schedule('alpaca-stock-bot-watchdog', '* 13-20 * * 1-5', $$select private.dispatch_stock_bot_if_idle()$$);

-- ---------------------------------------------------------------------
-- VERIFICHE (eseguile quando vuoi, una alla volta)
-- ---------------------------------------------------------------------
-- Job registrati (vedrai anche quello del bot opzioni):
--   select jobid, jobname, schedule, active from cron.job;
-- Ultime esecuzioni:
--   select start_time, status, return_message from cron.job_run_details order by start_time desc limit 10;
-- Risposte di GitHub (204 = workflow avviato; 401/403/404 = token o nome del repo):
--   select created, status_code, left(content::text, 200) from net._http_response order by created desc limit 10;
-- Prova manuale immediata:
--   select private.dispatch_stock_bot_if_idle();
-- Disattivare lo scheduler:
--   select cron.unschedule('alpaca-stock-bot-watchdog');
