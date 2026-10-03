-- Gold Alert : schéma Supabase (à exécuter dans SQL Editor)

create table if not exists public.settings (
  id int primary key default 1 check (id = 1),
  bot_enabled boolean not null default true,
  threshold int not null default 7 check (threshold between 1 and 10),
  pre_alert_minutes int[] not null default '{60,15}',
  stop_before_min int not null default 15,
  stop_after_min int not null default 30,
  vol_threshold_usd numeric not null default 15,
  vol_window_min int not null default 5,
  src_calendar boolean not null default true,
  src_news boolean not null default true,
  src_volatility boolean not null default true,
  updated_at timestamptz not null default now()
);
insert into public.settings (id) values (1) on conflict do nothing;

create table if not exists public.alerts (
  id bigserial primary key,
  created_at timestamptz not null default now(),
  kind text not null,
  level int not null default 0,
  title text not null,
  body text,
  source text,
  dedupe_key text unique
);
create index if not exists alerts_created_idx on public.alerts (created_at desc);

create table if not exists public.bot_status (
  id int primary key default 1 check (id = 1),
  last_cycle timestamptz,
  gemini_calls int not null default 0,
  gemini_day date,
  info jsonb not null default '{}'
);
insert into public.bot_status (id) values (1) on conflict do nothing;

-- Sécurité : seuls les comptes connectés (3 utilisateurs) lisent ; le bot utilise la service key (bypass RLS)
alter table public.settings enable row level security;
alter table public.alerts enable row level security;
alter table public.bot_status enable row level security;

drop policy if exists "auth read settings" on public.settings;
drop policy if exists "auth update settings" on public.settings;
drop policy if exists "auth read alerts" on public.alerts;
drop policy if exists "auth read status" on public.bot_status;

create policy "auth read settings" on public.settings for select to authenticated using (true);
create policy "auth update settings" on public.settings for update to authenticated using (true) with check (true);
create policy "auth read alerts" on public.alerts for select to authenticated using (true);
create policy "auth read status" on public.bot_status for select to authenticated using (true);

-- Temps réel
alter publication supabase_realtime add table public.alerts;
alter publication supabase_realtime add table public.bot_status;
