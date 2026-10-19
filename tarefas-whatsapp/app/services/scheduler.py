"""Job que roda em segundo plano e manda lembrete de tarefa antes do prazo."""
import logging

from apscheduler.schedulers.background import BackgroundScheduler

from . import crud, evolution_client

logger = logging.getLogger("lembretes")

JANELA_LEMBRETE_MINUTOS = 24 * 60  # avisa até 24h antes do prazo


def _checar_lembretes():
    for tarefa in crud.tarefas_para_lembrar(JANELA_LEMBRETE_MINUTOS):
        texto = (
            f"Lembrete: a tarefa \"{tarefa['titulo']}\" vence em breve "
            f"({tarefa['prazo'].strftime('%d/%m %H:%M')})."
        )
        try:
            evolution_client.enviar_mensagem(tarefa["telefone_whatsapp"], texto)
            crud.marcar_lembrete_enviado(tarefa["id"])
        except Exception:
            logger.exception("falha ao enviar lembrete da tarefa %s", tarefa["id"])


def iniciar() -> BackgroundScheduler:
    scheduler = BackgroundScheduler(timezone="America/Sao_Paulo")
    scheduler.add_job(_checar_lembretes, "interval", minutes=15, id="checar_lembretes")
    scheduler.start()
    return scheduler
