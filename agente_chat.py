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

import briefing_batch  # noqa: E402
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
        "Nunca invente resultado de ferramenta — se uma chamada falhar, diga o "
        "erro que voltou, textualmente, em vez de dizer só que 'os parâmetros "
        "podem estar incorretos'.\n\n"

        "COMO AGIR (aprendido com falhas reais):\n"
        "- Resolva os IDs você mesmo. Se o usuário citar um funil/etapa/lead "
        "pelo NOME, chame kommo_listar_funis (ou o listar correspondente) pra "
        "descobrir o id. Não peça id pro usuário — ele não tem por que saber.\n"
        "- Peça confirmação só antes de EXCLUIR (funil, etapa, node, conexões). "
        "Criar e atualizar você executa direto; ficar pedindo 'confirma?' a cada "
        "passo de uma tarefa simples é ruído.\n"
        "- Se uma chamada falhar, não repita a mesma coisa esperando resultado "
        "diferente: leia o erro, mude o que ele aponta, ou diga o que falta.\n\n"

        "ARMADILHAS CONHECIDAS DO KOMMO (confirmadas na doc oficial em 29/09/2026,\n"
        "não descubra de novo na tentativa e erro):\n"
        "- `status_id` é o campo `id` da etapa (ex: 112332268), NÃO o `sort` nem a "
        "posição dela no funil. Confundir os dois é o erro que mais apareceu nos logs: "
        "mandar sort 30 como status_id faz o Kommo responder um 'Bad request' que não "
        "explica nada. Pegue o `id` em `kommo_listar_funis` ou `kommo_ver_etapa`.\n"
        "- Três etapas vêm com `is_editable: false` e NÃO aceitam edição nem exclusão: "
        "a de leads de entrada (`type: 1`), a 142 (Venda ganha) e a 143 (Venda perdida). "
        "Se o usuário pedir pra mexer nelas, explique que o Kommo não permite.\n"
        "- Parâmetros terminados em `_json` (etapas_json, etapa_json...) são "
        "STRING contendo JSON, não objeto.\n"
        "- Ao atualizar etapa, mande SEMPRE `name` E `sort` juntos, com os valores "
        "atuais, mesmo que não vá mudá-los. Sem `name` o Kommo APAGA o nome; sem "
        "`sort` ele renumera a etapa e ela troca de lugar no funil.\n"
        "- DESCRIÇÃO/dica de etapa: o campo é `descriptions` no PLURAL e é um ARRAY "
        "de objetos. `level` só aceita 'newbie', 'candidate' ou 'master', no máximo "
        "3 por etapa (um por nível), 1000 caracteres cada. Não existe `description` no "
        "singular. Formato: etapa_json = {\"name\": \"Agendamento\", \"sort\": 30, "
        "\"descriptions\": [{\"level\": \"newbie\", \"description\": \"texto\"}]}\n"
        "- Dica é SÓ-ADIÇÃO: dá pra criar a dica de um nível vazio, mas a API do "
        "Kommo NÃO deixa alterar nem apagar uma dica que já existe — recusa mesmo "
        "com texto igual. Se o usuário pedir pra mudar uma dica existente, explique "
        "que isso só pela tela do Kommo, e não fique tentando outros formatos.\n"
        "- `kommo_listar_funis` NÃO traz as descrições das etapas, mesmo quando existem. "
        "Use `kommo_ver_etapa` pra ler uma etapa com as descrições antes de atualizá-la "
        "(assim você reaproveita name/sort/descrições atuais em vez de apagá-los).\n"
        "- COR de etapa: a paleta é fechada. Só estes 21 valores são aceitos — #fffeb2 "
        "#fffd7f #fff000 #ffeab2 #ffdc7f #ffce5a #ffdbdb #ffc8c8 #ff8f92 #d6eaff #c1e0ff "
        "#98cbff #ebffb1 #deff81 #87f2c0 #f9deff #f3beff #ccc8f9 #eb93ff #f2f3f4 #e6e8ea. "
        "Qualquer outro hex é recusado (#800080 e #ffffff, por exemplo). Se o usuário "
        "pedir uma cor fora da lista, ofereça a mais próxima em vez de tentar.\n"
        "- Tags de lead: o PATCH substitui a lista INTEIRA. Leia as tags atuais "
        "e reenvie todas, senão as que faltarem são removidas.\n"
        "- O Kommo às vezes devolve erro tendo gravado assim mesmo. Antes de "
        "tentar de novo, leia o estado atual pra ver se já aplicou.\n\n"

        "Seja direto e breve nas respostas."
    )


def _normalizar_args_kommo(args: dict) -> dict:
    """Os parâmetros `*_json` do MCP do Kommo são declarados como STRING
    contendo JSON (ex: etapas_json='[{"name":"X","sort":20}]'). O modelo tende
    a mandar a lista/objeto já estruturado, e aí a chamada falha com um erro
    genérico que não diz o motivo — foi parte do sufoco pra criar a etapa 'MIA'
    em 25/09/2026. Converter aqui é mais confiável que torcer pro modelo
    acertar o formato."""
    normalizados = {}
    for chave, valor in args.items():
        if chave.endswith("_json") and isinstance(valor, (dict, list)):
            normalizados[chave] = json.dumps(valor, ensure_ascii=False)
        else:
            normalizados[chave] = valor
    return normalizados


FERRAMENTAS_ESTRUTURAIS = ("atualizar_node", "criar_node", "remover_node", "atualizar_connections")


# Etapas que o Kommo marca com is_editable=false e recusa qualquer PATCH/DELETE
# (doc: pipelines-e-estágios-de-leads). 142/143 são iguais em toda conta.
ETAPAS_RESERVADAS = {142, 143}

# Paleta fechada de cores de etapa (doc: cores-de-etapa-disponiveis). Contas
# antigas têm etapas com cores fora desta lista (#ffff99, #99ccff, #c1c1c1...)
# que o Kommo mostra mas RECUSA se você reenviar — reenviar a cor atual de uma
# etapa dessas derruba o PATCH inteiro com "Bad request".
# Os três níveis de dica de etapa. Minúsculas: o Kommo recusa "NEWBIE".
NIVEIS_DICA = {"newbie", "candidate", "master"}

CORES_ETAPA_KOMMO = {
    "#fffeb2", "#fffd7f", "#fff000", "#ffeab2", "#ffdc7f", "#ffce5a", "#ffdbdb",
    "#ffc8c8", "#ff8f92", "#d6eaff", "#c1e0ff", "#98cbff", "#ebffb1", "#deff81",
    "#87f2c0", "#f9deff", "#f3beff", "#ccc8f9", "#eb93ff", "#f2f3f4", "#e6e8ea",
}


def _guarda_etapa_kommo(args: dict, credenciais: dict) -> tuple:
    """Confere o `status_id` ANTES de deixar o PATCH sair.

    Em 29/09/2026 o chat renomeou a etapa "Contato inicial" para "MIA" porque
    chutou o id: leu a etapa, viu que era outra, e gravou por cima assim mesmo.
    Nenhum texto de prompt segurou isso em dois testes seguidos — então a
    checagem vira código.

    Também completa `sort` e `color` quando o modelo omite: o Kommo trata um
    PATCH como substituição, e sem esses campos ele renumera a etapa (ela troca
    de lugar no funil) e reseta a cor pro amarelo padrão.

    Devolve (args_corrigidos, None) pra seguir, ou (None, "erro: ...") pra
    barrar e explicar ao modelo o que fazer.
    """
    try:
        status_id = int(args.get("status_id", 0))
        pipeline_id = int(args.get("pipeline_id", 0))
    except (TypeError, ValueError):
        return None, "erro: pipeline_id e status_id precisam ser numéricos"

    if status_id in ETAPAS_RESERVADAS:
        return None, (
            f"erro: a etapa {status_id} é reservada do Kommo (Venda ganha/perdida) "
            "e não aceita edição. Avise o usuário em vez de tentar de novo."
        )

    bruto = mcp_kommo_client.chamar_ferramenta(
        "kommo_ver_etapa", {"pipeline_id": pipeline_id, "status_id": status_id, **credenciais}
    )
    try:
        atual = json.loads(json.loads(bruto)[0]["data"])
    except (json.JSONDecodeError, KeyError, IndexError, TypeError):
        return None, (
            f"erro: não consegui confirmar que a etapa {status_id} existe no funil "
            f"{pipeline_id}. Chame kommo_listar_funis e use o campo `id` da etapa "
            "(não o `sort`) antes de tentar de novo."
        )

    if atual.get("is_editable") is False or atual.get("type") == 1:
        return None, (
            f"erro: a etapa {status_id} ('{atual.get('name')}') não é editável no Kommo "
            "(é a etapa de leads de entrada ou uma etapa de sistema). Avise o usuário."
        )

    try:
        corpo = json.loads(args.get("etapa_json") or "{}")
    except json.JSONDecodeError:
        return None, "erro: etapa_json não é um JSON válido"
    if not isinstance(corpo, dict):
        return None, "erro: etapa_json precisa ser um objeto, não uma lista"

    nome_pedido, nome_atual = corpo.get("name"), atual.get("name")
    if nome_pedido and nome_pedido != nome_atual and not corpo.pop("confirmar_renomear", False):
        return None, (
            f"erro: a etapa {status_id} se chama '{nome_atual}', não '{nome_pedido}'. "
            "Se era outra etapa que você queria, pegue o `id` certo em kommo_listar_funis. "
            f"Se a intenção era mesmo RENOMEAR '{nome_atual}' para '{nome_pedido}', "
            'reenvie incluindo "confirmar_renomear": true no etapa_json.'
        )
    corpo.pop("confirmar_renomear", None)

    # o PATCH substitui: o que não for reenviado, o Kommo redefine
    # `descriptions` é só-adição: o Kommo aceita criar a dica de um nível que
    # ainda não existe, mas RECUSA qualquer PATCH que mande um nível já
    # preenchido — mesmo com texto idêntico, e mesmo acompanhado de um nível
    # novo (testado em 29/09/2026). Sem esta checagem o modelo levava um
    # "Bad request" mudo e ficava tentando variações de formato.
    pedidas = corpo.get("descriptions")
    if isinstance(pedidas, list) and pedidas:
        pedidos = {d.get("level") for d in pedidas if isinstance(d, dict)}
        invalidos = sorted(n for n in pedidos if n not in NIVEIS_DICA)
        if invalidos:
            return None, (
                f"erro: nível de dica inválido: {', '.join(map(str, invalidos))}. "
                "Só existem newbie, candidate e master, em minúsculas "
                "(o Kommo recusa 'NEWBIE')."
            )

        existentes = atual.get("descriptions") or {}
        niveis_atuais = (
            {d.get("level") for d in existentes.values()}
            if isinstance(existentes, dict)
            else {d.get("level") for d in existentes}
        )
        repetidos = sorted(pedidos & niveis_atuais)
        if repetidos:
            livres = sorted(NIVEIS_DICA - niveis_atuais)
            saida = (
                f"Os três níveis desta etapa já estão preenchidos, então nenhuma dica "
                "nova cabe aqui."
                if not livres
                else f"Níveis ainda livres nesta etapa: {', '.join(livres)}."
            )
            return None, (
                f"erro: a etapa '{nome_atual}' já tem dica no(s) nível(is) "
                f"{', '.join(repetidos)}, e a API do Kommo não permite alterar nem "
                f"remover uma dica existente. {saida} Diga ao usuário que mudar uma "
                "dica já escrita só é possível pela tela do Kommo."
            )

    corpo.setdefault("name", nome_atual)
    corpo.setdefault("sort", atual.get("sort"))
    cor_atual = (atual.get("color") or "").lower()
    if cor_atual in CORES_ETAPA_KOMMO:
        corpo.setdefault("color", cor_atual)
    # cor fora da paleta: não dá pra preservar nem reenviar — o Kommo recusaria.
    # Omitir deixa a etapa cair no #fffeb2 padrão; é o único caminho que grava.

    if corpo.get("color") and str(corpo["color"]).lower() not in CORES_ETAPA_KOMMO:
        return None, (
            f"erro: a cor {corpo['color']} não está na paleta aceita pelo Kommo. "
            "Valores válidos: " + " ".join(sorted(CORES_ETAPA_KOMMO))
        )

    return {**args, "etapa_json": json.dumps(corpo, ensure_ascii=False)}, None


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
            args = _normalizar_args_kommo(args)
            if nome == "kommo_atualizar_etapa":
                args, erro = _guarda_etapa_kommo(args, credenciais)
                if erro:
                    db.registrar_log(cliente_id, "sistema", f"Bloqueado: {erro}")
                    return erro
                db.registrar_log(
                    cliente_id, "sistema",
                    f"Corpo enviado ao Kommo após a verificação: {args['etapa_json']}",
                )
            return mcp_kommo_client.chamar_ferramenta(nome, {**args, **credenciais})
        return f"erro: ferramenta desconhecida '{nome}'"
    except Exception as e:  # noqa: BLE001 — devolve pro modelo como resultado da tool, não derruba o chat
        return f"erro: {e}"


# Quantos turnos do histórico vão pro modelo. Mandar a conversa inteira parece
# generoso, mas em 29/09/2026 foi o que fez o chat errar a etapa do Kommo: a
# conversa carregava dezenas de tentativas frustradas com IDs errados, e o
# modelo repescava aqueles números em vez de consultar o funil. Com o histórico
# cortado ele acerta de primeira (listar_funis -> ver_etapa -> atualizar).
# Também segura o custo por turno, que antes crescia sem teto.
MAX_TURNOS_HISTORICO = 12


def _contexto_conversa(cliente: dict) -> list:
    historico = [
        m for m in db.listar_mensagens(cliente["id"])
        if m["role"] in ("user", "assistant")
    ]
    mensagens = [{"role": "system", "content": _system_prompt(cliente)}]
    recortado = historico[-MAX_TURNOS_HISTORICO:]
    if len(historico) > len(recortado):
        mensagens.append({
            "role": "system",
            "content": (
                f"[{len(historico) - len(recortado)} mensagens mais antigas desta "
                "conversa foram omitidas. Não reaproveite IDs, nomes de etapa ou "
                "resultados citados antes: consulte o estado atual pelas ferramentas.]"
            ),
        })
    mensagens += [{"role": m["role"], "content": m["conteudo"]} for m in recortado]
    return mensagens


def enviar_em_lote(cliente_id: int, texto_usuario: str) -> str:
    """Manda a mensagem pela Batch API em vez de responder na hora: 50% mais
    barato, resposta em minutos a horas. Serve pra pedido de GERAÇÃO (escreva
    o prompt, proponha a estrutura do funil) — sem ferramentas, então não
    executa nada no n8n nem no Kommo. Devolve o batch_id."""
    cliente = db.obter_cliente(cliente_id)
    if cliente is None:
        raise ValueError("cliente não encontrado")
    if cliente["status"] != "clonado":
        raise ValueError("cliente ainda não foi clonado — configure as credenciais primeiro")

    db.salvar_mensagem(cliente_id, "user", texto_usuario)
    mensagens = _contexto_conversa(cliente)
    mensagens.append({
        "role": "system",
        "content": (
            "Este pedido veio em MODO LOTE: você não tem ferramentas disponíveis "
            "nesta resposta. Produza o texto pedido (prompt, proposta, análise). "
            "Se o pedido exigir executar algo no n8n ou no Kommo, diga que isso "
            "precisa ser pedido no modo normal do chat."
        ),
    })

    batch_id = briefing_batch.submeter_conversa(mensagens)
    db.registrar_lote_chat(cliente_id, batch_id, texto_usuario)
    db.registrar_log(cliente_id, "sistema", f"Mensagem enviada pela Batch API (lote {batch_id}).")
    return batch_id


def buscar_respostas_em_lote(cliente_id: int) -> int:
    """Confere os lotes pendentes deste cliente e grava as respostas que já
    ficaram prontas. Devolve quantas chegaram."""
    chegaram = 0
    for lote in db.listar_lotes_chat_pendentes(cliente_id):
        try:
            r = briefing_batch.verificar_conversa(lote["batch_id"])
        except Exception as e:  # noqa: BLE001
            db.registrar_log(cliente_id, "erro", f"Falha ao checar lote {lote['batch_id']}: {e}")
            continue

        if r["status"] == "completed":
            db.salvar_mensagem(cliente_id, "assistant", r["resposta"] or "(lote concluído sem resposta)")
            db.encerrar_lote_chat(lote["id"], "completed")
            db.registrar_log(cliente_id, "sistema", f"Resposta do lote {lote['batch_id']} recebida.")
            chegaram += 1
        elif r["status"] in ("failed", "expired", "cancelled"):
            db.salvar_mensagem(cliente_id, "assistant", f"(o lote não concluiu: {r['status']})")
            db.encerrar_lote_chat(lote["id"], r["status"])
            chegaram += 1
    return chegaram


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
