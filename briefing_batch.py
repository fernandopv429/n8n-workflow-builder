"""Gera prompt do agente + sugestão de estrutura de funil/etapas do Kommo a
partir de um briefing livre, via Batch API da OpenAI (18/09/2026, a pedido do
Fernando — "se a pessoal enviar todas as instruções... então vale a pena").

Por quê Batch API aqui e não no chat: isso roda uma vez, ao criar o cliente
(passo 1), não em tempo real como o chat — tolera a espera (minutos a até
24h) e sai 50% mais barato. Os dois pedidos (prompt + estrutura Kommo) vão
juntos no mesmo lote.

Resultado é sempre uma SUGESTÃO — nada aqui aplica automaticamente no n8n ou
no Kommo. Quem revisa e aplica (via engrenagem/chat) é a pessoa.
"""
import json
import pathlib
import sys
import tempfile

RAIZ = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(RAIZ / ".pylibs"))

from config import carregar_env  # noqa: E402

from openai import OpenAI  # noqa: E402

from templates_nicho import prompt_base_do_nicho  # noqa: E402

MODELO = "gpt-4o-mini"
CUSTOM_ID_PROMPT = "prompt_agente"
CUSTOM_ID_KOMMO = "estrutura_kommo"




def _cliente_openai() -> OpenAI:
    return OpenAI(api_key=carregar_env()["OPENAI_API_KEY"])


# As instruções ficam separadas do formato da chamada porque os dois caminhos
# (Batch API e síncrono) mandam exatamente o mesmo texto — só muda o
# empacotamento. Se divergirem, o resultado passa a depender de qual caminho
# a pessoa escolheu, que é justamente o que não queremos.

def _instrucao_prompt(nicho: str, cliente_nome: str, briefing: str) -> str:
    template = prompt_base_do_nicho(nicho)
    return (
        "Adapte o prompt-base abaixo pro cliente e briefing informados. Mantenha "
        "qualquer placeholder de dado dinâmico e a estrutura geral — personalize "
        "tom, nome do agente e o contexto do negócio a partir do briefing. Devolva "
        "só o prompt final, sem comentário.\n\n"
        f"Cliente: {cliente_nome}\nNicho: {nicho}\n\nBriefing:\n{briefing}\n\n"
        f"Prompt-base do nicho:\n{template}"
    )


def _instrucao_kommo(cliente_nome: str, briefing: str) -> str:
    return (
        "A partir do briefing abaixo, proponha uma estrutura de funil de vendas "
        "pro Kommo CRM deste cliente. Devolva SÓ um JSON (sem comentário, sem "
        "markdown) no formato exato:\n"
        '{"funis": [{"nome": "string", "etapas": ["string", "string", ...]}]}\n\n'
        f"Cliente: {cliente_nome}\n\nBriefing:\n{briefing}"
    )


def _parsear_estrutura_kommo(bruto: str):
    if not bruto:
        return None
    try:
        return json.loads(bruto)
    except json.JSONDecodeError:
        return {"erro": "resposta não veio como JSON válido", "bruto": bruto}


def _linha_prompt(nicho: str, cliente_nome: str, briefing: str) -> dict:
    return {
        "custom_id": CUSTOM_ID_PROMPT,
        "method": "POST",
        "url": "/v1/chat/completions",
        "body": {
            "model": MODELO,
            "messages": [{"role": "user", "content": _instrucao_prompt(nicho, cliente_nome, briefing)}],
            "temperature": 0.4,
        },
    }


def _linha_kommo(cliente_nome: str, briefing: str) -> dict:
    return {
        "custom_id": CUSTOM_ID_KOMMO,
        "method": "POST",
        "url": "/v1/chat/completions",
        "body": {
            "model": MODELO,
            "messages": [{"role": "user", "content": _instrucao_kommo(cliente_nome, briefing)}],
            "temperature": 0.3,
            "response_format": {"type": "json_object"},
        },
    }


def processar_briefing_sincrono(nicho: str, cliente_nome: str, briefing: str) -> dict:
    """Mesmas duas gerações do lote, mas em chamadas normais — sai na hora e
    custa o dobro da Batch API. Devolve o mesmo formato de `verificar_batch`,
    pra quem chama tratar os dois caminhos igual."""
    client = _cliente_openai()

    r_prompt = client.chat.completions.create(
        model=MODELO,
        messages=[{"role": "user", "content": _instrucao_prompt(nicho, cliente_nome, briefing)}],
        temperature=0.4,
    )
    prompt_agente = r_prompt.choices[0].message.content

    r_kommo = client.chat.completions.create(
        model=MODELO,
        messages=[{"role": "user", "content": _instrucao_kommo(cliente_nome, briefing)}],
        temperature=0.3,
        response_format={"type": "json_object"},
    )
    estrutura_kommo = _parsear_estrutura_kommo(r_kommo.choices[0].message.content)

    return {"status": "completed", "prompt_agente": prompt_agente, "estrutura_kommo": estrutura_kommo}


def submeter_briefing(nicho: str, cliente_nome: str, briefing: str) -> str:
    """Sobe o arquivo .jsonl com os 2 pedidos e cria o batch. Devolve o batch_id."""
    linhas = [_linha_prompt(nicho, cliente_nome, briefing), _linha_kommo(cliente_nome, briefing)]

    with tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False) as f:
        for linha in linhas:
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


def verificar_batch(batch_id: str) -> dict:
    """Consulta o status do lote. Se 'completed', baixa e devolve os resultados —
    senão devolve só o status pra quem chamou tentar de novo depois."""
    client = _cliente_openai()
    lote = client.batches.retrieve(batch_id)

    if lote.status != "completed" or not lote.output_file_id:
        return {"status": lote.status, "prompt_agente": None, "estrutura_kommo": None}

    conteudo = client.files.content(lote.output_file_id).text
    resultados = {}
    for linha in conteudo.splitlines():
        if not linha.strip():
            continue
        item = json.loads(linha)
        custom_id = item.get("custom_id")
        corpo_resposta = item.get("response", {}).get("body", {})
        texto = corpo_resposta.get("choices", [{}])[0].get("message", {}).get("content", "")
        resultados[custom_id] = texto

    return {
        "status": "completed",
        "prompt_agente": resultados.get(CUSTOM_ID_PROMPT),
        "estrutura_kommo": _parsear_estrutura_kommo(resultados.get(CUSTOM_ID_KOMMO)),
    }
