-- Rode este script uma vez no SQL Editor do Supabase.
-- Os dois PCs usam a chave service_role (SUPABASE_KEY). Ela ignora estas políticas.
-- A chave anon não lê estes dados.

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

-- O programa cria o bucket privado "fotos" sozinho na primeira sincronização.
