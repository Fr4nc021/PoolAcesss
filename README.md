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

O arquivo `.env` está no `.gitignore` e **não deve** ser versionado.

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
