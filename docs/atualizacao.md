# Atualização automática

Os PCs do clube procuram versão nova no GitHub Releases de `Fr4nc021/PoolAcesss`. Só instalam um pacote assinado com a chave privada de quem publica. A instalação começa quando alguém clica em **Instalar**, na tela ou no ícone ao lado do relógio.

## Como funciona

1. Ao abrir, e depois a cada 6 horas, o programa lê a última release publicada.
2. Ele baixa o `manifest.json` e o `manifest.sig` e confere a assinatura com a chave pública de `app/update_key.py`. Depois confere se a tag é `v` + a versão assinada e se essa versão é maior que a instalada.
3. Baixa o `.zip`, confere o tamanho e o SHA-256 e extrai em `_update\staging\<versão>`. A extração recusa caminhos com `..`, caminhos absolutos, links, arquivos fora do manifest, hash diferente, pacote grande demais e qualquer coisa em `data\`, `.env`, `env.txt` ou `_update\`.
4. Aparece o aviso **Versão X pronta para instalar**. O botão só existe no navegador do próprio PC. A rota recusa pedidos de outro IP, outro Host ou outra Origin, e fica bloqueada enquanto houver cadastro, renovação, troca de foto, bloqueio ou exclusão em andamento.
5. Ao instalar, o programa confere os arquivos de novo, copia o `AcessoPiscinas-Atualizador.exe` para `_update\runner`, sincroniza e fecha.
6. O atualizador espera o programa fechar e guarda o banco e os arquivos atuais em `_update\backup`. Depois copia a versão nova e abre o programa. Se `/saude?rapido=1` não responder com a versão nova em 90 segundos, ele desfaz a troca, devolve o banco e abre a versão anterior.

O resultado aparece no rodapé da tela. O registro detalhado fica em `_update\atualizador.log`.

## Configurar a chave (uma vez)

No seu PC, fora da pasta do projeto:

```bash
python scripts/release_keygen.py C:\chaves\acessopiscinas-release.pem
```

Cole a linha `UPDATE_PUBLIC_KEY = "..."` impressa em `app/update_key.py` e faça commit. Guarde o `.pem` e a senha num lugar seguro. Sem eles, nenhuma versão nova será aceita, e a troca de chave exige reinstalar o programa nos PCs.

Os PCs instalados antes da chave existir não se atualizam sozinhos. Instale neles, à mão, a primeira versão que já tem a chave.

## Publicar uma versão

1. Mude `VERSION` em `app/version.py`, por exemplo para `1.2.0`. Faça o merge no `main`.
2. Crie e envie a tag:

   ```bash
   git tag v1.2.0
   git push origin v1.2.0
   ```

3. O workflow **Release** gera o pacote e cria a release **em rascunho**, com o `.zip` e o `manifest.json`. O workflow falha se a tag não bater com `VERSION`, se `app/update_key.py` estiver vazio ou se o pacote tiver `.env`, `env.txt`, `data\` ou `.db`.
4. Baixe os arquivos do rascunho, confira e assine:

   ```bash
   gh release download v1.2.0 --dir release-v1.2.0
   python scripts/release_sign.py --manifest release-v1.2.0/manifest.json --zip release-v1.2.0/AcessoPiscinas-1.2.0.zip --key C:\chaves\acessopiscinas-release.pem
   ```

   O script confere o hash do `.zip` e cada arquivo antes de pedir a senha. Ele recusa a assinatura se a chave não for a de `app/update_key.py`.
5. Anexe a assinatura e publique:

   ```bash
   gh release upload v1.2.0 release-v1.2.0/manifest.sig
   gh release edit v1.2.0 --draft=false
   ```

Em até 6 horas, ou ao reabrir o programa, os PCs mostram o aviso. Para não esperar, use **Procurar atualização** no rodapé.

## Primeira instalação num PC novo

`GERAR_EXE.bat` monta `dist\AcessoPiscinas_Pacote`. O pacote não leva o `.env` deste PC. Copie a pasta, renomeie `env.exemplo.txt` para `env.txt` e preencha com os dados daquele PC, inclusive o `SERVER_LAN_IP` dele.

## Testes

```bash
python scripts/test_updater.py
```

Cobre comparação de versão, assinatura, extração segura, troca com rollback e as travas da rota de instalação.
