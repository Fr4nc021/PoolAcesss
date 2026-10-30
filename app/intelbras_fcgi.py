"""Cadastro no facial Intelbras do mesmo jeito que o painel web (/fcgi).

Pela API (/api/user/add sem foto e depois /api/user/set com FaceUrl), os
dois faciais do clube (XPE-3200-PLUS-IP, firmwares 216.57.1.13 e
216.57.0.90) davam a todo rosto novo o FaceID do último cadastrado, mesmo
limpos e reiniciados. O painel, que dá FaceIDs diferentes, faz outro caminho
(lido pela sessão no PC do clube nas requisições do próprio painel):

1. GET /fcgi/do?action=Encrypt&UserName=..&Password=..: o facial devolve a
   senha criptografada no campo hcSingleResult;
2. cria a sessão com POST /fcgi/do?id=1 (Operation=CreateSession, com usuário
   e senha criptografada no SubmitData). A página que volta traz o número da
   sessão em hcSessionIdNow, que vira o cookie SessionId;
3. cria o usuário JUNTO com a foto num único POST multipart para
   /fcgi/do?id=16&id=5, com DestUpFile=UserDataUploadAndCommit e os dados
   cUserAdd=<UserID>/<Nome>/<PIN>/<Andar>/<Cartões>/<Relé>/<WebRelay>/<Validade>/<FaceID>/.
   FaceID vai "0": o facial escolhe.
"""

from __future__ import annotations

import random
import re
import time
from typing import Optional

import httpx

# Limite que o painel aplica à matrícula.
PANEL_REGISTRATION = re.compile(r"[0-9A-Za-z]{1,11}")


# Mesma troca que o PostEncode do painel (js/common.js do facial). O facial lê
# os textos assim, não com a codificação de URL. A crase vem primeiro, para
# não estragar os códigos `A..`K que entram depois.
_POST_ENCODE = (
    ("`", "`A"), ("&", "`B"), ("=", "`C"), (" ", "`D"), ("\r\n", "`E"), ("'", "`F"),
    ("%", "`G"), ("/", "`H"), ("$", "`I"), ("#", "`J"), ("+", "`K"),
)


def post_encode(text: str) -> str:
    for plain, code in _POST_ENCODE:
        text = text.replace(plain, code)
    return text


class PanelUnavailable(Exception):
    """Este facial não tem o /fcgi do painel."""


class PanelError(Exception):
    """O facial recusou o login ou o cadastro pelo /fcgi."""


def registration_fits(registration: str) -> bool:
    return bool(PANEL_REGISTRATION.fullmatch(str(registration or "")))


def _hidden(text: str, field: str) -> Optional[str]:
    """Valor de um <input id=field ... value=...> escondido; None se não houver.

    O facial escreve sem aspas no id e com aspas simples no valor
    (<input id=hcSingleResult type=hidden value='c14f...'>); aceita os dois jeitos.
    """
    match = re.search(
        rf"<input[^>]*\bid=[\"']?{re.escape(field)}[\"']?[^>]*\bvalue=([\"']?)([^\"'\s>]*)\1", text or ""
    )
    return match.group(2) if match else None


def _session_in_page(text: str) -> Optional[str]:
    """Número da sessão na página do CreateSession: hcSessionIdNow ou outro campo de sessão."""
    value = _hidden(text, "hcSessionIdNow")
    if value:
        return value
    for match in re.finditer(r"<input[^>]*\bid=[\"']?(\w*session\w*)[\"']?", text or "", re.I):
        value = _hidden(text, match.group(1))
        if value:
            return value
    return None


def _page_summary(text: str, http: httpx.Client) -> str:
    """O que a página do CreateSession trouxe, sem valores: ajuda a ver o que mudou no firmware."""
    title = re.search(r"<title>(.*?)</title>", text or "", re.I | re.S)
    ids = sorted(set(re.findall(r"<input[^>]*\bid=[\"']?(\w+)", text or "")))
    status = _hidden(text, "hcLoginStatus")
    return (
        f"título {title.group(1).strip() if title else '?'}; hcLoginStatus {status if status is not None else 'ausente'}; "
        f"campos {', '.join(ids[:25]) or 'nenhum'}; cookies {', '.join(sorted(http.cookies.keys())) or 'nenhum'}"
    )


def _answer(text: str, password: str) -> str:
    """Começo da resposta do facial, sem a senha, para o erro mostrar o que ele disse."""
    clean = re.sub(r"\s+", " ", text or "").strip()
    if password:
        clean = clean.replace(password, "***")
    return clean[:300]


def login(http: httpx.Client, base_url: str, user: str, password: str) -> None:
    """Abre a sessão do painel no http (cookies), como o login da tela do Intelbras."""
    response = http.get(
        f"{base_url}/fcgi/do",
        params={"action": "Encrypt", "UserName": post_encode(user), "Password": post_encode(password), "_": int(time.time() * 1000)},
    )
    if response.status_code == 404:
        raise PanelUnavailable(f"{base_url} não tem /fcgi")
    response.raise_for_status()
    encrypted = _hidden(response.text, "hcSingleResult") or ""
    if not encrypted or encrypted == "-1":
        if "hcSingleResult" not in response.text:
            raise PanelUnavailable(
                f"{base_url} não respondeu como o painel do Intelbras. Resposta: {_answer(response.text, password)}"
            )
        raise PanelError(
            "O facial recusou o usuário ou a senha (login do painel). "
            f"Resposta: {_answer(response.text, password)}"
        )
    # Como o OnLogin do painel: usuário e senha criptografada vão no SubmitData
    # (o painel guarda os dois no localStorage, não em cookie). A página que volta
    # traz o número da sessão em hcSessionIdNow, que o JavaScript grava no cookie
    # SessionId; daí em diante só esse cookie vale como login. A térmica
    # (216.57.0.90) parece abrir a sessão sem usuário e senha: tenta os dois.
    forms = (
        f"begin&Operation=CreateSession&DestURL=id`C1&UserName={user.strip()}&Password={encrypted}&SubmitData=end",
        "begin&Operation=CreateSession&DestURL=id`C1&SubmitData=end",
    )
    session_id = None
    for form in forms:
        response = http.post(f"{base_url}/fcgi/do?id=1", data={"SubmitData": form})
        response.raise_for_status()
        session_id = _session_in_page(response.text)
        if session_id or http.cookies.get("SessionId"):
            break
    if not session_id and http.cookies.get("SessionId"):
        # O facial já mandou o cookie de sessão no cabeçalho (Set-Cookie).
        return
    if not session_id:
        status = _hidden(response.text, "hcLoginStatus")
        if status == "-2":
            raise PanelError("O facial bloqueou o login do painel por tentativas demais. Espere alguns minutos.")
        raise PanelError(
            "O facial não abriu a sessão do painel (sem hcSessionIdNow). "
            f"Página: {_page_summary(response.text, http)}"
        )
    # Sem domínio, o cookie vai para este facial seja ele IP ou nome; o http é só desta sessão.
    http.cookies.set("SessionId", session_id, path="/")


def add_user_with_face(
    http: httpx.Client,
    base_url: str,
    registration: str,
    name: str,
    jpeg: bytes,
    *,
    relay: str = "1",
    validity: str = "0",
) -> str:
    """O mesmo pedido do botão Salvar do painel ao criar um usuário com foto. Devolve o que o facial respondeu."""
    if not registration_fits(registration):
        raise PanelError("Matrícula precisa ter só letras e números, até 11 caracteres.")
    # Barra separa os campos: no nome ela vai codificada pelo PostEncode, como no painel.
    fields = f"cUserAdd={post_encode(registration)}/{post_encode(name.strip()[:60])}//0//{relay}//{validity}/0/"
    response = http.post(
        f"{base_url}/fcgi/do?id=16&id=5&RefRand={random.randint(10, 100000010)}",
        data={"uploadType": f"&Operation=Upload&DestUpFile=UserDataUploadAndCommit/{fields}&"},
        files={"importUserFacePhoto": ("foto.jpg", jpeg, "image/jpeg")},
        headers={"Referer": f"{base_url}/fcgi/do?id=16&id=5", "Origin": base_url},
    )
    response.raise_for_status()
    return _answer(response.text, "")
