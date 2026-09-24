"""Testes do cloner.py contra os 4 workflows reais salvos em fixtures/ — sem
tocar no n8n. Rodar: python3 testes.py

Cada teste cita o achado real da investigação (ver plano) que ele confirma.
"""
import copy
import json
import pathlib

from cloner import (
    _aplicar_prompt,
    _garantir_webhook_unico,
    _reescrever_database,
    _reescrever_referencias_subworkflow,
    _referencias_subworkflow,
    _trocar_credencial_openai,
)
from manifest import ClienteManifest
from n8n_edicao import (
    EdicaoInvalida,
    _node_agente_principal,
    _node_database,
    _renomear_em_connections,
    _renomear_em_expressoes,
)

RAIZ = pathlib.Path(__file__).resolve().parent
FIXTURES = RAIZ / "fixtures"

PREMIUM_FILM_ID = "pb1oEYADOW0J90iR"
FABIFISIO_SUBWORKFLOW_ID = "dZaYneEAx7iLi3mt"
FABIFISIO_MENTORIA_SUBWORKFLOW_ID = "52PVTrRagOiBnAST"


def carregar(nome_arquivo: str) -> dict:
    return json.loads((FIXTURES / nome_arquivo).read_text())


def manifesto_exemplo(**overrides) -> ClienteManifest:
    """openai_credential_id não é mais um campo de entrada (a credencial é
    criada de verdade por executar_clone_real) — aqui simula o estado "já
    criada" que _trocar_credencial_openai espera ao ser chamado isoladamente."""
    base = dict(
        cliente_nome="Cliente Teste",
        nicho="clinica",
        kommo_subdominio="clienteteste",
        kommo_token="TOKEN-KOMMO-NOVO",
        openai_api_key="sk-teste-nao-usada-nestes-testes",
        workflow_origem_id="ignorado-neste-teste",
    )
    base.update(overrides)
    m = ClienteManifest(**base)
    m.openai_credential_id = "CRED-NOVA-ID"
    return m


def teste_detecta_subworkflow_errada_sabrina():
    """Armadilha #1: Dr. Marcos/Sabrina aponta 100% para a sub-workflow da Premium Film."""
    d = carregar("sabrina_dr_marcos__9oBmsomnh4xwuPE4.json")
    ids = _referencias_subworkflow(d["nodes"])
    assert ids == {PREMIUM_FILM_ID}, f"esperava só {PREMIUM_FILM_ID}, achou {ids}"


def teste_fabifisio_nao_mentoria_esta_correto():
    d = carregar("fabifisio__Jv3y1QjnT84AHywf.json")
    ids = _referencias_subworkflow(d["nodes"])
    assert ids == {FABIFISIO_SUBWORKFLOW_ID}, f"esperava só a própria sub-workflow, achou {ids}"
    assert PREMIUM_FILM_ID not in ids


def teste_detecta_correcao_parcial_fabifisio_mentoria():
    """Armadilha #1 (parcial): mov_status/preenchimento corrigidos, CLIENTE ENCAMINHADO esquecido."""
    d = carregar("fabifisio_mentoria__wYSk5zDaCZbQbcIn.json")
    ids = _referencias_subworkflow(d["nodes"])
    assert FABIFISIO_MENTORIA_SUBWORKFLOW_ID in ids, "esperava a sub-workflow própria também referenciada"
    assert PREMIUM_FILM_ID in ids, (
        "esperava PEGAR o resíduo da Premium Film esquecido no node 'CLIENTE ENCAMINHADO' "
        "— se não achou, a detecção regrediu"
    )


def teste_reescreve_todas_referencias_subworkflow():
    d = carregar("sabrina_dr_marcos__9oBmsomnh4xwuPE4.json")
    nodes = copy.deepcopy(d["nodes"])
    mapa = {PREMIUM_FILM_ID: {"id": "NOVO-ID-123", "name": "mov_status cliente teste"}}
    _reescrever_referencias_subworkflow(nodes, mapa)
    restantes = _referencias_subworkflow(nodes)
    assert restantes == {"NOVO-ID-123"}, f"sobrou referência antiga: {restantes}"


def teste_troca_credencial_openai_sem_excecao():
    """Armadilha #2: 'OpenAI Chat Model1' ficava com credential da Premium Film — aqui TODOS têm que trocar."""
    d = carregar("sabrina_dr_marcos__9oBmsomnh4xwuPE4.json")
    nodes = copy.deepcopy(d["nodes"])
    nodes_openai_antes = [n for n in nodes if "openai" in n.get("type", "").lower() and n.get("credentials")]
    assert len(nodes_openai_antes) >= 2, "fixture não tem nodes OpenAI suficientes pra testar isso"
    # confirma o bug real antes da correção
    credenciais_antes = {n["name"]: n["credentials"]["openAiApi"]["name"] for n in nodes_openai_antes}
    assert credenciais_antes.get("OpenAI Chat Model1") == "Premium Film Key OpenIA", (
        "fixture mudou — o bug que este teste verifica não está mais presente nela"
    )

    m = manifesto_exemplo()
    avisos = []
    _trocar_credencial_openai(nodes, m, avisos)

    for n in nodes:
        if "openai" not in n.get("type", "").lower():
            continue
        cred = n.get("credentials", {}).get("openAiApi")
        if cred is None:
            continue
        assert cred["id"] == m.openai_credential_id, f"node '{n['name']}' não foi trocado"
        assert cred["name"] == m.openai_credential_name


def teste_reescreve_database_com_manifesto():
    d = carregar("sabrina_dr_marcos__9oBmsomnh4xwuPE4.json")
    nodes = copy.deepcopy(d["nodes"])
    m = manifesto_exemplo()
    avisos = []
    _reescrever_database(nodes, m, avisos)

    node_db = next(n for n in nodes if n["name"] == "Database")
    por_nome = {
        a["name"]: a["value"]
        for a in node_db["parameters"]["assignments"]["assignments"]
    }
    assert por_nome["kommo-token"] == m.kommo_token
    assert por_nome["base-url"] == m.kommo_base_url


def teste_database_sem_campos_evolution_nao_bloqueia():
    """Verificado 18/09/2026: o clone 'Bruno/Premium Film' não tem evolution-api-key/
    server-url no Database, mas nada no workflow lê esses campos depois — não é uma
    armadilha real, e clonar a partir desse template não deve ser bloqueado por isso."""
    d = carregar("bruno_premiumfilm__ivYNWtpgz4QfzvHh.json")
    nodes = copy.deepcopy(d["nodes"])
    m = manifesto_exemplo()
    _reescrever_database(nodes, m, [])  # não deve levantar


def teste_garante_webhook_unico():
    """Armadilha real (18/09/2026, achada ao vivo): o path do webhook vem
    igual ao template em todo clone — ativar sem trocar colide 409 contra
    quem já usa esse path (o próprio template, cliente real e ativo)."""
    d = carregar("sabrina_dr_marcos__9oBmsomnh4xwuPE4.json")
    nodes = copy.deepcopy(d["nodes"])
    node_webhook = next(n for n in nodes if n.get("type") == "n8n-nodes-base.webhook")
    path_do_template = node_webhook["parameters"]["path"]

    m = manifesto_exemplo(cliente_nome="Clínica Nova XYZ")
    avisos = []
    _garantir_webhook_unico(nodes, m, avisos)

    node_webhook = next(n for n in nodes if n.get("type") == "n8n-nodes-base.webhook")
    novo_path = node_webhook["parameters"]["path"]
    assert novo_path != path_do_template, "path do webhook não pode continuar igual ao do template"
    assert novo_path == "ecossistema-ia-clinica-nova-xyz"
    assert node_webhook.get("webhookId") == novo_path


def teste_path_desvia_de_path_ja_em_uso():
    """Armadilha real (21/09/2026): a 1ª clonagem do 'Will teste' deu certo e
    ficou ativa; as retentativas geravam o MESMO path e colidiam 409 contra
    ela — 7 workflows órfãos empilhados. O path tem que desviar do que já
    existe na instância, não só do path do template."""
    d = carregar("sabrina_dr_marcos__9oBmsomnh4xwuPE4.json")
    nodes = copy.deepcopy(d["nodes"])
    m = manifesto_exemplo(cliente_nome="Will teste")

    em_uso = {"ecossistema-ia-will-teste"}  # a clonagem anterior, ativa
    _garantir_webhook_unico(nodes, m, [], em_uso)

    node_webhook = next(n for n in nodes if n.get("type") == "n8n-nodes-base.webhook")
    assert node_webhook["parameters"]["path"] == "ecossistema-ia-will-teste-2"
    assert node_webhook["webhookId"] == "ecossistema-ia-will-teste-2"


def teste_aplica_prompt_do_briefing_no_agente():
    """Armadilha real (21/09/2026): o prompt gerado pelo briefing era salvo no
    banco mas nunca aplicado — o agente do 'Will teste' subiu respondendo como
    "Jaque, da Dra. Fabiana", a persona do template."""
    d = carregar("fabifisio__Jv3y1QjnT84AHywf.json")
    nodes = copy.deepcopy(d["nodes"])
    agente = _node_agente_principal(nodes)
    prompt_do_template = agente["parameters"]["options"]["systemMessage"]
    assert "Jaque" in prompt_do_template, "fixture mudou — esperava a persona do template aqui"

    _aplicar_prompt(nodes, "Você é Bia, atendente da Clínica Nova.", [])

    agente = _node_agente_principal(nodes)
    aplicado = agente["parameters"]["options"]["systemMessage"]
    assert aplicado == "Você é Bia, atendente da Clínica Nova."
    assert "Jaque" not in aplicado, "sobrou a persona do template"


def teste_sem_briefing_avisa_que_ficou_com_persona_do_template():
    d = carregar("fabifisio__Jv3y1QjnT84AHywf.json")
    nodes = copy.deepcopy(d["nodes"])
    avisos = []
    _aplicar_prompt(nodes, "", avisos)
    assert any("persona do template" in a for a in avisos), avisos


def teste_identifica_agente_principal_por_nao_ser_agentemov():
    """n8n_edicao: o agente principal é sempre o node tipo agent que NÃO se
    chama 'AgenteMov' — testado contra os 4 clientes reais, personas diferentes
    (Sabrina/Fabifisio/"Fabifisio Men"/Bruno), nunca inferido pelo nome."""
    nomes_esperados = {
        "sabrina_dr_marcos__9oBmsomnh4xwuPE4.json": "Sabrina",
        "fabifisio__Jv3y1QjnT84AHywf.json": "Fabifisio",
        "fabifisio_mentoria__wYSk5zDaCZbQbcIn.json": "Fabifisio Men",
        "bruno_premiumfilm__ivYNWtpgz4QfzvHh.json": "Bruno",
    }
    for arquivo, esperado in nomes_esperados.items():
        d = carregar(arquivo)
        agente = _node_agente_principal(d["nodes"])
        assert agente["name"] == esperado, f"{arquivo}: esperava '{esperado}', achou '{agente['name']}'"
        assert agente["name"] != "AgenteMov"


def teste_acha_node_database_nos_4_clientes():
    for arquivo in (
        "sabrina_dr_marcos__9oBmsomnh4xwuPE4.json",
        "fabifisio__Jv3y1QjnT84AHywf.json",
        "fabifisio_mentoria__wYSk5zDaCZbQbcIn.json",
        "bruno_premiumfilm__ivYNWtpgz4QfzvHh.json",
    ):
        d = carregar(arquivo)
        node = _node_database(d["nodes"])
        assert node["name"] == "Database"


def teste_renomear_node_atualiza_expressoes():
    """renomear_node (n8n_edicao) precisa trocar $('NomeAntigo') em TODO node
    que referencia o alvo — testado contra um node real (Fabifisio) que é lido
    por expressão em pelo menos 3 lugares diferentes."""
    d = carregar("fabifisio__Jv3y1QjnT84AHywf.json")
    nodes = copy.deepcopy(d["nodes"])
    alvo = "RETRIEVE LAST MESSAGES"

    ocorrencias_antes = json.dumps(nodes).count(f"$('{alvo}')")
    assert ocorrencias_antes >= 1, "fixture mudou — este teste depende de expressão real referenciando o node"

    trocas = _renomear_em_expressoes(nodes, alvo, "NOVO NOME")
    assert trocas == ocorrencias_antes
    texto_depois = json.dumps(nodes)
    assert f"$('{alvo}')" not in texto_depois
    assert texto_depois.count("$('NOVO NOME')") == ocorrencias_antes


def teste_renomear_node_atualiza_connections():
    d = carregar("fabifisio__Jv3y1QjnT84AHywf.json")
    connections = copy.deepcopy(d["connections"])
    alvo = "Webhook"
    assert alvo in connections, "fixture mudou — este teste depende do node 'Webhook' ter conexão de saída"

    novas = _renomear_em_connections(connections, alvo, "Webhook Renomeado")
    assert alvo not in novas
    assert "Webhook Renomeado" in novas
    assert f'"node": "{alvo}"' not in json.dumps(novas)


def teste_nao_deixa_renomear_nodes_protegidos():
    """AgenteMov/Database são nomes fixos dos quais o resto do sistema depende
    (_node_agente_principal, _node_database, ler_credenciais_kommo) — a
    checagem de proteção roda ANTES de qualquer chamada ao n8n."""
    import n8n_edicao

    for protegido in ("AgenteMov", "Database"):
        try:
            n8n_edicao.renomear_node("qualquer-id-nunca-chamado", protegido, "Outro Nome")
        except EdicaoInvalida:
            pass
        else:
            raise AssertionError(f"deveria ter recusado renomear '{protegido}'")
        try:
            n8n_edicao.renomear_node("qualquer-id-nunca-chamado", "Outro Node", protegido)
        except EdicaoInvalida:
            pass
        else:
            raise AssertionError(f"deveria ter recusado renomear PARA '{protegido}'")


TESTES = [
    teste_detecta_subworkflow_errada_sabrina,
    teste_fabifisio_nao_mentoria_esta_correto,
    teste_detecta_correcao_parcial_fabifisio_mentoria,
    teste_reescreve_todas_referencias_subworkflow,
    teste_troca_credencial_openai_sem_excecao,
    teste_reescreve_database_com_manifesto,
    teste_database_sem_campos_evolution_nao_bloqueia,
    teste_garante_webhook_unico,
    teste_path_desvia_de_path_ja_em_uso,
    teste_aplica_prompt_do_briefing_no_agente,
    teste_sem_briefing_avisa_que_ficou_com_persona_do_template,
    teste_identifica_agente_principal_por_nao_ser_agentemov,
    teste_acha_node_database_nos_4_clientes,
    teste_renomear_node_atualiza_expressoes,
    teste_renomear_node_atualiza_connections,
    teste_nao_deixa_renomear_nodes_protegidos,
]


if __name__ == "__main__":
    falhas = 0
    for teste in TESTES:
        try:
            teste()
            print(f"OK   {teste.__name__}")
        except Exception as e:
            falhas += 1
            print(f"FAIL {teste.__name__}: {e}")
    print(f"\n{len(TESTES) - falhas}/{len(TESTES)} passaram")
    raise SystemExit(1 if falhas else 0)
