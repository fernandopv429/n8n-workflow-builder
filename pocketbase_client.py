"""Imagem de cada cliente no PocketBase compartilhado da A5
(`db.a5ecossistema.tech`) — o mesmo hub que já guarda as imagens do
cadastro-veiculos e as mídias de lr_autostore/premium_film/fabifisio.

Segue o padrão já usado no cadastro-veiculos: coleção própria do projeto
(`painel_agentes_imagens`), UM registro por imagem, leitura pública e escrita
só com superusuário. O vínculo com o cliente mora no NOSSO Postgres
(`clientes.imagem_pb_record_id`/`imagem_pb_filename`), não no PocketBase.

Sem dependência extra: só urllib da biblioteca padrão. O upload monta o
multipart na mão porque é o único formato que o PocketBase aceita pra arquivo.
"""
import json
import mimetypes
import pathlib
import sys
import urllib.error
import urllib.request
import uuid

RAIZ = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(RAIZ))

from config import carregar_env  # noqa: E402

COLECAO = "painel_agentes_imagens"
TAMANHO_MAXIMO = 5 * 1024 * 1024  # mesmo teto declarado na coleção

_token = None


class PocketBaseError(RuntimeError):
    pass


def _base_url() -> str:
    return carregar_env().get("POCKETBASE_URL", "").rstrip("/")


def configurado() -> bool:
    env = carregar_env()
    return bool(
        env.get("POCKETBASE_URL") and env.get("POCKETBASE_ADMIN_EMAIL")
        and env.get("POCKETBASE_ADMIN_PASSWORD")
    )


def _autenticar() -> str:
    """Token de superusuário. Guardado em memória — o PocketBase devolve um
    JWT de validade longa e reautenticar a cada upload seria desperdício."""
    global _token
    if _token:
        return _token
    env = carregar_env()
    corpo = json.dumps({
        "identity": env["POCKETBASE_ADMIN_EMAIL"],
        "password": env["POCKETBASE_ADMIN_PASSWORD"],
    }).encode("utf-8")
    req = urllib.request.Request(
        f"{_base_url()}/api/collections/_superusers/auth-with-password",
        data=corpo, method="POST",
    )
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            _token = json.loads(resp.read())["token"]
    except urllib.error.HTTPError as e:
        raise PocketBaseError(f"login no PocketBase falhou ({e.code}): {e.read().decode('utf-8','replace')[:200]}") from None
    return _token


def _multipart(campos: dict, arquivo_nome: str, arquivo_bytes: bytes) -> tuple:
    """Monta o corpo multipart/form-data. Devolve (content_type, corpo)."""
    limite = f"----painel{uuid.uuid4().hex}"
    tipo = mimetypes.guess_type(arquivo_nome)[0] or "application/octet-stream"
    partes = []
    for chave, valor in campos.items():
        partes.append(
            f"--{limite}\r\nContent-Disposition: form-data; name=\"{chave}\"\r\n\r\n{valor}\r\n".encode("utf-8")
        )
    partes.append(
        f"--{limite}\r\nContent-Disposition: form-data; name=\"imagem\"; filename=\"{arquivo_nome}\"\r\n"
        f"Content-Type: {tipo}\r\n\r\n".encode("utf-8")
    )
    partes.append(arquivo_bytes)
    partes.append(f"\r\n--{limite}--\r\n".encode("utf-8"))
    return f"multipart/form-data; boundary={limite}", b"".join(partes)


def enviar_imagem(cliente_id: int, nome_arquivo: str, conteudo: bytes) -> dict:
    """Sobe a imagem e devolve {record_id, filename, url}."""
    if len(conteudo) > TAMANHO_MAXIMO:
        raise PocketBaseError(
            f"imagem tem {len(conteudo) // 1024} KB — o limite é {TAMANHO_MAXIMO // 1024} KB"
        )

    tipo, corpo = _multipart({"cliente_ref": str(cliente_id)}, nome_arquivo, conteudo)
    req = urllib.request.Request(
        f"{_base_url()}/api/collections/{COLECAO}/records", data=corpo, method="POST"
    )
    req.add_header("Authorization", _autenticar())
    req.add_header("Content-Type", tipo)
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            registro = json.loads(resp.read())
    except urllib.error.HTTPError as e:
        raise PocketBaseError(f"upload falhou ({e.code}): {e.read().decode('utf-8','replace')[:300]}") from None

    return {
        "record_id": registro["id"],
        "filename": registro["imagem"],
        "url": url_publica(registro["id"], registro["imagem"]),
    }


def url_publica(record_id: str, filename: str) -> str:
    """A coleção tem listRule/viewRule públicos, então o arquivo é servido sem
    token — dá pra usar direto no <img> do painel."""
    if not record_id or not filename:
        return ""
    return f"{_base_url()}/api/files/{COLECAO}/{record_id}/{filename}"


def remover_imagem(record_id: str):
    if not record_id:
        return
    req = urllib.request.Request(
        f"{_base_url()}/api/collections/{COLECAO}/records/{record_id}", method="DELETE"
    )
    req.add_header("Authorization", _autenticar())
    try:
        urllib.request.urlopen(req, timeout=20)
    except urllib.error.HTTPError as e:
        if e.code != 404:  # já removida é resultado aceitável
            raise PocketBaseError(f"falha ao remover a imagem ({e.code})") from None
