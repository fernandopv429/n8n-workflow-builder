"""Arquivos que o agente envia: onde ficam em disco e por qual URL saem.

A Evolution BAIXA o arquivo — ela não recebe bytes nossos, recebe um endereço.
Isso obriga duas coisas: a URL tem que ser alcançável de fora, e por isso fica
fora da senha do painel (a Evolution não sabe autenticar). O que protege é o
`id_publico`, aleatório e longo: sem ele não há como listar nem adivinhar.

O diretório PRECISA ser um volume. Sem volume, o contêiner é recriado no
redeploy e todo arquivo enviado some, enquanto as linhas continuam no banco
apontando pra nada — o agente então promete um PDF que não existe mais.
"""
import os
import pathlib
import secrets

import config

RAIZ = pathlib.Path(__file__).resolve().parent

# 16 MB é o teto do WhatsApp pra vídeo; acima disso o envio falha no aparelho
# de quem recebe, não aqui, o que seria um defeito invisível pra nós.
LIMITE_MB = 16

EXTENSOES = {
    "image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp",
    "video/mp4": ".mp4", "audio/mpeg": ".mp3", "audio/ogg": ".ogg",
    "application/pdf": ".pdf",
}


def diretorio() -> pathlib.Path:
    d = pathlib.Path(config.carregar_env().get("MIDIAS_DIR", "").strip()
                     or os.environ.get("MIDIAS_DIR", "").strip()
                     or (RAIZ / "dados" / "midias"))
    d.mkdir(parents=True, exist_ok=True)
    return d


def novo_id() -> str:
    return secrets.token_urlsafe(24)


def gravar(id_publico: str, conteudo: bytes, mimetype: str) -> pathlib.Path:
    ext = EXTENSOES.get((mimetype or "").lower(), "")
    caminho = diretorio() / f"{id_publico}{ext}"
    caminho.write_bytes(conteudo)
    return caminho


def apagar(caminho: str) -> bool:
    try:
        pathlib.Path(caminho).unlink()
        return True
    except OSError:
        return False


def url_publica(id_publico: str, nome_original: str = "") -> str:
    """URL absoluta: a Evolution roda em outro host, caminho relativo não serve.

    PAINEL_URL é o endereço público do painel. Sem ele não há como montar a
    URL, e falhar aqui é melhor que mandar um link que ninguém consegue abrir.
    """
    base = (config.carregar_env().get("PAINEL_URL", "").strip()
            or os.environ.get("PAINEL_URL", "").strip()).rstrip("/")
    if not base:
        raise RuntimeError(
            "PAINEL_URL não configurada: sem ela não dá pra montar o endereço "
            "que a Evolution usa pra baixar o arquivo")
    return f"{base}/midias/{id_publico}"
