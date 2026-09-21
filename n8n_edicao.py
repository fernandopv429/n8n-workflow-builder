"""Ferramentas curadas de edição do agente n8n, usadas pelo chat
(agente_chat.py). Conjunto restrito e determinístico — nunca "editar JSON
livre": só os pontos que o chat precisa tocar, cada escrita com verificação
pós-mudança (mesmo padrão de segurança do cloner.py).

Identificação do agente principal: confirmado contra os 4 workflows reais
(fixtures/) que todo cliente tem exatamente 2 nodes
`@n8n/n8n-nodes-langchain.agent` — um sempre chamado "AgenteMov" (fixo), o
outro é a persona do cliente (nome varia: Sabrina, Fabifisio, Bruno...). O
agente principal é sempre o que NÃO se chama "AgenteMov" — nunca inferido
pelo nome da persona.
"""
import json
import pathlib
import re
import sys

RAIZ = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(RAIZ / ".pylibs"))
sys.path.insert(0, str(RAIZ))

from n8n_client import N8nClient  # noqa: E402

NOME_NODE_AGENTE_MOVIMENTACAO = "AgenteMov"
NOME_NODE_DATABASE = "Database"
TIPO_NODE_AGENTE = "@n8n/n8n-nodes-langchain.agent"

# únicos campos do Database que o chat pode reescrever — mesmo conjunto que o
# cloner.py já trata como dado real de cliente (ver manifest.py).
CAMPOS_DATABASE_EDITAVEIS = ("kommo-token", "base-url")


class EdicaoInvalida(RuntimeError):
    """Levantado quando o agente principal não é identificável ou a
    verificação pós-escrita não bate com o que foi mandado salvar."""


def _node_agente_principal(nodes: list) -> dict:
    candidatos = [
        n for n in nodes
        if n.get("type") == TIPO_NODE_AGENTE and n.get("name") != NOME_NODE_AGENTE_MOVIMENTACAO
    ]
    if len(candidatos) != 1:
        raise EdicaoInvalida(
            f"esperava exatamente 1 node de agente principal (tipo {TIPO_NODE_AGENTE}, "
            f"nome != '{NOME_NODE_AGENTE_MOVIMENTACAO}'), achei {len(candidatos)}"
        )
    return candidatos[0]


def _node_database(nodes: list) -> dict:
    node = next((n for n in nodes if n.get("name") == NOME_NODE_DATABASE), None)
    if node is None:
        raise EdicaoInvalida(f"node '{NOME_NODE_DATABASE}' não encontrado no workflow")
    return node


def _payload_atualizacao(workflow: dict, nodes: list) -> dict:
    return {
        "name": workflow["name"],
        "nodes": nodes,
        "connections": workflow["connections"],
        "settings": {"executionOrder": "v1"},
    }


def ler_prompt_agente(workflow_id: str) -> dict:
    cliente = N8nClient()
    wf = cliente.get_workflow(workflow_id)
    node = _node_agente_principal(wf["nodes"])
    prompt = node.get("parameters", {}).get("options", {}).get("systemMessage", "")
    return {"node": node["name"], "prompt": prompt}


def atualizar_prompt_agente(workflow_id: str, novo_prompt: str) -> dict:
    cliente = N8nClient()
    wf = cliente.get_workflow(workflow_id)
    nodes = wf["nodes"]
    node = _node_agente_principal(nodes)
    node.setdefault("parameters", {}).setdefault("options", {})["systemMessage"] = novo_prompt

    cliente.update_workflow(workflow_id, _payload_atualizacao(wf, nodes))

    verificado = ler_prompt_agente(workflow_id)
    if verificado["prompt"] != novo_prompt:
        raise EdicaoInvalida(
            f"prompt do node '{node['name']}' não bateu após salvar — revisar manualmente no n8n"
        )
    return {"workflow_id": workflow_id, "node": node["name"]}


def ler_campo_database(workflow_id: str, campo: str) -> str:
    cliente = N8nClient()
    wf = cliente.get_workflow(workflow_id)
    node = _node_database(wf["nodes"])
    assignments = node.get("parameters", {}).get("assignments", {}).get("assignments", [])
    achado = next((a for a in assignments if a.get("name") == campo), None)
    if achado is None:
        raise EdicaoInvalida(f"campo '{campo}' não existe no node '{NOME_NODE_DATABASE}'")
    return achado.get("value", "")


def atualizar_campo_database(workflow_id: str, campo: str, valor: str) -> dict:
    if campo not in CAMPOS_DATABASE_EDITAVEIS:
        raise EdicaoInvalida(
            f"campo '{campo}' não é editável pelo chat — só {CAMPOS_DATABASE_EDITAVEIS}"
        )
    cliente = N8nClient()
    wf = cliente.get_workflow(workflow_id)
    nodes = wf["nodes"]
    node = _node_database(nodes)
    assignments = node.get("parameters", {}).get("assignments", {}).get("assignments", [])
    achado = next((a for a in assignments if a.get("name") == campo), None)
    if achado is None:
        raise EdicaoInvalida(f"campo '{campo}' não existe no node '{NOME_NODE_DATABASE}'")
    achado["value"] = valor

    cliente.update_workflow(workflow_id, _payload_atualizacao(wf, nodes))

    verificado = ler_campo_database(workflow_id, campo)
    if verificado != valor:
        raise EdicaoInvalida(f"campo '{campo}' não bateu após salvar — revisar manualmente no n8n")
    return {"workflow_id": workflow_id, "campo": campo}


def listar_nodes(workflow_id: str) -> list:
    cliente = N8nClient()
    wf = cliente.get_workflow(workflow_id)
    return [{"nome": n.get("name"), "tipo": n.get("type")} for n in wf["nodes"]]


def _renomear_em_connections(connections: dict, nome_antigo: str, nome_novo: str) -> dict:
    """`connections` referencia node por nome tanto na chave (origem) quanto em
    cada conexão (destino, campo "node") — os dois precisam ser trocados."""
    novo = {}
    for origem, por_tipo_saida in connections.items():
        chave = nome_novo if origem == nome_antigo else origem
        novo_por_tipo = {}
        for tipo_saida, portas in por_tipo_saida.items():
            novas_portas = []
            for porta in portas:
                nova_porta = []
                for conexao in porta:
                    nova_conexao = dict(conexao)
                    if nova_conexao.get("node") == nome_antigo:
                        nova_conexao["node"] = nome_novo
                    nova_porta.append(nova_conexao)
                novas_portas.append(nova_porta)
            novo_por_tipo[tipo_saida] = novas_portas
        novo[chave] = novo_por_tipo
    return novo


def _renomear_em_expressoes(nodes: list, nome_antigo: str, nome_novo: str) -> int:
    """Expressões do n8n referenciam node por nome como `$('NomeDoNode')` —
    melhor esforço via regex nos parâmetros de cada node (nomes muito
    genéricos podem, em teoria, colidir com texto solto; por isso o resultado
    inclui a contagem de trocas, pra revisão)."""
    padrao = re.compile(r"\$\(\s*(['\"])" + re.escape(nome_antigo) + r"\1\s*\)")
    total = 0
    for n in nodes:
        bruto = json.dumps(n.get("parameters", {}), ensure_ascii=False)
        novo_bruto, qtd = padrao.subn(f"$('{nome_novo}')", bruto)
        if qtd:
            n["parameters"] = json.loads(novo_bruto)
            total += qtd
    return total


def renomear_node(workflow_id: str, nome_atual: str, nome_novo: str) -> dict:
    if NOME_NODE_AGENTE_MOVIMENTACAO in (nome_atual, nome_novo):
        raise EdicaoInvalida(
            f"'{NOME_NODE_AGENTE_MOVIMENTACAO}' não pode ser renomeado — o sistema depende desse nome fixo"
        )
    if NOME_NODE_DATABASE in (nome_atual, nome_novo):
        raise EdicaoInvalida(f"'{NOME_NODE_DATABASE}' não pode ser renomeado — o sistema depende desse nome fixo")

    cliente = N8nClient()
    wf = cliente.get_workflow(workflow_id)
    nodes = wf["nodes"]

    alvo = next((n for n in nodes if n.get("name") == nome_atual), None)
    if alvo is None:
        raise EdicaoInvalida(f"node '{nome_atual}' não encontrado")
    if any(n.get("name") == nome_novo for n in nodes):
        raise EdicaoInvalida(f"já existe um node chamado '{nome_novo}'")

    alvo["name"] = nome_novo
    trocas = _renomear_em_expressoes(nodes, nome_atual, nome_novo)
    novas_connections = _renomear_em_connections(wf["connections"], nome_atual, nome_novo)

    payload = {"name": wf["name"], "nodes": nodes, "connections": novas_connections, "settings": {"executionOrder": "v1"}}
    cliente.update_workflow(workflow_id, payload)

    verificado = cliente.get_workflow(workflow_id)
    nomes = [n.get("name") for n in verificado["nodes"]]
    if nome_novo not in nomes or nome_atual in nomes:
        raise EdicaoInvalida("rename não bateu após salvar — revisar manualmente no n8n")

    return {
        "workflow_id": workflow_id,
        "nome_antigo": nome_atual,
        "nome_novo": nome_novo,
        "referencias_de_expressao_trocadas": trocas,
    }


# --- acesso irrestrito (a pedido do Fernando, 18/09/2026) --------------
#
# Diferente de tudo acima, estas funções não têm verificação de conteúdo —
# aceitam qualquer JSON de node/connections que o chamador mandar. A única
# rede de segurança é: (1) AgenteMov/Database continuam protegidos (o próprio
# sistema depende desses nomes fixos pra continuar gerenciando o cliente);
# (2) quem chama (agente_chat.py) é responsável por tirar backup ANTES —
# ver `ler_workflow_completo` + log em db.registrar_log.


def ler_workflow_completo(workflow_id: str) -> dict:
    cliente = N8nClient()
    wf = cliente.get_workflow(workflow_id)
    return {"name": wf["name"], "nodes": wf["nodes"], "connections": wf["connections"]}


def ler_node(workflow_id: str, nome_node: str) -> dict:
    cliente = N8nClient()
    wf = cliente.get_workflow(workflow_id)
    node = next((n for n in wf["nodes"] if n.get("name") == nome_node), None)
    if node is None:
        raise EdicaoInvalida(f"node '{nome_node}' não encontrado")
    return node


def atualizar_node(workflow_id: str, nome_node: str, novo_node: dict) -> dict:
    """Substitui a definição inteira de um node existente (type/parameters/
    tudo) pelo JSON fornecido — sem checagem de conteúdo."""
    if nome_node in (NOME_NODE_AGENTE_MOVIMENTACAO, NOME_NODE_DATABASE):
        raise EdicaoInvalida(f"'{nome_node}' não pode ser editado por essa ferramenta — nome fixo usado pelo sistema")
    cliente = N8nClient()
    wf = cliente.get_workflow(workflow_id)
    nodes = wf["nodes"]
    idx = next((i for i, n in enumerate(nodes) if n.get("name") == nome_node), None)
    if idx is None:
        raise EdicaoInvalida(f"node '{nome_node}' não encontrado")
    novo_node = dict(novo_node)
    novo_node.setdefault("name", nome_node)
    nodes[idx] = novo_node
    cliente.update_workflow(workflow_id, _payload_atualizacao(wf, nodes))
    return {"workflow_id": workflow_id, "node": novo_node["name"]}


def criar_node(workflow_id: str, node: dict) -> dict:
    cliente = N8nClient()
    wf = cliente.get_workflow(workflow_id)
    nodes = wf["nodes"]
    if any(n.get("name") == node.get("name") for n in nodes):
        raise EdicaoInvalida(f"já existe um node chamado '{node.get('name')}'")
    nodes.append(node)
    cliente.update_workflow(workflow_id, _payload_atualizacao(wf, nodes))
    return {"workflow_id": workflow_id, "node": node.get("name")}


def remover_node(workflow_id: str, nome_node: str) -> dict:
    if nome_node in (NOME_NODE_AGENTE_MOVIMENTACAO, NOME_NODE_DATABASE):
        raise EdicaoInvalida(f"'{nome_node}' não pode ser removido — nome fixo usado pelo sistema")
    cliente = N8nClient()
    wf = cliente.get_workflow(workflow_id)
    nodes = [n for n in wf["nodes"] if n.get("name") != nome_node]
    if len(nodes) == len(wf["nodes"]):
        raise EdicaoInvalida(f"node '{nome_node}' não encontrado")

    connections = {k: v for k, v in wf["connections"].items() if k != nome_node}
    for por_tipo_saida in connections.values():
        for portas in por_tipo_saida.values():
            for porta in portas:
                porta[:] = [c for c in porta if c.get("node") != nome_node]

    payload = {"name": wf["name"], "nodes": nodes, "connections": connections, "settings": {"executionOrder": "v1"}}
    cliente.update_workflow(workflow_id, payload)
    return {"workflow_id": workflow_id, "node_removido": nome_node}


def ler_connections(workflow_id: str) -> dict:
    cliente = N8nClient()
    return cliente.get_workflow(workflow_id)["connections"]


def atualizar_connections(workflow_id: str, connections: dict) -> dict:
    cliente = N8nClient()
    wf = cliente.get_workflow(workflow_id)
    payload = {"name": wf["name"], "nodes": wf["nodes"], "connections": connections, "settings": {"executionOrder": "v1"}}
    cliente.update_workflow(workflow_id, payload)
    return {"workflow_id": workflow_id, "connections_atualizadas": True}


def ler_credenciais_kommo(workflow_id: str) -> dict:
    """Pro chat autorizar chamadas ao MCP do Kommo — nunca guardamos o token
    do Kommo no nosso banco, ele mora só no node Database do workflow."""
    subdominio = ler_campo_database(workflow_id, "base-url")
    token = ler_campo_database(workflow_id, "kommo-token")
    return {"kommo_domain": f"{subdominio}.kommo.com", "access_token": token}
