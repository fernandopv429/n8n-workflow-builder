"""Juiz (LLM) que relê a conversa do agente com um paciente/lead e aponta
problema — "o agente seguiu o próprio prompt?" — pra quem gerencia o cliente
não precisar ler cada conversa pra descobrir isso.

Só fala com a OpenAI, não grava nada (quem chama decide o que fazer com o
veredito — ver `servidor.py`: `/clientes/<id>/qa/rodar`). Chamada síncrona,
não Batch API: o volume por rodada é pequeno (`db.contatos_pendentes_de_qa`
já limita) e quem chama precisa da resposta na hora, pro botão do painel.
"""
import json
import pathlib
import sys

RAIZ = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(RAIZ / ".pylibs"))
sys.path.insert(0, str(RAIZ))

from config import carregar_env  # noqa: E402

from openai import OpenAI  # noqa: E402

MODELO = "gpt-4o-mini"

SEVERIDADES = ("ok", "atencao", "critico")

INSTRUCAO = """Você avalia a qualidade de um atendimento automático de WhatsApp.

Abaixo vêm as INSTRUÇÕES que o agente recebeu (o que ele deveria fazer) e a
CONVERSA real dele com o paciente/lead. Julgue só se o agente seguiu as
próprias instruções e atendeu bem — não dê opinião sobre as instruções em si.

Aponte problema real, concreto, com base no que está escrito — nunca invente
ou suponha o que "provavelmente" aconteceu fora do texto.

Marque "critico" quando o agente: contradisse a si mesmo, inventou uma
informação que não estava nas instruções, ignorou uma pergunta direta do
paciente, ou quebrou uma regra explícita das instruções.
Marque "atencao" quando o tom ficou ruim, uma resposta ficou confusa ou
incompleta, ou o agente repetiu pergunta que o paciente já tinha respondido —
mas sem o risco das categorias acima.
Marque "ok" quando não há nada digno de nota.

Devolva SÓ um JSON (sem comentário, sem markdown) no formato exato:
{{"severidade": "ok"|"atencao"|"critico", "problemas": ["string", ...], "resumo": "string"}}

"problemas" é uma lista curta (pode ser vazia) de apontamentos de uma frase
cada. "resumo" é 1 a 2 frases explicando o veredito — vazio não serve, mesmo
pra "ok" diga por que está ok (ex: "respondeu a pergunta e seguiu o roteiro
de agendamento").

INSTRUÇÕES DO AGENTE:
{prompt_cliente}

CONVERSA (role "user" é o paciente, "assistant" é o agente):
{conversa}
"""


def _formatar_conversa(mensagens: list) -> str:
    linhas = []
    for m in mensagens:
        quem = "paciente" if m["role"] == "user" else "agente"
        if m["role"] not in ("user", "assistant"):
            continue
        linhas.append(f"{quem}: {m['conteudo']}")
    return "\n".join(linhas)


def avaliar_conversa(prompt_cliente: str, mensagens: list) -> dict | None:
    """Pede pro modelo julgar a conversa. Devolve
    {"severidade", "problemas", "resumo"} ou None se a chamada falhar ou a
    resposta vier fora do formato — não dá pra inventar veredito, quem chama
    decide se tenta de novo depois (ver `db.contatos_pendentes_de_qa`, que
    não marca como avaliado até `registrar_avaliacao_qa` rodar)."""
    conversa = _formatar_conversa(mensagens)
    if not conversa.strip():
        return None

    client = OpenAI(api_key=carregar_env()["OPENAI_API_KEY"])
    instrucao = INSTRUCAO.format(
        prompt_cliente=(prompt_cliente or "(nenhuma instrução cadastrada)").strip()[:6000],
        conversa=conversa,
    )

    try:
        r = client.chat.completions.create(
            model=MODELO,
            messages=[{"role": "user", "content": instrucao}],
            temperature=0.2,
            response_format={"type": "json_object"},
        )
        veredito = json.loads(r.choices[0].message.content)
    except Exception:  # noqa: BLE001 — chamada/parse ruim não pode travar o lote
        return None

    if veredito.get("severidade") not in SEVERIDADES:
        return None
    if not isinstance(veredito.get("problemas"), list):
        veredito["problemas"] = []
    if not str(veredito.get("resumo") or "").strip():
        return None

    return {
        "severidade": veredito["severidade"],
        "problemas": [str(p) for p in veredito["problemas"]],
        "resumo": str(veredito["resumo"]).strip(),
    }
