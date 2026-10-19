"""Envio de mensagens de WhatsApp via Evolution API."""
import httpx

from .. import config


def enviar_mensagem(telefone: str, texto: str) -> None:
    url = config.obrigatorio("EVOLUTION_URL").rstrip("/")
    instancia = config.obrigatorio("EVOLUTION_INSTANCE")
    apikey = config.obrigatorio("EVOLUTION_APIKEY")
    resposta = httpx.post(
        f"{url}/message/sendText/{instancia}",
        headers={"apikey": apikey},
        json={"number": telefone, "text": texto},
        timeout=15,
    )
    resposta.raise_for_status()
