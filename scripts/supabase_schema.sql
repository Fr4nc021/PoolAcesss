-- Rode este script uma vez no SQL Editor do Supabase.
-- Os dois PCs usam a chave service_role (SUPABASE_KEY). Ela ignora o RLS.
-- A chave anon não lê estes dados.
-- O que sobe: sócios, eventos de acesso, exclusões e as fotos no bucket "fotos".
-- O que fica só no PC: id local, caminho da foto e a pendência do facial.

create table if not exists public.members (
    registration text primary key,
    name text not null,
    device_user_id integer,
    photo_sha256 text,
    photo_filename text,
    medical_exam_expires_at text not null,
    access_blocked boolean not null default false,
    created_at text not null,
    updated_at text not null,
    pool text
);

alter table public.members add column if not exists pool text;

create table if not exists public.access_events (
    sync_id text primary key,
    event_at text not null,
    member_name text,
    registration text,
    result text not null,
    source text not null default 'local',
    detail text,
    external_id text,
    created_at text not null
);

alter table public.members enable row level security;
alter table public.access_events enable row level security;

create table if not exists public.member_deletions (
    registration text primary key,
    deleted_at text not null
);

alter table public.member_deletions enable row level security;

-- A chave anon e a authenticated não acessam estas tabelas.
-- A service_role é a SUPABASE_KEY dos PCs e precisa destes direitos
-- mesmo com "Automatically expose new tables" desligado.
revoke all on table public.members from anon, authenticated;
revoke all on table public.access_events from anon, authenticated;
revoke all on table public.member_deletions from anon, authenticated;

grant select, insert, update, delete on table public.members to service_role;
grant select, insert, update, delete on table public.access_events to service_role;
grant select, insert, update, delete on table public.member_deletions to service_role;

-- Fotos não vão para uma tabela. O arquivo fica neste bucket privado,
-- com o nome do hash (photo_sha256.jpg). A matrícula só guarda o hash.
insert into storage.buckets (id, name, public)
values ('fotos', 'fotos', false)
on conflict (id) do update set public = excluded.public;

-- Heartbeat do projeto. Uma linha só, atualizada por um cron externo.
-- Não entra no backup dos sócios e o programa do clube não lê esta tabela.
-- Quem já rodou o schema antes cola apenas este bloco no SQL Editor.
create table if not exists public.system_heartbeat (
    id bigint primary key,
    last_ping timestamptz not null default now()
);

alter table public.system_heartbeat enable row level security;

revoke all on table public.system_heartbeat from anon, authenticated;

grant select, insert, update on table public.system_heartbeat to service_role;

insert into public.system_heartbeat (id, last_ping)
values (1, now())
on conflict (id) do nothing;
