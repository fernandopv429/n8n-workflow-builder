"""Recebe a transcrição de uma reunião do Meet (chamada feita pelo workflow do
n8n, que já cuida de monitorar o Google Drive e identificar o email do dono —
ver conversa de implantação). Aqui só: achar o usuário cadastrado por esse
email, extrair projeto/tarefas com IA, e gravar."""
import logging

from fastapi import APIRouter, Header, HTTPException, Request

from .. import config
from ..services import ai_agent, crud, evolution_client

logger = logging.getLogger("reunioes")
router = APIRouter()


@router.post("/api/reunioes/ingest")
async def ingerir_reuniao(request: Request, authorization: str = Header(default="")):
    token_esperado = config.obrigatorio("MEET_INGEST_TOKEN")
    if authorization != f"Bearer {token_esperado}":
        raise HTTPException(status_code=401, detail="token inválido")

    corpo = await request.json()
    pasta_drive_id = corpo.get("pasta_drive_id")
    titulo = corpo.get("titulo", "Reunião sem título")
    email_dono = corpo.get("email_dono", "").strip().lower()
    transcricao = corpo.get("transcricao", "")

    if not pasta_drive_id or not email_dono or not transcricao:
        raise HTTPException(status_code=400, detail="pasta_drive_id, email_dono e transcricao são obrigatórios")

    usuario = crud.buscar_usuario_por_email(email_dono)
    if not usuario:
        logger.info("reuniao de email nao cadastrado, ignorada: %s", email_dono)
        crud.registrar_reuniao(pasta_drive_id, titulo, email_dono, None, transcricao)
        return {"ok": True, "criado": False, "motivo": "email não cadastrado no sistema"}

    reuniao = crud.registrar_reuniao(pasta_drive_id, titulo, email_dono, usuario["id"], transcricao)

    extraido = ai_agent.extrair_projeto_e_tarefas(titulo, transcricao)
    projeto = crud.criar_projeto(
        usuario, extraido["projeto_nome"], extraido.get("projeto_descricao", ""), origem="reuniao"
    )
    crud.vincular_projeto_criado(reuniao["id"], projeto["id"])

    tarefas_criadas = []
    for t in extraido.get("tarefas", []):
        prazo = None
        if t.get("prazo_iso"):
            try:
                from datetime import datetime

                prazo = datetime.fromisoformat(t["prazo_iso"])
            except ValueError:
                prazo = None
        tarefa = crud.criar_tarefa(
            usuario, t["titulo"], t.get("descricao", ""), prazo, projeto["id"], origem="reuniao"
        )
        tarefas_criadas.append(tarefa)

    if usuario.get("telefone_whatsapp") and tarefas_criadas:
        resumo = "\n".join(f"- {t['titulo']}" for t in tarefas_criadas)
        try:
            evolution_client.enviar_mensagem(
                usuario["telefone_whatsapp"],
                f"Reunião \"{titulo}\" processada. Criei o projeto \"{projeto['nome']}\" com estas tarefas:\n{resumo}",
            )
        except Exception:
            logger.exception("falha ao notificar usuario %s sobre reuniao", usuario["id"])

    return {"ok": True, "criado": True, "projeto_id": projeto["id"], "tarefas": len(tarefas_criadas)}
