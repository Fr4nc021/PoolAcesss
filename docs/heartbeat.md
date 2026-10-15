# Heartbeat do Supabase

Rotina diária para o projeto do Clube Aliança continuar recebendo atividade mesmo quando ninguém usa o cadastro facial. O programa do clube não participa: ele pode estar fechado.

## O que foi criado

| Peça | Onde |
|---|---|
| Tabela `public.system_heartbeat` | [`scripts/supabase_schema.sql`](../scripts/supabase_schema.sql) |
| Edge Function `heartbeat` | [`supabase/functions/heartbeat/index.ts`](../supabase/functions/heartbeat/index.ts) |
| Cron | [`.github/workflows/heartbeat.yml`](../.github/workflows/heartbeat.yml) |

A tabela tem uma linha fixa:

| Coluna | Valor |
|---|---|
| `id` | `1` |
| `last_ping` | horário da última chamada, em `timestamptz` |

RLS fica ligado. `anon` e `authenticated` não acessam a tabela. Só a `service_role`, usada pela função dentro do Supabase, pode ler e atualizar.

## Endpoint

```text
POST https://lkucvzrvnmekzizxioqb.supabase.co/functions/v1/heartbeat
Header: x-heartbeat-secret: <HEARTBEAT_SECRET>
```

Resposta esperada:

```json
{"ok":true,"id":1,"last_ping":"2026-10-01T15:00:00.000Z"}
```

`verify_jwt` está desligado nesta função. Quem chama não envia a chave anon nem a `service_role`. O header acima é a única autorização.

## Secrets

Nada disso entra no `.env` do computador do clube.

| Nome | Onde configurar | Para quê |
|---|---|---|
| `HEARTBEAT_SECRET` | Supabase → Edge Functions → Secrets, e também nos secrets do GitHub | Senha que o cron envia no header |
| `HEARTBEAT_URL` | Secrets do GitHub | URL completa do `POST` acima |
| `SUPABASE_URL` | Já injetada na Edge Function | Endereço do projeto |
| `SUPABASE_SERVICE_ROLE_KEY` | Já injetada na Edge Function | A função grava `last_ping`. Não copie esta chave para o cron nem para o programa |

Gere o segredo no PowerShell:

```powershell
[Convert]::ToBase64String((1..32 | ForEach-Object { Get-Random -Maximum 256 }))
```

Use o mesmo valor no Supabase e no GitHub.

## Publicar uma vez

No computador de quem administra o Supabase (não no PC da piscina):

1. No SQL Editor, rode o bloco `system_heartbeat` de [`scripts/supabase_schema.sql`](../scripts/supabase_schema.sql). Quem ainda não criou o banco pode rodar o arquivo inteiro.
2. Na pasta do projeto, com a [Supabase CLI](https://supabase.com/docs/guides/cli) autenticada:

```bash
supabase link --project-ref <ref>
supabase secrets set HEARTBEAT_SECRET=<o segredo gerado>
supabase functions deploy heartbeat --no-verify-jwt
```

O `<ref>` é o subdomínio do projeto, o trecho antes de `.supabase.co`.

## Cron

O workflow [`.github/workflows/heartbeat.yml`](../.github/workflows/heartbeat.yml) dispara todo dia às 11:17 UTC (08:17 em Brasília) e também aceita execução manual (`workflow_dispatch`).

No repositório GitHub, em Settings → Secrets and variables → Actions, crie:

- `HEARTBEAT_URL` = `https://lkucvzrvnmekzizxioqb.supabase.co/functions/v1/heartbeat`
- `HEARTBEAT_SECRET` = o mesmo segredo da função

Esses dois secrets já estão no repositório `Fr4nc021/PoolAcesss`. O cron passa a valer quando o workflow for enviado para o GitHub.

Outro agendador (cron-job.org, EasyCron, etc.) serve do mesmo jeito: um `POST` diário nessa URL, com o header `x-heartbeat-secret`.

## Teste manual

Troque a URL e o segredo. Não use a `service_role`.

```powershell
curl.exe -sS -X POST `
  -H "x-heartbeat-secret: SEU_SEGREDO" `
  "https://lkucvzrvnmekzizxioqb.supabase.co/functions/v1/heartbeat"
```

Confira no SQL Editor:

```sql
select id, last_ping from public.system_heartbeat;
```

`last_ping` deve ser o horário do `POST`. Chame de novo e o valor tem que mudar.

Uma chamada sem o header, ou com o segredo errado, responde `401` e não altera a linha.
