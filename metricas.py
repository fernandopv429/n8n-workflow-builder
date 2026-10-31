"""Mede quanto tempo cada chamada de ferramenta leva e se deu certo, grava em
Postgres (`db.metricas_ferramentas`) — é o dado que embasa decisão de onde
vale a pena repetir o que já foi feito com o Kommo (tirar do MCP, cachear
schema/transporte): sem saber o que é mais chamado e o que é mais lento,
qualquer otimização é chute.

Chamado nos três lugares que executam ferramenta: worker_agente.py
(paciente), agente_chat.py (chat de gerenciamento), agente_geral.py
(WhatsApp da A5) — um por `origem`, pra não confundir ferramenta de nome
igual chamada de lugares diferentes.
"""
import pathlib
import sys
import time

RAIZ = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(RAIZ / ".pylibs"))
sys.path.insert(0, str(RAIZ))

import db  # noqa: E402


def medir(origem: str, cliente_id: int, ferramenta: str, chamada):
    """Roda `chamada()` (callable sem argumento — normalmente um lambda em
    volta de `_executar(...)`), grava duração e sucesso, e deixa a exceção
    original seguir se `chamada()` levantar.

    Nunca muda o que já acontecia: se `chamada()` dá erro, o erro sobe igual
    a antes — a métrica só observa, não decide nada. Convenção do resto do
    sistema é "erro:" no início do texto de retorno pra indicar falha sem
    levantar exceção; é isso que `sucesso` reconhece.
    """
    inicio = time.monotonic()
    sucesso = False
    try:
        resultado = chamada()
        sucesso = isinstance(resultado, str) and not resultado.startswith("erro")
        return resultado
    finally:
        duracao_ms = int((time.monotonic() - inicio) * 1000)
        try:
            db.registrar_metrica_ferramenta(cliente_id, origem, ferramenta, sucesso, duracao_ms)
        except Exception:  # noqa: BLE001 — métrica não pode derrubar o atendimento
            pass
