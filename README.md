# PoolAcesss (AcessoPiscinas)

Sistema web para **cadastro facial** em equipamentos **Intelbras XPE** (ex.: XPE 3200 PLUS IP), com validade ligada ao **exame médico**.

Quando o exame vence, o sistema remove o usuário do equipamento automaticamente.

> **Privacidade:** este repositório não deve conter IPs reais, senhas, dumps de usuários nem fotos. Configure tudo via `.env` (veja [Configuração](#configuração)).

---

## Funcionalidades

- Cadastro de membro com nome + foto → envia face ao equipamento
- Controle de validade (padrão: **30 dias**)
- Renovação ao confirmar exame médico (`+VALIDITY_DAYS`)
- Remoção automática no equipamento quando o exame expira
- Endpoint de saúde para diagnóstico

---

## Requisitos

- Python **3.10+**
- PC na **mesma rede** do facial Intelbras
- Porta do app liberada no firewall (padrão **5000**), se for acessar de outros PCs

---

## Configuração

1. Copie o modelo de ambiente:

```bash
copy .env.example .env
```

2. Edite o `.env` com os dados do **seu** ambiente:

| Variável | Descrição |
|----------|-----------|
| `DEVICE_URL` | URL HTTP do facial (ex.: `http://192.168.x.x`) |
| `DEVICE_LOGIN` / `DEVICE_PASSWORD` | Credenciais do aparelho |
| `SERVER_LAN_IP` | IP deste PC na LAN (para o equipamento baixar fotos) |
| `SECRET_KEY` | Chave Flask — use um valor único em produção |
| `VALIDITY_DAYS` | Dias de validade após o exame (padrão 30) |
| `DOOR_NUM` | Canal OpenDoor no equipamento |
| `SYNC_PEER_URL` | Endereço do outro PC, ex.: `http://192.168.x.x:5000` |
| `SYNC_TOKEN` | Segredo igual nos dois PCs para a troca na rede local |
| `SYNC_INTERVAL_SECONDS` | Intervalo da troca (padrão 20) |
| `SUPABASE_URL` / `SUPABASE_KEY` | Backup na nuvem. A chave é a `service_role` |

O arquivo `.env` está no `.gitignore` e **não deve** ser versionado.

### Dois PCs e backup

Cada PC roda o programa e guarda o próprio banco. Na rede do clube, um puxa o outro a cada 20 segundos, sem internet. Com internet, as mesmas matrículas, exames e fotos sobem para o Supabase e o outro PC baixa de lá.

1. No SQL Editor do Supabase, rode [`scripts/supabase_schema.sql`](scripts/supabase_schema.sql).
2. Nos dois `.env`, use a mesma `SUPABASE_URL`, a mesma `SUPABASE_KEY` (`service_role`) e o mesmo `SYNC_TOKEN`.
3. `SYNC_PEER_URL` em cada PC é o endereço do outro (`http://IP:5000`).
4. Libere a porta 5000 no firewall do Windows entre os dois PCs.

O topo da tela mostra **Outro PC** e **Nuvem**. Sem `SYNC_PEER_URL` ou sem `SUPABASE_URL`, aquele caminho fica desligado e o restante do programa segue igual.

O projeto na nuvem também precisa de um ping diário para não pausar por inatividade. Isso fica fora do programa do clube. Veja [docs/heartbeat.md](docs/heartbeat.md).

O programa instalado se atualiza pelo GitHub Releases, com pacote assinado e volta automática se a versão nova não abrir. Como publicar uma versão: [docs/atualizacao.md](docs/atualizacao.md).

---

## Instalação (Windows)

Na pasta do projeto:

1. Duplo clique em **`INSTALAR.bat`** (cria venv e instala dependências)
2. Confira o `.env`
3. Duplo clique em **`INICIAR.bat`**
4. Abra no Chrome: `http://127.0.0.1:5000`  
   De outro PC na rede: `http://IP-DO-SERVIDOR:5000`

### Manual (alternativa)

```bash
python -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
copy .env.example .env
.venv\Scripts\python.exe -m app.main
```

---

## Uso

1. **Cadastrar** — nome + foto → libera face no XPE pelo período configurado  
2. **Membros** — status do exame; **OK exame médico** renova a validade  
3. Exame vencido → o sistema remove o usuário do equipamento  

Diagnóstico (com o app rodando): `http://127.0.0.1:5000/saude`

---

## Pacote executável

Para gerar o pacote no PC de desenvolvimento:

```text
GERAR_EXE.bat
```

A saída fica em `dist/` (pasta **ignorada pelo Git**). Copie o pacote gerado para o PC da piscina e execute o `.exe` conforme o `LEIA-ME` do pacote.

---

## Estrutura do projeto

```text
AcessoPiscinas/
├── app/                 # Aplicação Flask
│   ├── config.py        # Lê variáveis do .env
│   ├── intelbras_client.py
│   ├── routes.py
│   └── ...
├── templates/           # HTML
├── static/              # CSS / assets
├── data/                # DB e fotos (local; não versionado)
├── scripts/             # Utilitários / diagnóstico
├── supabase/functions/  # Heartbeat (Edge Function)
├── docs/heartbeat.md    # Cron e secrets do heartbeat
├── docs/atualizacao.md  # Publicar versão e atualização automática
├── .env.example         # Modelo de configuração (sem segredos)
├── INSTALAR.bat
├── INICIAR.bat
├── GERAR_EXE.bat
├── requirements.txt
└── README.md
```

---

## Segurança (antes de publicar)

- Não commite `.env`, dumps (`scripts/_*`), bancos, fotos ou pastas `dist/` / `build/`
- Não deixe senhas hardcoded em scripts — use variáveis de ambiente
- Troque `SECRET_KEY` e a senha do equipamento se já foram expostas em algum lugar
- Prefira repositório **privado** se o código for específico de um clube/instalação

---

## Licença

Uso interno / projeto privado, salvo indicação em contrário.
