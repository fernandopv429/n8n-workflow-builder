"""Engine determinística de clonagem de agente de IA no n8n.

Corrige, por construção, as armadilhas reais confirmadas ao inspecionar 4
workflows de cliente (ver plano/README): sub-workflow apontando pro cliente
errado, credencial OpenAI esquecida, e depender do nome do workflow (nunca
usado aqui como identificador — o achado da "Dra. Raphaela" que na verdade é
outro cliente).

Evolution API NÃO faz parte do manifesto (verificado 18/09/2026): os nodes que
enviam mensagem usam uma credencial n8n compartilhada entre clientes, e os
campos de mesmo nome no node Database — quando existem — são preenchidos
dinamicamente pelo payload do próprio webhook, não config estática por
cliente. Ver manifest.py.

Limitação conhecida e não resolvida por este engine: o CONTEÚDO interno das
sub-workflows clonadas (`mov_status`, `preenchimento` etc.) pode ter
`field_id`/`status_id` do Kommo hardcoded (ver "Armadilhas ao clonar workflow"
em PADROES-AGENTES-IA-A5ECO.md) — isso não dá pra corrigir genericamente sem
saber a estrutura de campos do Kommo de cada cliente, então o clone só troca a
REFERÊNCIA (workflowId) e avisa para revisão manual do conteúdo copiado.
"""
import copy
import json
import re
import unicodedata

import n8n_edicao
from manifest import ClienteManifest
from n8n_client import N8nClient

TIPOS_SUBWORKFLOW = (
    "n8n-nodes-base.executeWorkflow",
    "@n8n/n8n-nodes-langchain.toolWorkflow",
)
NOME_NODE_DATABASE = "Database"
# Só kommo-token/base-url são dado real de cliente (ver manifest.py — os
# campos de Evolution API foram removidos em 18/09/2026 após verificação
# contra os 4 workflows reais: são credencial n8n compartilhada ou expressão
# dinâmica do webhook, nenhum caso é config estática por cliente).
CAMPOS_DATABASE_DO_MANIFESTO = {
    "kommo-token": "kommo_token",
    "base-url": "kommo_base_url",
}


class ClonagemInvalida(RuntimeError):
    """Levantado quando a verificação pós-clone encontra resíduo do template."""


def _eh_node_openai(node: dict) -> bool:
    return "openai" in node.get("type", "").lower()


def _referencias_subworkflow(nodes: list) -> set:
    ids = set()
    for n in nodes:
        if n.get("type") in TIPOS_SUBWORKFLOW:
            ref = n.get("parameters", {}).get("workflowId", {})
            if isinstance(ref, dict) and ref.get("value"):
                ids.add(ref["value"])
    return ids


def _buscar_subworkflows(cliente: N8nClient, ids_origem: set) -> dict:
    """Só GET — devolve {id_antigo: workflow_json}. Não cria nada no n8n."""
    return {antigo_id: cliente.get_workflow(antigo_id) for antigo_id in ids_origem}


def _criar_copias_subworkflows(cliente: N8nClient, origens: dict, avisos: list) -> dict:
    """Cria de fato uma cópia de cada sub-workflow já buscada. Devolve {id_antigo: novo_workflow}.

    Só chamar no caminho real (dry_run=False) — isto cria workflows no n8n.
    """
    mapa = {}
    for antigo_id, origem in origens.items():
        payload = _payload_criacao(origem)
        novo = cliente.create_workflow(payload)
        mapa[antigo_id] = novo
        avisos.append(
            f"Sub-workflow '{origem.get('name')}' ({antigo_id}) clonada como "
            f"'{novo.get('name')}' ({novo.get('id')}) — CONTEÚDO INTERNO não foi "
            f"revisado (field_id/status_id do Kommo podem estar hardcoded do "
            f"cliente de origem, ver PADROES-AGENTES-IA-A5ECO.md)."
        )
    return mapa


def _reescrever_referencias_subworkflow(nodes: list, mapa_ids: dict):
    for n in nodes:
        if n.get("type") not in TIPOS_SUBWORKFLOW:
            continue
        ref = n.get("parameters", {}).get("workflowId", {})
        antigo_id = ref.get("value") if isinstance(ref, dict) else None
        if antigo_id in mapa_ids:
            novo = mapa_ids[antigo_id]
            ref["value"] = novo["id"]
            ref["cachedResultUrl"] = f"/workflow/{novo['id']}"
            ref["cachedResultName"] = novo.get("name", ref.get("cachedResultName"))


def _reescrever_database(nodes: list, cliente: ClienteManifest, avisos: list):
    node = next((n for n in nodes if n.get("name") == NOME_NODE_DATABASE), None)
    if node is None:
        avisos.append(
            f"Nenhum node '{NOME_NODE_DATABASE}' encontrado — campos do cliente "
            "(kommo-token, base-url, evolution-api-key, server-url) NÃO foram "
            "aplicados. Revisar manualmente."
        )
        return
    assignments = node.get("parameters", {}).get("assignments", {}).get("assignments", [])
    por_nome = {a.get("name"): a for a in assignments}
    faltando = []
    for campo, atributo_manifesto in CAMPOS_DATABASE_DO_MANIFESTO.items():
        valor = getattr(cliente, atributo_manifesto)
        if campo not in por_nome:
            faltando.append(campo)
            continue
        por_nome[campo]["value"] = valor
        por_nome[campo]["type"] = "string"
    if faltando:
        raise ClonagemInvalida(
            f"Node '{NOME_NODE_DATABASE}' não tem os campos {faltando} — "
            "template incompatível com o formato esperado, abortando antes de criar nada."
        )


def _trocar_credencial_openai(nodes: list, cliente: ClienteManifest, avisos: list):
    trocados = 0
    for n in nodes:
        if not _eh_node_openai(n):
            continue
        cred = n.get("credentials", {}).get("openAiApi")
        if cred is None:
            continue
        cred["id"] = cliente.openai_credential_id
        cred["name"] = cliente.openai_credential_name
        trocados += 1
    if trocados == 0:
        avisos.append("Nenhum node OpenAI encontrado no template — nada para trocar de credencial.")


def _slug(texto: str) -> str:
    sem_acento = unicodedata.normalize("NFKD", texto).encode("ascii", "ignore").decode("ascii")
    texto = re.sub(r"[^a-z0-9]+", "-", sem_acento.lower().strip())
    return texto.strip("-") or "cliente"


def _aplicar_prompt(nodes: list, prompt: str, avisos: list):
    """Troca o prompt do agente principal pelo gerado a partir do briefing.

    Sem isto o clone entra no ar com a persona do TEMPLATE: o agente do
    'Will teste' ficou respondendo como "Jaque, da Dra. Fabiana" porque o
    prompt sugerido só era guardado no banco, nunca aplicado (21/09/2026).
    """
    if not prompt:
        avisos.append(
            "Nenhum prompt gerado a partir de briefing — o agente entra com a persona do "
            "template. Ajuste pelo chat antes de apontar o WhatsApp do cliente pra cá."
        )
        return
    try:
        node = n8n_edicao._node_agente_principal(nodes)
    except n8n_edicao.EdicaoInvalida as e:
        avisos.append(f"Não consegui identificar o agente principal pra aplicar o prompt do briefing: {e}")
        return
    node.setdefault("parameters", {}).setdefault("options", {})["systemMessage"] = prompt
    avisos.append(f"Prompt do briefing aplicado no agente '{node['name']}'.")


def _escolher_path_livre(cliente_nome: str, paths_em_uso: set) -> str:
    """Um path derivado só do nome colide sempre que o nome se repete — e
    também quando uma tentativa anterior do MESMO cliente deixou workflow pra
    trás (foi o que aconteceu com o 'Will teste' em 21/09/2026: a 1ª clonagem
    deu certo e as retentativas colidiram contra ela). Por isso a numeração."""
    base = f"ecossistema-ia-{_slug(cliente_nome)}"
    if base not in paths_em_uso:
        return base
    for n in range(2, 100):
        candidato = f"{base}-{n}"
        if candidato not in paths_em_uso:
            return candidato
    raise ClonagemInvalida(f"não achei path livre pra '{base}' — limpe os workflows antigos no n8n")


def _garantir_webhook_unico(nodes: list, manifesto: ClienteManifest, avisos: list, paths_em_uso: set = None):
    """O node webhook vem do template com o MESMO `path` de todos os outros
    clientes clonados dele (e do próprio template, que é um cliente real e
    ativo) — ativar o clone sem trocar o path colide 409 "conflict with one
    of the webhooks" contra quem já estiver usando esse path (confirmado ao
    vivo 18/09/2026, clone de teste colidiu com o path do Fabifisio). Também
    ajusta `webhookId` pro mesmo valor — sem isso a URL de produção dá 404
    mesmo com o workflow ativo (ver memória n8n-workflow-por-api-webhookid)."""
    novo_path = _escolher_path_livre(manifesto.cliente_nome, paths_em_uso or set())
    for n in nodes:
        if n.get("type") == "n8n-nodes-base.webhook":
            antigo = n.get("parameters", {}).get("path")
            n.setdefault("parameters", {})["path"] = novo_path
            n["webhookId"] = novo_path
            if antigo and antigo != novo_path:
                avisos.append(f"Path do webhook trocado de '{antigo}' pra '{novo_path}' (evita colisão na ativação).")


def _payload_criacao(workflow: dict) -> dict:
    """Só os campos que a API de criação aceita — resto (id/versionId/etc.) é rejeitado."""
    return {
        "name": workflow["name"],
        "nodes": workflow["nodes"],
        "connections": workflow["connections"],
        "settings": {"executionOrder": "v1"},
    }


def montar_previa(cliente_n8n: N8nClient, manifesto: ClienteManifest, prompt_briefing: str = "") -> dict:
    """Só leitura (GET) — usado pelo dry-run. NUNCA chama create_workflow.

    As referências de sub-workflow ficam com o id ANTIGO no payload de prévia
    (não existe id novo ainda, porque nada foi criado) — servem só para
    contagem/inspeção, não para reenviar como criação de verdade.
    """
    avisos = []
    template = cliente_n8n.get_workflow(manifesto.workflow_origem_id)
    nodes = copy.deepcopy(template["nodes"])

    ids_subworkflow = _referencias_subworkflow(nodes)
    origens_subworkflow = _buscar_subworkflows(cliente_n8n, ids_subworkflow) if ids_subworkflow else {}
    for antigo_id, origem in origens_subworkflow.items():
        avisos.append(
            f"Sub-workflow '{origem.get('name')}' ({antigo_id}) SERIA clonada "
            "(dry-run — nenhuma chamada de criação foi feita)."
        )

    avisos.append(
        f"Credencial OpenAI '{manifesto.openai_credential_name}' SERIA criada agora "
        "(dry-run — nenhuma chamada de criação foi feita)."
    )
    manifesto.openai_credential_id = "(seria criado)"  # só pra preview — nunca enviado ao n8n
    _reescrever_database(nodes, manifesto, avisos)
    _trocar_credencial_openai(nodes, manifesto, avisos)
    _garantir_webhook_unico(nodes, manifesto, avisos)
    _aplicar_prompt(nodes, prompt_briefing, avisos)

    payload = {
        "name": f"Ecossistema IA - {manifesto.cliente_nome}",
        "nodes": nodes,
        "connections": copy.deepcopy(template["connections"]),
        "settings": {"executionOrder": "v1"},
    }
    return {
        "dry_run": True,
        "payload": payload,
        "avisos": avisos,
        "subworkflows_que_seriam_clonadas": list(origens_subworkflow.keys()),
    }


def _desfazer(cliente_n8n: N8nClient, workflow_id: str, ids_subworkflows: list, credencial_id: str, avisos: list):
    """Apaga o que já tinha sido criado quando a clonagem falha no meio.

    Sem isso, cada tentativa que falha deixa workflow órfão no n8n — e como o
    órfão fica ocupando o path do webhook, a PRÓXIMA tentativa colide 409
    contra ele. Foi exatamente o que aconteceu com o 'Will teste' em
    21/09/2026: 7 órfãos empilhados, todos por retentativa.
    """
    for wid in ([workflow_id] if workflow_id else []) + list(ids_subworkflows):
        try:
            cliente_n8n.deactivate_workflow(wid)
        except Exception:  # noqa: BLE001 — já pode estar inativo
            pass
        try:
            cliente_n8n.delete_workflow(wid)
        except Exception as e:  # noqa: BLE001
            avisos.append(f"Não consegui remover o workflow {wid} na limpeza: {e}")
    if credencial_id:
        try:
            cliente_n8n.delete_credencial(credencial_id)
        except Exception as e:  # noqa: BLE001
            avisos.append(f"Não consegui remover a credencial {credencial_id} na limpeza: {e}")


def executar_clone_real(cliente_n8n: N8nClient, manifesto: ClienteManifest, prompt_briefing: str = "") -> dict:
    """Cria de fato: credencial OpenAI, sub-workflows, workflow principal, ativa e verifica.

    Tudo ou nada: se qualquer passo falhar, `_desfazer` apaga o que já foi
    criado antes de propagar o erro.
    """
    avisos = []
    credencial_id = ""
    workflow_id = ""
    ids_subworkflows_criadas = []

    try:
        # O schema da credencial openAiApi tem if/then condicional em cima de
        # "header"/"allowedHttpRequestDomains" — se essas chaves vierem AUSENTES
        # (não só false/vazias), o "if" casa por vacuidade e o n8n passa a EXIGIR
        # headerName/headerValue/allowedDomains (erro 400 visto em teste real,
        # 18/09/2026). Mandar os dois campos explícitos evita cair nesse ramo.
        credencial = cliente_n8n.criar_credencial(
            manifesto.openai_credential_name,
            "openAiApi",
            {
                "apiKey": manifesto.openai_api_key,
                "header": False,
                "allowedHttpRequestDomains": "all",
            },
        )
        credencial_id = credencial["id"]
        manifesto.openai_credential_id = credencial_id
        avisos.append(f"Credencial OpenAI '{manifesto.openai_credential_name}' criada (id {credencial_id}).")

        # Lido ANTES de criar qualquer coisa: precisa refletir o estado do n8n
        # sem os workflows desta clonagem.
        paths_em_uso = cliente_n8n.paths_de_webhook_em_uso()

        template = cliente_n8n.get_workflow(manifesto.workflow_origem_id)
        nodes = copy.deepcopy(template["nodes"])

        ids_subworkflow = _referencias_subworkflow(nodes)
        mapa_subworkflows = {}
        if ids_subworkflow:
            origens_subworkflow = _buscar_subworkflows(cliente_n8n, ids_subworkflow)
            mapa_subworkflows = _criar_copias_subworkflows(cliente_n8n, origens_subworkflow, avisos)
            ids_subworkflows_criadas = [v["id"] for v in mapa_subworkflows.values()]
            _reescrever_referencias_subworkflow(nodes, mapa_subworkflows)
            # O n8n recusa ativar o workflow principal se uma sub-workflow que ele
            # chama via executeWorkflow/toolWorkflow não estiver "publicada"
            # (= ativa) — erro real visto ao vivo 18/09/2026: "which is not
            # published. Please publish all referenced sub-workflows first."
            for novo in mapa_subworkflows.values():
                cliente_n8n.activate_workflow(novo["id"])

        _reescrever_database(nodes, manifesto, avisos)
        _trocar_credencial_openai(nodes, manifesto, avisos)
        _garantir_webhook_unico(nodes, manifesto, avisos, paths_em_uso)
        _aplicar_prompt(nodes, prompt_briefing, avisos)

        payload = {
            "name": f"Ecossistema IA - {manifesto.cliente_nome}",
            "nodes": nodes,
            "connections": copy.deepcopy(template["connections"]),
            "settings": {"executionOrder": "v1"},
        }

        ids_antigos = set(mapa_subworkflows.keys()) | {manifesto.workflow_origem_id}
        criado = cliente_n8n.create_workflow(payload)
        workflow_id = criado["id"]

        # `create_workflow` já manda `settings` simplificado desde a criação —
        # não precisa de um PUT extra aqui. Um PUT só com {"settings": ...}
        # (sem name/nodes/connections) dá 400 "must have required property
        # 'name'": o PUT de workflow do n8n é substituição total, não patch
        # parcial (confirmado 18/09/2026, testado ao vivo).
        cliente_n8n.deactivate_workflow(workflow_id)
        cliente_n8n.activate_workflow(workflow_id)
        _verificar_clone(cliente_n8n, workflow_id, manifesto, ids_antigos)
    except Exception:
        _desfazer(cliente_n8n, workflow_id, ids_subworkflows_criadas, credencial_id, avisos)
        raise

    return {
        "dry_run": False,
        "workflow_id": workflow_id,
        "workflow_url": f"{cliente_n8n.base_url}/workflow/{workflow_id}",
        "subworkflows_criadas": {k: v["id"] for k, v in mapa_subworkflows.items()},
        "avisos": avisos,
    }


def _verificar_clone(cliente_n8n: N8nClient, workflow_id: str, manifesto: ClienteManifest, ids_antigos: set):
    """Revarre o workflow criado. Levanta ClonagemInvalida se sobrou resíduo do template."""
    criado = cliente_n8n.get_workflow(workflow_id)
    problemas = []

    for n in criado["nodes"]:
        if n.get("type") in TIPOS_SUBWORKFLOW:
            ref = n.get("parameters", {}).get("workflowId", {})
            if ref.get("value") in ids_antigos:
                problemas.append(
                    f"Node '{n.get('name')}' ainda aponta para sub-workflow antiga {ref.get('value')}"
                )
        if _eh_node_openai(n):
            cred = n.get("credentials", {}).get("openAiApi", {})
            if cred and cred.get("id") != manifesto.openai_credential_id:
                problemas.append(
                    f"Node '{n.get('name')}' com credential OpenAI errada: {cred.get('name')}"
                )

    node_db = next((n for n in criado["nodes"] if n.get("name") == NOME_NODE_DATABASE), None)
    if node_db:
        por_nome = {
            a.get("name"): a.get("value")
            for a in node_db.get("parameters", {}).get("assignments", {}).get("assignments", [])
        }
        for campo, atributo in CAMPOS_DATABASE_DO_MANIFESTO.items():
            esperado = getattr(manifesto, atributo)
            if por_nome.get(campo) != esperado:
                problemas.append(f"Campo '{campo}' do node Database não bate com o manifesto")

    if problemas:
        raise ClonagemInvalida(
            "Verificação pós-clone falhou, workflow " + workflow_id + " desativado:\n- "
            + "\n- ".join(problemas)
        )


def clonar(manifesto: ClienteManifest, dry_run: bool = True, prompt_briefing: str = "") -> dict:
    """Ponto de entrada único. dry_run=True (padrão) NUNCA chama create_workflow —
    só leitura. dry_run=False executa a clonagem de verdade."""
    manifesto.validar()
    cliente_n8n = N8nClient()
    if dry_run:
        return montar_previa(cliente_n8n, manifesto, prompt_briefing)
    return executar_clone_real(cliente_n8n, manifesto, prompt_briefing)


if __name__ == "__main__":
    import sys

    if len(sys.argv) != 2:
        print("uso: python3 cloner.py <manifesto.json>")
        raise SystemExit(1)
    dados = json.load(open(sys.argv[1]))
    m = ClienteManifest.de_dict(dados)
    resultado = clonar(m, dry_run=True)
    print(json.dumps(resultado, indent=2, ensure_ascii=False)[:4000])
