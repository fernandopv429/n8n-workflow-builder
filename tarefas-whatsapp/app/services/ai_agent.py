"""IA que interpreta mensagens do WhatsApp (function calling) e que extrai
projeto/tarefas de uma transcrição de reunião (saída estruturada)."""
import json
from datetime import datetime

from openai import OpenAI

from .. import config
from . import crud

_cliente = None


def _openai() -> OpenAI:
    global _cliente
    if _cliente is None:
        _cliente = OpenAI(api_key=config.obrigatorio("OPENAI_API_KEY"))
    return _cliente


_FERRAMENTAS = [
    {
        "type": "function",
        "function": {
            "name": "criar_tarefa",
            "description": "Cria uma nova tarefa para o usuário.",
            "parameters": {
                "type": "object",
                "properties": {
                    "titulo": {"type": "string"},
                    "descricao": {"type": "string"},
                    "prazo_iso": {
                        "type": "string",
                        "description": "Data/hora do prazo em ISO 8601 (ex: 2026-10-25T18:00:00). Omitir se o usuário não disse prazo.",
                    },
                    "projeto_nome": {
                        "type": "string",
                        "description": "Nome do projeto a que a tarefa pertence, se mencionado.",
                    },
                },
                "required": ["titulo"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "criar_projeto",
            "description": "Cria um novo projeto para o usuário.",
            "parameters": {
                "type": "object",
                "properties": {
                    "nome": {"type": "string"},
                    "descricao": {"type": "string"},
                },
                "required": ["nome"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "listar_tarefas",
            "description": "Lista as tarefas do usuário, opcionalmente filtrando por status.",
            "parameters": {
                "type": "object",
                "properties": {
                    "status": {"type": "string", "enum": ["pendente", "concluida", "cancelada"]},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "concluir_tarefa",
            "description": "Marca uma tarefa como concluída, pelo título (ou parte dele).",
            "parameters": {
                "type": "object",
                "properties": {"titulo": {"type": "string"}},
                "required": ["titulo"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "listar_projetos",
            "description": "Lista os projetos do usuário.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
]

_SYSTEM_PROMPT = """Você é um assistente de produtividade que fala por WhatsApp em português do Brasil.
Sua função é ajudar o usuário a criar e consultar tarefas e projetos.
Seja direto e breve nas respostas (é WhatsApp, não e-mail).
Hoje é {hoje}. Quando o usuário mencionar um prazo relativo ("sexta", "amanhã",
"daqui 2 dias"), calcule a data real a partir de hoje e preencha prazo_iso.
Se a mensagem não pedir nenhuma ação de tarefa/projeto, apenas responda
normalmente, sem chamar função nenhuma."""


def _executar_ferramenta(usuario: dict, nome: str, args: dict) -> dict:
    if nome == "criar_tarefa":
        prazo = None
        if args.get("prazo_iso"):
            try:
                prazo = datetime.fromisoformat(args["prazo_iso"])
            except ValueError:
                prazo = None
        projeto_id = None
        if args.get("projeto_nome"):
            for p in crud.listar_projetos(usuario):
                if p["nome"].strip().lower() == args["projeto_nome"].strip().lower():
                    projeto_id = p["id"]
                    break
        tarefa = crud.criar_tarefa(
            usuario, args["titulo"], args.get("descricao", ""), prazo, projeto_id, origem="whatsapp"
        )
        return {"ok": True, "tarefa_id": tarefa["id"]}

    if nome == "criar_projeto":
        projeto = crud.criar_projeto(usuario, args["nome"], args.get("descricao", ""), origem="whatsapp")
        return {"ok": True, "projeto_id": projeto["id"]}

    if nome == "listar_tarefas":
        tarefas = crud.listar_tarefas(usuario, status=args.get("status"))
        return {
            "tarefas": [
                {
                    "titulo": t["titulo"],
                    "status": t["status"],
                    "prazo": t["prazo"].isoformat() if t["prazo"] else None,
                    "projeto": t["projeto_nome"],
                }
                for t in tarefas
            ]
        }

    if nome == "concluir_tarefa":
        alvo = args["titulo"].strip().lower()
        for t in crud.listar_tarefas(usuario, status="pendente"):
            if alvo in t["titulo"].strip().lower():
                crud.concluir_tarefa(usuario, t["id"])
                return {"ok": True, "titulo": t["titulo"]}
        return {"ok": False, "erro": "nenhuma tarefa pendente com esse título"}

    if nome == "listar_projetos":
        projetos = crud.listar_projetos(usuario)
        return {"projetos": [{"nome": p["nome"], "descricao": p["descricao"]} for p in projetos]}

    return {"erro": f"ferramenta desconhecida: {nome}"}


def responder_mensagem_whatsapp(usuario: dict, texto_usuario: str) -> str:
    cliente = _openai()
    mensagens = [
        {"role": "system", "content": _SYSTEM_PROMPT.format(hoje=datetime.now().strftime("%Y-%m-%d (%A)"))},
        {"role": "user", "content": texto_usuario},
    ]

    resposta = cliente.chat.completions.create(
        model="gpt-4o-mini", messages=mensagens, tools=_FERRAMENTAS, tool_choice="auto"
    )
    mensagem = resposta.choices[0].message

    if not mensagem.tool_calls:
        return mensagem.content or "Ok."

    mensagens.append(mensagem)
    for chamada in mensagem.tool_calls:
        args = json.loads(chamada.function.arguments or "{}")
        resultado = _executar_ferramenta(usuario, chamada.function.name, args)
        mensagens.append(
            {"role": "tool", "tool_call_id": chamada.id, "content": json.dumps(resultado, default=str)}
        )

    final = cliente.chat.completions.create(model="gpt-4o-mini", messages=mensagens)
    return final.choices[0].message.content or "Pronto."


_SCHEMA_EXTRACAO_REUNIAO = {
    "type": "json_schema",
    "json_schema": {
        "name": "extracao_reuniao",
        "schema": {
            "type": "object",
            "properties": {
                "projeto_nome": {"type": "string"},
                "projeto_descricao": {"type": "string"},
                "tarefas": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "titulo": {"type": "string"},
                            "descricao": {"type": "string"},
                            "prazo_iso": {"type": ["string", "null"]},
                        },
                        "required": ["titulo", "descricao", "prazo_iso"],
                        "additionalProperties": False,
                    },
                },
            },
            "required": ["projeto_nome", "projeto_descricao", "tarefas"],
            "additionalProperties": False,
        },
        "strict": True,
    },
}


def extrair_projeto_e_tarefas(titulo_reuniao: str, transcricao: str) -> dict:
    """A partir da transcrição de uma reunião, devolve um projeto + lista de
    tarefas combinadas (ação, dono, prazo quando mencionado)."""
    cliente = _openai()
    resposta = cliente.chat.completions.create(
        model="gpt-4o-mini",
        messages=[
            {
                "role": "system",
                "content": (
                    "Você analisa a transcrição de uma reunião e extrai: um nome de "
                    "projeto que resuma o assunto, e a lista de ações/tarefas combinadas "
                    "durante a reunião. Hoje é "
                    f"{datetime.now().strftime('%Y-%m-%d')}. Se um prazo relativo for "
                    "mencionado, calcule a data em prazo_iso; senão deixe null. Se a "
                    "reunião não tiver nenhuma ação clara, devolva tarefas como lista vazia."
                ),
            },
            {"role": "user", "content": f"Título da reunião: {titulo_reuniao}\n\nTranscrição:\n{transcricao}"},
        ],
        response_format=_SCHEMA_EXTRACAO_REUNIAO,
    )
    return json.loads(resposta.choices[0].message.content)
