"""Webhook da Evolution API: mensagem de texto vira comando pra IA."""
import logging

from fastapi import APIRouter, Request

from ..services import ai_agent, crud, evolution_client

logger = logging.getLogger("whatsapp")
router = APIRouter()


def _extrair_telefone_e_texto(payload: dict) -> tuple[str, str] | None:
    dados = payload.get("data", {})
    chave = dados.get("key", {})
    if chave.get("fromMe"):
        return None  # eco da própria mensagem enviada pelo sistema
    remote_jid = chave.get("remoteJid", "")
    telefone = remote_jid.split("@")[0]
    mensagem = dados.get("message", {})
    texto = mensagem.get("conversation") or mensagem.get("extendedTextMessage", {}).get("text")
    if not telefone or not texto:
        return None
    return telefone, texto


@router.post("/webhook/whatsapp")
async def webhook_whatsapp(request: Request):
    payload = await request.json()
    if payload.get("event") != "messages.upsert":
        return {"ok": True}

    extraido = _extrair_telefone_e_texto(payload)
    if not extraido:
        return {"ok": True}
    telefone, texto = extraido

    usuario = crud.buscar_usuario_por_telefone(telefone)
    if not usuario:
        logger.info("mensagem de numero nao cadastrado: %s", telefone)
        return {"ok": True}

    try:
        resposta = ai_agent.responder_mensagem_whatsapp(usuario, texto)
        evolution_client.enviar_mensagem(telefone, resposta)
    except Exception:
        logger.exception("falha ao processar mensagem de %s", telefone)

    return {"ok": True}
