"""Manda uma mensagem do chat de gerenciamento pela Batch API da OpenAI —
sai 50% mais barato que uma chamada normal, ao custo de a resposta demorar
minutos a até 24h em vez de vir na hora (ver agente_chat.py: enviar_em_lote/
verificar_lote, e a tabela `chat_lotes` em db.py, que guarda o pedido até a
resposta chegar).

Até 25/09/2026 este módulo também gerava prompt + sugestão de estrutura de
Kommo a partir de um briefing livre, no passo 1 de criação do cliente (antes
de existir Kommo cadastrado pra consultar). Saiu quando esse passo virou
"só o nome" e o briefing passou a ser conversa no chat, depois das
credenciais — removido junto com nicho/workflow_origem_id (30/10/2026).
"""
import json
import pathlib
import sys
import tempfile

RAIZ = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(RAIZ / ".pylibs"))

from config import carregar_env  # noqa: E402

from openai import OpenAI  # noqa: E402

MODELO = "gpt-4o-mini"


def _cliente_openai() -> OpenAI:
    return OpenAI(api_key=carregar_env()["OPENAI_API_KEY"])


CUSTOM_ID_CONVERSA = "conversa"


def submeter_conversa(mensagens: list) -> str:
    """Manda uma conversa do chat pela Batch API. Devolve o batch_id.

    SEM ferramentas: a Batch API é de ida e volta única, e tool-calling precisa
    de vários turnos (modelo pede ferramenta -> executamos -> devolvemos). Cada
    turno viraria outro lote de horas. Então o modo lote serve pra pedido de
    geração de texto (escrever prompt, propor estrutura), não pra ação.
    """
    linha = {
        "custom_id": CUSTOM_ID_CONVERSA,
        "method": "POST",
        "url": "/v1/chat/completions",
        "body": {"model": MODELO, "messages": mensagens, "temperature": 0.4},
    }
    with tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False) as f:
        f.write(json.dumps(linha, ensure_ascii=False) + "\n")
        caminho = f.name

    client = _cliente_openai()
    try:
        arquivo = client.files.create(file=open(caminho, "rb"), purpose="batch")
        lote = client.batches.create(
            input_file_id=arquivo.id,
            endpoint="/v1/chat/completions",
            completion_window="24h",
        )
    finally:
        pathlib.Path(caminho).unlink(missing_ok=True)
    return lote.id


def verificar_conversa(batch_id: str) -> dict:
    """Status do lote de uma conversa. Com 'completed', devolve a resposta."""
    client = _cliente_openai()
    lote = client.batches.retrieve(batch_id)
    if lote.status != "completed" or not lote.output_file_id:
        return {"status": lote.status, "resposta": None}

    conteudo = client.files.content(lote.output_file_id).text
    for linha in conteudo.splitlines():
        if not linha.strip():
            continue
        item = json.loads(linha)
        if item.get("custom_id") == CUSTOM_ID_CONVERSA:
            corpo = item.get("response", {}).get("body", {})
            texto = corpo.get("choices", [{}])[0].get("message", {}).get("content", "")
            return {"status": "completed", "resposta": texto}
    return {"status": "completed", "resposta": None}
