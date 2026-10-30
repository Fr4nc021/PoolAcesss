"""Cadastro no facial Intelbras do mesmo jeito que o painel web (/fcgi).

Pela API (/api/user/add sem foto e depois /api/user/set com FaceUrl), os
dois faciais do clube (XPE-3200-PLUS-IP, firmwares 216.57.1.13 e
216.57.0.90) davam a todo rosto novo o FaceID do último cadastrado, mesmo
limpos e reiniciados. O painel, que dá FaceIDs diferentes, faz outro caminho
(lido pela sessão no PC do clube nas requisições do próprio painel):

1. GET /fcgi/do?action=Encrypt&UserName=..&Password=..: o facial devolve a
   senha criptografada no campo hcSingleResult;
2. grava UserName e a senha criptografada como cookies e cria a sessão com
   POST /fcgi/do?id=1 (Operation=CreateSession);
3. cria o usuário JUNTO com a foto num único POST multipart para
   /fcgi/do?id=16&id=5, com DestUpFile=UserDataUploadAndCommit e os dados
   cUserAdd=<UserID>/<Nome>/<PIN>/<Andar>/<Cartões>/<Relé>/<WebRelay>/<Validade>/<FaceID>/.
   FaceID vai "0": o facial escolhe.
"""

from __future__ import annotations

import random
import re
import time
import httpx

# Limite que o painel aplica à matrícula.
PANEL_REGISTRATION = re.compile(r"[0-9A-Za-z]{1,11}")
# O facial escreve sem aspas no id e com aspas simples no valor:
# <input id=hcSingleResult type=hidden value='c14f...'>. Aceita os dois jeitos.
_Q = "[\"']?"
_SINGLE_RESULT = (
    re.compile(rf"id={_Q}hcSingleResult{_Q}[^>]*value={_Q}([^\"'\s>]*)"),
    re.compile(rf"value={_Q}([^\"'\s>]*){_Q}[^>]*id={_Q}hcSingleResult\b"),
)
_LOGIN_FAILED = re.compile(rf"id={_Q}hcLoginStatus{_Q}[^>]*value={_Q}-1\b|value={_Q}-1{_Q}[^>]*id={_Q}hcLoginStatus\b")


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


def _encrypted_password(text: str) -> str:
    for pattern in _SINGLE_RESULT:
        match = pattern.search(text)
        if match:
            return match.group(1)
    return ""


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
    encrypted = _encrypted_password(response.text)
    if not encrypted or encrypted == "-1":
        if "hcSingleResult" not in response.text:
            raise PanelUnavailable(
                f"{base_url} não respondeu como o painel do Intelbras. Resposta: {_answer(response.text, password)}"
            )
        raise PanelError(
            "O facial recusou o usuário ou a senha (login do painel). "
            f"Resposta: {_answer(response.text, password)}"
        )
    # Sem domínio, o cookie vai para este facial seja ele IP ou nome; o http é só desta sessão.
    http.cookies.set("UserName", post_encode(user))
    http.cookies.set("Password", encrypted)
    response = http.post(
        f"{base_url}/fcgi/do?id=1",
        data={"SubmitData": "begin&Operation=CreateSession&DestURL=id=1&SubmitData=end"},
    )
    response.raise_for_status()
    if _LOGIN_FAILED.search(response.text):
        raise PanelError(
            f"O facial recusou a sessão do painel (CreateSession). Resposta: {_answer(response.text, password)}"
        )


def add_user_with_face(
    http: httpx.Client,
    base_url: str,
    registration: str,
    name: str,
    jpeg: bytes,
    *,
    relay: str = "1",
    validity: str = "0",
) -> None:
    """O mesmo pedido do botão Salvar do painel ao criar um usuário com foto."""
    if not registration_fits(registration):
        raise PanelError("Matrícula precisa ter só letras e números, até 11 caracteres.")
    # Barra separa os campos: no nome ela vai codificada pelo PostEncode, como no painel.
    fields = f"cUserAdd={post_encode(registration)}/{post_encode(name.strip()[:60])}//0//{relay}//{validity}/0/"
    response = http.post(
        f"{base_url}/fcgi/do?id=16&id=5&RefRand={random.randint(10, 100000010)}",
        data={"uploadType": f"&Operation=Upload&DestUpFile=UserDataUploadAndCommit/{fields}&"},
        files={"importUserFacePhoto": ("foto.jpg", jpeg, "image/jpeg")},
    )
    response.raise_for_status()
