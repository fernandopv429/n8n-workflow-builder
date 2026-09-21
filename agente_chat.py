"""Loop de tool-calling do chat da tela de gerenciamento. Único ponto que
combina as duas famílias de ferramentas:

1. n8n_edicao.py — ler/trocar o prompt do agente principal, ler/trocar campos
   do node Database. Curado e restrito (ver n8n_edicao.py).
2. mcp_kommo_client.py — proxy pro MCP do Kommo já hospedado no n8n. O
   kommo_domain/access_token NUNCA são expostos pro modelo como parâmetro —
   são injetados aqui, lidos do workflow do próprio cliente a cada chamada
   (nunca guardados no nosso banco).

Rastreado pelo AgentOps (é a parte genuinamente não-determinística do
sistema — diferente do prompt_gerador.py, que é uma chamada isolada).
"""
import json
import pathlib
import sys

RAIZ = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(RAIZ / ".pylibs"))
sys.path.insert(0, str(RAIZ))

from config import carregar_env  # noqa: E402

import agentops  # noqa: E402
from openai import OpenAI  # noqa: E402

import db  # noqa: E402
import mcp_kommo_client  # noqa: E402
import n8n_edicao  # noqa: E402

MODELO = "gpt-4o-mini"
MAX_RODADAS_FERRAMENTA = 6

# só "base-url" fica exposto ao chat — "kommo-token" é segredo, nunca deve
# aparecer na conversa (chat_mensagens/logs não são criptografados).
CAMPOS_DATABASE_NO_CHAT = ("base-url",)

_inicializado = False




def _garantir_agentops():
    global _inicializado
    if _inicializado:
        return
    chave = carregar_env().get("AGENTOPS_API_KEY")
    if chave:
        agentops.init(api_key=chave)
    _inicializado = True


def _ferramentas_n8n() -> list:
    return [
        {
            "type": "function",
            "function": {
                "name": "ler_prompt_agente",
                "description": "Lê o prompt (system message) atual do agente principal deste cliente.",
                "parameters": {"type": "object", "properties": {}, "required": []},
            },
        },
        {
            "type": "function",
            "function": {
                "name": "atualizar_prompt_agente",
                "description": "Substitui o prompt (system message) do agente principal deste cliente pelo texto novo.",
                "parameters": {
                    "type": "object",
                    "properties": {"novo_prompt": {"type": "string", "description": "Texto completo do novo prompt"}},
                    "required": ["novo_prompt"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "ler_campo_database",
                "description": "Lê um campo do node Database do workflow deste cliente.",
                "parameters": {
                    "type": "object",
                    "properties": {"campo": {"type": "string", "enum": list(CAMPOS_DATABASE_NO_CHAT)}},
                    "required": ["campo"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "atualizar_campo_database",
                "description": "Atualiza um campo do node Database do workflow deste cliente.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "campo": {"type": "string", "enum": list(CAMPOS_DATABASE_NO_CHAT)},
                        "valor": {"type": "string"},
                    },
                    "required": ["campo", "valor"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "listar_nodes",
                "description": "Lista o nome e o tipo de todos os nodes do workflow deste cliente — use antes de renomear_node pra confirmar o nome exato.",
                "parameters": {"type": "object", "properties": {}, "required": []},
            },
        },
        {
            "type": "function",
            "function": {
                "name": "renomear_node",
                "description": (
                    "Renomeia um node do workflow, atualizando automaticamente conexões e "
                    "referências de expressão $('NomeAntigo') em outros nodes. Não pode "
                    "renomear 'AgenteMov' nem 'Database' (nomes fixos usados pelo sistema)."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "nome_atual": {"type": "string"},
                        "nome_novo": {"type": "string"},
                    },
                    "required": ["nome_atual", "nome_novo"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "ler_node",
                "description": "Lê o JSON completo (type, parameters, tudo) de um node específico do workflow.",
                "parameters": {
                    "type": "object",
                    "properties": {"nome_node": {"type": "string"}},
                    "required": ["nome_node"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "atualizar_node",
                "description": (
                    "Substitui a definição INTEIRA de um node existente (type, parameters, "
                    "tudo) pelo JSON fornecido — cobre editar parâmetros internos, mudar o "
                    "tipo do node, etc. Sem verificação de conteúdo. Backup automático antes."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "nome_node": {"type": "string"},
                        "novo_node": {"type": "object", "description": "JSON completo do node (mesmo formato do n8n)"},
                    },
                    "required": ["nome_node", "novo_node"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "criar_node",
                "description": "Adiciona um node novo ao workflow (JSON completo, mesmo formato do n8n). Backup automático antes.",
                "parameters": {
                    "type": "object",
                    "properties": {"node": {"type": "object", "description": "JSON completo do node novo"}},
                    "required": ["node"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "remover_node",
                "description": "Remove um node do workflow (e as conexões que apontavam pra ele). Backup automático antes.",
                "parameters": {
                    "type": "object",
                    "properties": {"nome_node": {"type": "string"}},
                    "required": ["nome_node"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "ler_connections",
                "description": "Lê o objeto completo de conexões (o 'fiação' entre nodes) do workflow.",
                "parameters": {"type": "object", "properties": {}, "required": []},
            },
        },
        {
            "type": "function",
            "function": {
                "name": "atualizar_connections",
                "description": "Substitui o objeto de conexões inteiro do workflow — reestrutura o fluxo. Backup automático antes.",
                "parameters": {
                    "type": "object",
                    "properties": {"connections": {"type": "object", "description": "Objeto connections completo, mesmo formato do n8n"}},
                    "required": ["connections"],
                },
            },
        },
    ]


def _preparar_ferramentas_kommo() -> list:
    """Pega o schema de cada tool do MCP do Kommo e tira kommo_domain/access_token
    — esses dois são injetados na hora da chamada, nunca preenchidos pelo modelo."""
    preparadas = []
    for f in mcp_kommo_client.listar_ferramentas():
        schema = dict(f["input_schema"] or {})
        propriedades = {
            k: v for k, v in schema.get("properties", {}).items()
            if k not in ("kommo_domain", "access_token")
        }
        obrigatorios = [c for c in schema.get("required", []) if c not in ("kommo_domain", "access_token")]
        preparadas.append({
            "type": "function",
            "function": {
                "name": f["name"],
                "description": f["description"] or "",
                "parameters": {"type": "object", "properties": propriedades, "required": obrigatorios},
            },
        })
    return preparadas


def _bloco_sugestao(cliente: dict) -> str:
    """Sugestões geradas a partir do briefing (briefing_batch.py) entram no
    contexto do chat — senão a pessoa teria que copiar e colar o que já está
    salvo no banco pra conseguir mandar aplicar."""
    partes = []
    if cliente.get("estrutura_kommo_sugerida"):
        partes.append(
            "Estrutura de funil sugerida pro Kommo a partir do briefing (ainda NÃO "
            "aplicada — só vira realidade se o usuário pedir e você chamar "
            "kommo_criar_funil):\n"
            + json.dumps(cliente["estrutura_kommo_sugerida"], ensure_ascii=False)
        )
    if cliente.get("prompt_sugerido"):
        partes.append(
            "Também existe um prompt sugerido a partir do briefing (ainda NÃO "
            "aplicado no agente — só vira realidade via atualizar_prompt_agente, "
            "se o usuário pedir). Primeiros 500 caracteres:\n"
            + cliente["prompt_sugerido"][:500]
        )
    return ("\n\n" + "\n\n".join(partes) + "\n") if partes else ""


def _system_prompt(cliente: dict) -> str:
    return (
        f"Você ajuda a gerenciar o agente de IA e o CRM (Kommo) do cliente "
        f"'{cliente['cliente_nome']}' (nicho: {cliente['nicho']}) da A5 Ecossistema."
        + _bloco_sugestao(cliente)
        + "\nFerramentas disponíveis:\n"
        "- ler/reescrever o prompt (system message) do agente principal\n"
        "- ler/reescrever 'kommo-token' e 'base-url' do node Database\n"
        "- listar_nodes / ler_node / renomear_node / atualizar_node / criar_node / "
        "remover_node / ler_connections / atualizar_connections — acesso completo à "
        "estrutura do workflow (renomear_node já corrige conexões e expressões "
        "sozinho; as outras exigem você mesmo montar o JSON certo)\n"
        "- ferramentas do Kommo (leads, funis, etapas, contatos)\n\n"
        "'AgenteMov' e 'Database' NUNCA podem ser renomeados, editados por "
        "atualizar_node ou removidos — são nomes fixos dos quais o resto do sistema "
        "depende pra continuar gerenciando esse cliente.\n\n"
        "As ferramentas estruturais (atualizar_node, criar_node, remover_node, "
        "atualizar_connections) NÃO têm verificação automática de conteúdo — "
        "confirme com o usuário antes de chamar qualquer uma delas, descrevendo "
        "exatamente o que vai mudar, EXCETO quando o próprio pedido já for uma "
        "instrução explícita e específica pra fazer a mudança. Sempre que possível, "
        "leia o node com ler_node antes de reescrevê-lo com atualizar_node, pra não "
        "perder parâmetros que o usuário não mencionou.\n\n"
        "Se o pedido não corresponder a NENHUMA ferramenta, diga isso claramente — "
        "NUNCA execute uma ferramenta diferente do que foi pedido como substituto e "
        "diga que deu certo (ex: só chamar atualizar_prompt_agente quando o pedido "
        "for sobre o PROMPT/persona, nunca como tentativa de atender outro pedido).\n\n"
        "Nunca invente resultado de ferramenta — se uma chamada falhar, diga isso "
        "claramente. Antes de executar qualquer ação MARCADA como irreversível "
        "(excluir funil, excluir etapa), pergunte e espere confirmação explícita "
        "do usuário antes de chamar a ferramenta. Seja direto e breve nas respostas."
    )


FERRAMENTAS_ESTRUTURAIS = ("atualizar_node", "criar_node", "remover_node", "atualizar_connections")


def _executar_ferramenta(nome: str, args: dict, workflow_id: str, cliente_id: int) -> str:
    try:
        if nome in FERRAMENTAS_ESTRUTURAIS:
            # backup do workflow inteiro ANTES da mudança — sem verificação de
            # conteúdo nessas ferramentas, isso é a única forma de reverter se
            # a edição quebrar o workflow.
            backup = n8n_edicao.ler_workflow_completo(workflow_id)
            db.registrar_log(
                cliente_id, "sistema",
                f"Backup automático do workflow antes de '{nome}' (pra reverter, restaurar este JSON via PUT /api/v1/workflows/{workflow_id}).",
                detalhe=backup,
            )

        if nome == "ler_node":
            return json.dumps(n8n_edicao.ler_node(workflow_id, args["nome_node"]), ensure_ascii=False)
        if nome == "atualizar_node":
            return json.dumps(
                n8n_edicao.atualizar_node(workflow_id, args["nome_node"], args["novo_node"]), ensure_ascii=False
            )
        if nome == "criar_node":
            return json.dumps(n8n_edicao.criar_node(workflow_id, args["node"]), ensure_ascii=False)
        if nome == "remover_node":
            return json.dumps(n8n_edicao.remover_node(workflow_id, args["nome_node"]), ensure_ascii=False)
        if nome == "ler_connections":
            return json.dumps(n8n_edicao.ler_connections(workflow_id), ensure_ascii=False)
        if nome == "atualizar_connections":
            return json.dumps(
                n8n_edicao.atualizar_connections(workflow_id, args["connections"]), ensure_ascii=False
            )

        if nome == "ler_prompt_agente":
            return json.dumps(n8n_edicao.ler_prompt_agente(workflow_id), ensure_ascii=False)
        if nome == "atualizar_prompt_agente":
            return json.dumps(
                n8n_edicao.atualizar_prompt_agente(workflow_id, args["novo_prompt"]), ensure_ascii=False
            )
        if nome == "ler_campo_database":
            campo = args.get("campo")
            if campo not in CAMPOS_DATABASE_NO_CHAT:
                return f"erro: campo '{campo}' não permitido pelo chat"
            return n8n_edicao.ler_campo_database(workflow_id, campo)
        if nome == "atualizar_campo_database":
            campo = args.get("campo")
            if campo not in CAMPOS_DATABASE_NO_CHAT:
                return f"erro: campo '{campo}' não permitido pelo chat"
            return json.dumps(
                n8n_edicao.atualizar_campo_database(workflow_id, campo, args["valor"]), ensure_ascii=False
            )
        if nome == "listar_nodes":
            return json.dumps(n8n_edicao.listar_nodes(workflow_id), ensure_ascii=False)
        if nome == "renomear_node":
            return json.dumps(
                n8n_edicao.renomear_node(workflow_id, args["nome_atual"], args["nome_novo"]), ensure_ascii=False
            )
        if nome.startswith("kommo_"):
            credenciais = n8n_edicao.ler_credenciais_kommo(workflow_id)
            return mcp_kommo_client.chamar_ferramenta(nome, {**args, **credenciais})
        return f"erro: ferramenta desconhecida '{nome}'"
    except Exception as e:  # noqa: BLE001 — devolve pro modelo como resultado da tool, não derruba o chat
        return f"erro: {e}"


def processar_mensagem(cliente_id: int, texto_usuario: str) -> str:
    cliente = db.obter_cliente(cliente_id)
    if cliente is None:
        raise ValueError("cliente não encontrado")
    if cliente["status"] != "clonado":
        raise ValueError("cliente ainda não foi clonado — configure as credenciais primeiro")
    workflow_id = cliente["workflow_novo_id"]

    _garantir_agentops()
    client = OpenAI(api_key=carregar_env()["OPENAI_API_KEY"])

    db.salvar_mensagem(cliente_id, "user", texto_usuario)
    historico = db.listar_mensagens(cliente_id)
    mensagens = [{"role": "system", "content": _system_prompt(cliente)}]
    mensagens += [{"role": m["role"], "content": m["conteudo"]} for m in historico if m["role"] in ("user", "assistant")]

    ferramentas = _ferramentas_n8n() + _preparar_ferramentas_kommo()

    for _ in range(MAX_RODADAS_FERRAMENTA):
        resposta = client.chat.completions.create(model=MODELO, messages=mensagens, tools=ferramentas)
        msg = resposta.choices[0].message

        if not msg.tool_calls:
            texto_final = msg.content or ""
            db.salvar_mensagem(cliente_id, "assistant", texto_final)
            return texto_final

        mensagens.append({
            "role": "assistant",
            "content": msg.content,
            "tool_calls": [tc.model_dump() for tc in msg.tool_calls],
        })
        for tc in msg.tool_calls:
            args = json.loads(tc.function.arguments or "{}")
            resultado = _executar_ferramenta(tc.function.name, args, workflow_id, cliente_id)
            db.registrar_log(
                cliente_id, "tool_call",
                f"{tc.function.name}({json.dumps(args, ensure_ascii=False)}) -> {resultado[:200]}",
            )
            mensagens.append({"role": "tool", "tool_call_id": tc.id, "content": resultado})

    aviso = "Não consegui concluir em poucas etapas — tenta reformular o pedido?"
    db.salvar_mensagem(cliente_id, "assistant", aviso)
    return aviso
