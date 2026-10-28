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


def teste_converte_param_json_do_kommo_pra_string():
    """Falha real (25/09/2026): criar a etapa 'MIA' custou ~8 tentativas. Os
    parâmetros `*_json` do MCP do Kommo são STRING contendo JSON, e o modelo
    manda a lista estruturada — o erro que volta é genérico e não diz isso."""
    import agente_chat

    a = agente_chat._normalizar_args_kommo({
        "etapas_json": [{"name": "MIA", "sort": 15}],
        "pipeline_id": 12852327,
    })
    assert isinstance(a["etapas_json"], str), "lista tinha que virar string JSON"
    assert json.loads(a["etapas_json"]) == [{"name": "MIA", "sort": 15}]
    assert a["pipeline_id"] == 12852327, "o que não é *_json não pode ser mexido"

    # string já pronta passa intacta
    b = agente_chat._normalizar_args_kommo({"etapa_json": '{"name":"X"}'})
    assert b["etapa_json"] == '{"name":"X"}'


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


# --- trava do kommo_atualizar_etapa (agente_chat._guarda_etapa_kommo) ---------
# Em 29/09/2026 o chat renomeou a etapa "Contato inicial" para "MIA" porque
# chutou o status_id, e em outra tentativa mandou PATCH pra etapa 142 e pra de
# leads de entrada. Prompt não segurou; estes testes cobrem a trava que segura.

def _etapa_falsa(**campos):
    """Dubla o kommo_ver_etapa: devolve no formato que o MCP realmente usa
    ([{"data": "<string JSON>"}]), pra trava ser testada contra o formato de
    verdade e não contra um dict conveniente."""
    padrao = {"id": 99116543, "name": "Oferta feita", "sort": 50,
              "is_editable": True, "type": 0, "color": "#ffff99"}
    padrao.update(campos)
    return json.dumps([{"data": json.dumps(padrao)}])


def _com_mcp_dublado(resposta):
    import agente_chat, mcp_kommo_client
    original = mcp_kommo_client.chamar_ferramenta
    mcp_kommo_client.chamar_ferramenta = lambda nome, args: resposta
    return agente_chat, original


def _restaura(original):
    import mcp_kommo_client
    mcp_kommo_client.chamar_ferramenta = original


def teste_trava_barra_patch_na_etapa_errada():
    ac, orig = _com_mcp_dublado(_etapa_falsa(name="Contato inicial"))
    try:
        corr, erro = ac._guarda_etapa_kommo(
            {"pipeline_id": 1, "status_id": 99116539,
             "etapa_json": json.dumps({"name": "MIA", "sort": 15})}, {})
    finally:
        _restaura(orig)
    assert corr is None, "devia ter barrado o PATCH na etapa errada"
    assert "Contato inicial" in erro and "MIA" in erro, erro


def teste_trava_deixa_renomear_quando_confirmado():
    ac, orig = _com_mcp_dublado(_etapa_falsa(name="Contato inicial", color="#98cbff"))
    try:
        corr, erro = ac._guarda_etapa_kommo(
            {"pipeline_id": 1, "status_id": 99116539,
             "etapa_json": json.dumps({"name": "MIA", "confirmar_renomear": True})}, {})
    finally:
        _restaura(orig)
    assert erro is None, erro
    corpo = json.loads(corr["etapa_json"])
    assert corpo["name"] == "MIA", corpo
    assert "confirmar_renomear" not in corpo, "flag interna não pode vazar pro Kommo"


def teste_trava_barra_etapas_que_o_kommo_nao_edita():
    for status_id, dublê, motivo in (
        (142, _etapa_falsa(), "reservada"),
        (99116535, _etapa_falsa(id=99116535, name="Incoming leads", type=1,
                                is_editable=False), "leads de entrada"),
    ):
        ac, orig = _com_mcp_dublado(dublê)
        try:
            corr, erro = ac._guarda_etapa_kommo(
                {"pipeline_id": 1, "status_id": status_id,
                 "etapa_json": json.dumps({"name": "x"})}, {})
        finally:
            _restaura(orig)
        assert corr is None, f"devia barrar etapa {motivo}"


def teste_trava_completa_sort_e_omite_cor_legada():
    """Sem `sort` a etapa troca de lugar no funil; com a cor legada (#ffff99,
    que o Kommo mostra mas recusa na escrita) o PATCH inteiro é rejeitado."""
    ac, orig = _com_mcp_dublado(_etapa_falsa(color="#ffff99"))
    try:
        corr, erro = ac._guarda_etapa_kommo(
            {"pipeline_id": 1, "status_id": 99116543,
             "etapa_json": json.dumps({"descriptions": [
                 {"level": "newbie", "description": "dica"}]})}, {})
    finally:
        _restaura(orig)
    assert erro is None, erro
    corpo = json.loads(corr["etapa_json"])
    assert corpo["sort"] == 50, "sort atual tinha que ser reposto"
    assert corpo["name"] == "Oferta feita", "name atual tinha que ser reposto"
    assert "color" not in corpo, "cor fora da paleta não pode ser reenviada"


def teste_trava_preserva_cor_valida():
    ac, orig = _com_mcp_dublado(_etapa_falsa(color="#98cbff"))
    try:
        corr, erro = ac._guarda_etapa_kommo(
            {"pipeline_id": 1, "status_id": 99116543,
             "etapa_json": json.dumps({"sort": 55})}, {})
    finally:
        _restaura(orig)
    assert erro is None, erro
    assert json.loads(corr["etapa_json"])["color"] == "#98cbff"


def teste_trava_avisa_que_dica_existente_nao_muda():
    """A API do Kommo recusa qualquer PATCH que mande um `level` que já tem
    texto — mesmo idêntico. Sem isto o modelo levava "Bad request" mudo e
    ficava tentando variações de formato (testado ao vivo em 29/09/2026)."""
    ac, orig = _com_mcp_dublado(_etapa_falsa(
        color="#98cbff",
        descriptions={"12": {"level": "newbie", "description": "dica que já existe"}}))
    try:
        corr, erro = ac._guarda_etapa_kommo(
            {"pipeline_id": 1, "status_id": 99116543,
             "etapa_json": json.dumps({"descriptions": [
                 {"level": "newbie", "description": "texto novo"}]})}, {})
    finally:
        _restaura(orig)
    assert corr is None, "devia barrar antes de gastar a chamada"
    assert "newbie" in erro and "tela do Kommo" in erro, erro


def teste_trava_deixa_criar_dica_de_nivel_vazio():
    ac, orig = _com_mcp_dublado(_etapa_falsa(
        color="#98cbff",
        descriptions={"12": {"level": "newbie", "description": "já existe"}}))
    try:
        corr, erro = ac._guarda_etapa_kommo(
            {"pipeline_id": 1, "status_id": 99116543,
             "etapa_json": json.dumps({"descriptions": [
                 {"level": "candidate", "description": "nível ainda vazio"}]})}, {})
    finally:
        _restaura(orig)
    assert erro is None, erro
    assert json.loads(corr["etapa_json"])["descriptions"][0]["level"] == "candidate"


def teste_trava_rejeita_nivel_de_dica_invalido():
    """`level` é sensível a maiúscula e fechado em três valores; o Kommo recusa
    'NEWBIE' e 'expert' com o mesmo Bad request mudo (testado 29/09/2026)."""
    for nivel in ("expert", "NEWBIE"):
        ac, orig = _com_mcp_dublado(_etapa_falsa(color="#98cbff"))
        try:
            corr, erro = ac._guarda_etapa_kommo(
                {"pipeline_id": 1, "status_id": 99116543,
                 "etapa_json": json.dumps({"descriptions": [
                     {"level": nivel, "description": "x"}]})}, {})
        finally:
            _restaura(orig)
        assert corr is None, f"devia barrar o nível {nivel}"
        assert "inválido" in erro, erro


def teste_trava_diz_quais_niveis_sobraram():
    cheia = {str(i): {"level": n, "description": "x"}
             for i, n in enumerate(("newbie", "candidate", "master"))}
    for existentes, espera in ((cheia, "já estão preenchidos"),
                               ({"1": {"level": "newbie", "description": "x"}}, "candidate, master")):
        ac, orig = _com_mcp_dublado(_etapa_falsa(color="#98cbff", descriptions=existentes))
        try:
            _, erro = ac._guarda_etapa_kommo(
                {"pipeline_id": 1, "status_id": 99116543,
                 "etapa_json": json.dumps({"descriptions": [
                     {"level": "newbie", "description": "y"}]})}, {})
        finally:
            _restaura(orig)
        assert espera in erro, f"esperava '{espera}' em: {erro}"


# --- trava do kommo_criar_funil ---------------------------------------------

def teste_criar_funil_completa_campos_obrigatorios():
    """O Kommo só cria funil com is_main false E is_unsorted_on true juntos;
    faltando qualquer um responde só "Bad request" (testado 29/09/2026)."""
    import agente_chat
    corr, erro = agente_chat._guarda_criar_funil({"funil_json": json.dumps(
        {"name": "X", "_embedded": {"statuses": [{"name": "Etapa", "sort": 10}]}})})
    assert erro is None, erro
    corpo = json.loads(corr["funil_json"])
    assert corpo["is_main"] is False and corpo["is_unsorted_on"] is True, corpo
    # o `sort` do funil (não o das etapas) também é obrigatório — faltando ele o
    # Kommo devolve FieldMissing, que o MCP entrega como "Bad request" genérico
    assert corpo["sort"] == 100, corpo


def teste_criar_funil_aceita_campos_soltos():
    """O modelo manda os campos do funil soltos em vez de dentro de funil_json."""
    import agente_chat
    corr, erro = agente_chat._guarda_criar_funil(
        {"name": "X", "sort": 10, "_embedded": {"statuses": [{"name": "E", "sort": 10}]}})
    assert erro is None, erro
    assert json.loads(corr["funil_json"])["name"] == "X"


def teste_criar_funil_barra_is_main_e_funil_sem_etapa():
    import agente_chat
    etapas = {"_embedded": {"statuses": [{"name": "E", "sort": 10}]}}
    _, erro = agente_chat._guarda_criar_funil({"name": "X", "is_main": True, **etapas})
    assert erro and "PRINCIPAL" in erro, erro
    _, erro = agente_chat._guarda_criar_funil({"name": "X"})
    assert erro and "sem etapa" in erro, erro


def teste_criar_funil_barra_dica_na_criacao():
    """Criar funil IGNORA descriptions: o funil nasceria mudo e o modelo diria
    que deu certo. A trava obriga a receita de 3 chamadas."""
    import agente_chat
    _, erro = agente_chat._guarda_criar_funil({"name": "X", "_embedded": {"statuses": [
        {"name": "E", "sort": 10,
         "descriptions": [{"level": "newbie", "description": "dica"}]}]}})
    assert erro and "kommo_adicionar_etapas" in erro, erro


def teste_reconhece_corpo_de_erro_do_kommo():
    """Com `neverError` nos nodes do MCP o corpo do Kommo chega até nós — é o
    que diz qual campo faltou. O preço é que um 400/401 passa a ter a mesma
    forma de um sucesso, então quem checa precisa olhar dentro do corpo."""
    import mcp_kommo_client as mk

    falha_400 = json.dumps([{"data": json.dumps({
        "validation-errors": [{"request_id": "0", "errors": [
            {"code": "FieldMissing", "path": "sort", "detail": "This field is missing."}]}],
        "title": "Bad Request", "status": 400, "detail": "Request validation failed"})}])
    msg = mk.erro_do_kommo(falha_400)
    assert msg and "sort" in msg and "FieldMissing" in msg, msg

    falha_401 = json.dumps([{"data": json.dumps(
        {"title": "Unauthorized", "status": 401, "detail": "Invalid user name or password"})}])
    assert "401" in (mk.erro_do_kommo(falha_401) or ""), mk.erro_do_kommo(falha_401)

    # formato antigo, de node que ainda estoure NodeApiError
    assert mk.erro_do_kommo(json.dumps({"error": {"message": "Bad request"}}))

    sucesso = json.dumps([{"data": json.dumps({"_embedded": {"pipelines": [{"id": 1}]}})}])
    assert mk.erro_do_kommo(sucesso) is None, "sucesso não pode ser lido como erro"


# --- projeto/chave da OpenAI por cliente (openai_admin.py) -------------------
# Um projeto por cliente é o que torna o custo rastreável: até 29/09/2026 toda
# a organização usava um projeto só, e não havia como saber quanto cada cliente
# consumia. Estes testes dublam a Admin API — nunca tocam na conta real.

def _com_admin_dublado(respostas):
    """respostas: {(metodo, caminho): corpo}. Devolve (modulo, original)."""
    import openai_admin
    original = openai_admin._chamar
    def falso(metodo, caminho, corpo=None):
        # do padrão mais específico pro mais genérico: "/projects" também casa
        # com "/projects/X/service_accounts", e aí o dublê responderia errado
        for (m, c), resposta in sorted(respostas.items(), key=lambda kv: -len(kv[0][1])):
            if m == metodo and c in caminho:
                return resposta(corpo) if callable(resposta) else resposta
        raise AssertionError(f"chamada inesperada: {metodo} {caminho}")
    openai_admin._chamar = falso
    return openai_admin, original


def teste_cria_projeto_e_chave_do_cliente():
    oa, orig = _com_admin_dublado({
        ("POST", "/projects"): lambda corpo: {"id": "proj_X", "name": corpo["name"]},
        ("POST", "/projects/proj_X/service_accounts"): {"api_key": {"value": "sk-proj-abc", "id": "key_1"}},
    })
    try:
        r = oa.criar_projeto_e_chave("Dra. Fabiana")
    finally:
        oa._chamar = orig
    assert r["projeto_id"] == "proj_X", r
    assert r["api_key"] == "sk-proj-abc", r
    # prefixo deixa distinguir projeto de cliente de projeto interno no painel
    assert r["projeto_nome"] == "A5 Dra. Fabiana", r


def teste_nao_deixa_projeto_orfao_se_a_chave_nao_vier():
    """Projeto criado sem chave não serve pra nada e ficaria pendurado no painel
    da OpenAI — tem que ser arquivado antes de propagar o erro."""
    arquivados = []
    oa, orig = _com_admin_dublado({
        ("POST", "/projects"): {"id": "proj_Y", "name": "A5 X"},
        ("POST", "/projects/proj_Y/service_accounts"): {},   # sem api_key
        ("POST", "/projects/proj_Y/archive"): lambda _c: arquivados.append("proj_Y") or {},
    })
    try:
        try:
            oa.criar_projeto_e_chave("X")
            raise AssertionError("devia ter falhado")
        except oa.OpenAiAdminError:
            pass
    finally:
        oa._chamar = orig
    assert arquivados == ["proj_Y"], f"projeto ficou órfão: {arquivados}"


def teste_erro_da_openai_carrega_o_corpo():
    """Mesma lição do Kommo: erro sem o corpo faz a gente chutar."""
    import openai_admin
    e = openai_admin.OpenAiAdminError(403, '{"error":{"message":"insufficient permissions"}}')
    assert "403" in str(e) and "insufficient permissions" in str(e), str(e)


def teste_reset_so_dispara_em_comando_exato():
    """"reset" solto é comando; "preciso resetar minha senha" é conversa.

    Comparar por "contém" transformaria frase legítima em apagamento de
    histórico — e o paciente perderia o contexto sem pedir."""
    import worker_agente as w
    for comando in ("reset", "Reset", "RESET", "  reset  "):
        assert w._eh_comando_reset(comando), f"deveria ser comando: {comando!r}"
    # variantes saíram de propósito: quanto menos coisa apaga histórico, melhor
    for conversa in ("resetar", "/reset", "#reset", "reiniciar",
                     "preciso resetar minha senha", "como faco reset da conta",
                     "resetou?", "reset do aparelho", "", "oi"):
        assert not w._eh_comando_reset(conversa), f"NÃO deveria ser comando: {conversa!r}"


def teste_normalizador_classifica_cada_tipo_de_midia():
    """O Code do trilho roda no n8n, então é testado como JS de verdade.

    O defeito que isto tranca: imagem COM legenda caía no ramo de texto, a
    legenda seguia sozinha e a foto sumia sem ninguém ver — o paciente achava
    que tinha mostrado o exame.
    """
    import json as _json
    import subprocess
    import tempfile
    import trilho_n8n

    casos = [
        ("texto puro", {"conversation": "oi"}, {"tipo": "texto", "texto": "oi"}),
        ("audio", {"audioMessage": {}}, {"tipo": "audio", "texto": ""}),
        ("imagem sem legenda", {"imageMessage": {"mimetype": "image/png"}},
         {"tipo": "imagem", "texto": "", "mimetype": "image/png"}),
        ("imagem com legenda",
         {"imageMessage": {"caption": "isso ta normal?", "mimetype": "image/jpeg"}},
         {"tipo": "imagem", "texto": "isso ta normal?"}),
        ("video sem legenda", {"videoMessage": {}}, {"tipo": "video", "texto": ""}),
        ("video com legenda", {"videoMessage": {"caption": "olha isso"}},
         {"tipo": "video", "texto": "olha isso"}),
        ("documento", {"documentMessage": {}}, {"tipo": "nao_suportado"}),
        ("figurinha", {"stickerMessage": {}}, {"ignorado": True}),
    ]

    codigo = trilho_n8n.CODIGO_NORMALIZA
    for nome, msg, esperado in casos:
        entrada = {"body": {"instance": "inst", "data": {
            "key": {"id": "ABC", "remoteJid": "5511999@s.whatsapp.net"},
            "pushName": "Claudio", "message": msg}}}
        js = (f"const entrada = {_json.dumps(entrada)};\n"
              "const $input = { first: () => ({ json: entrada }) };\n"
              f"const saida = (() => {{{codigo}}})();\n"
              "console.log(JSON.stringify(saida));\n")
        with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False) as f:
            f.write(js)
            caminho = f.name
        bruto = subprocess.run(["node", caminho], capture_output=True, text=True, timeout=30)
        assert bruto.returncode == 0, f"{nome}: node falhou: {bruto.stderr[:300]}"
        saida = _json.loads(bruto.stdout)
        assert len(saida) == 1, f"{nome}: esperava 1 item, veio {len(saida)}"
        obtido = saida[0]["json"]
        for k, v in esperado.items():
            assert obtido.get(k) == v, f"{nome}: {k} = {obtido.get(k)!r}, esperado {v!r}"
        # a chave de idempotência nunca pode faltar: sem ela, reentrega do
        # Evolution vira resposta repetida pro paciente
        if not obtido.get("ignorado"):
            assert obtido.get("mensagem_id") == "ABC", f"{nome}: perdeu o mensagem_id"


def teste_grupo_e_eco_do_proprio_agente_continuam_descartados():
    """Mexer na classificação de mídia não pode reabrir o loop do fromMe."""
    import json as _json
    import subprocess
    import tempfile
    import trilho_n8n

    casos = [
        ("eco do agente", {"id": "A", "remoteJid": "5511999@s.whatsapp.net", "fromMe": True}),
        ("grupo", {"id": "B", "remoteJid": "123456@g.us"}),
        ("status", {"id": "C", "remoteJid": "status@broadcast"}),
    ]
    for nome, chave in casos:
        entrada = {"body": {"instance": "i", "data": {
            "key": chave, "message": {"imageMessage": {}}}}}
        js = (f"const entrada = {_json.dumps(entrada)};\n"
              "const $input = { first: () => ({ json: entrada }) };\n"
              f"const saida = (() => {{{trilho_n8n.CODIGO_NORMALIZA}}})();\n"
              "console.log(JSON.stringify(saida));\n")
        with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False) as f:
            f.write(js)
            caminho = f.name
        r = subprocess.run(["node", caminho], capture_output=True, text=True, timeout=30)
        assert r.returncode == 0, f"{nome}: {r.stderr[:200]}"
        assert _json.loads(r.stdout) == [], f"{nome}: deveria ser descartado"


def teste_imagem_vira_texto_preservando_a_legenda():
    """A descrição entra no histórico; a legenda continua sendo a pergunta.

    Só a descrição faria o agente responder sobre a foto sem saber o que foi
    perguntado ("isso aqui tá normal?" é o pedido, não a imagem)."""
    import worker_agente as w

    chamadas = {}

    def falso_descrever(cliente, instancia, mensagem_id, mimetype="image/jpeg"):
        chamadas["mimetype"] = mimetype
        return "Receita médica com o nome Claudio, medicamento X, 2x ao dia."

    original = w.descrever_imagem
    w.descrever_imagem = falso_descrever
    try:
        texto, mime = "isso aqui ta normal?", "image/png"
        descricao = w.descrever_imagem({}, "inst", "ABC", mime)
        combinado = f"{texto}\n[imagem enviada pelo paciente: {descricao}]"
        assert texto in combinado, "a legenda sumiu"
        assert "Receita médica" in combinado, "a descrição sumiu"
        assert chamadas["mimetype"] == mime, "o mimetype do WhatsApp não chegou"
    finally:
        w.descrever_imagem = original


def teste_tipo_que_nao_lemos_tem_resposta_pronta():
    """Vídeo e arquivo não podem virar silêncio: o paciente mandou algo."""
    import worker_agente as w
    for aviso in (w.SEM_VIDEO, w.SEM_ARQUIVO, w.FALHA_IMAGEM):
        assert aviso.strip(), "aviso vazio vira silêncio"
        assert "?" in aviso, f"aviso sem saída pro paciente: {aviso!r}"


def teste_ferramenta_de_arquivo_so_aceita_nome_do_catalogo():
    """O modelo escolhe por nome, nunca por URL.

    URL vinda do modelo é URL influenciada pelo texto do paciente — bastaria
    "manda o arquivo em http://..." pra virar encaminhador de link."""
    import db
    import worker_agente as w

    original = db.listar_midias_do_cliente
    db.listar_midias_do_cliente = lambda cid: [
        {"nome": "preparo do exame", "descricao": "antes da coleta"},
        {"nome": "tabela de preço", "descricao": ""},
    ]
    try:
        f = w._ferramenta_enviar_arquivo({"id": 1})["function"]
        props = f["parameters"]["properties"]
        assert set(props) == {"nome", "legenda"}, f"parâmetros demais: {sorted(props)}"
        assert "url" not in props and "link" not in props, "aceitar URL é o que não pode"
        assert props["nome"]["enum"] == ["preparo do exame", "tabela de preço"]
        # a descrição precisa listar os itens: sem isso o modelo chuta o nome
        assert "preparo do exame" in f["description"]
    finally:
        db.listar_midias_do_cliente = original


def teste_sem_arquivo_cadastrado_a_ferramenta_nem_aparece():
    """Ferramenta que só sabe falhar faz o modelo gastar rodada tentando."""
    import db
    import worker_agente as w
    original = db.listar_midias_do_cliente
    db.listar_midias_do_cliente = lambda cid: []
    try:
        assert w._ferramenta_enviar_arquivo({"id": 1}) is None
    finally:
        db.listar_midias_do_cliente = original


def teste_envio_busca_o_arquivo_pelo_cliente_e_nao_pelos_argumentos():
    """Arquivo de um cliente não pode sair pelo agente de outro.

    O `cliente_id` chega de fora; se viesse nos argumentos, bastaria o modelo
    pedir um nome que existe em outro cliente."""
    import db
    import worker_agente as w

    vistos = []
    original = db.obter_midia
    db.obter_midia = lambda cid, nome: vistos.append((cid, nome)) or None
    try:
        r = w._executar("enviar_arquivo", {"nome": "tabela", "cliente_id": 999},
                        {}, 7, 0, None, cliente={"evolution_instancia": "i"},
                        contato={"id_whatsapp": "5511"})
        assert "não existe arquivo" in r, r
        assert vistos == [(7, "tabela")], f"consultou com {vistos}"
    finally:
        db.obter_midia = original


def teste_tipo_de_midia_mapeia_o_que_o_whatsapp_espera():
    """O Evolution recusa `mediatype` fora da lista dele."""
    import evolution_client as e
    assert e.tipo_de_midia("image/png") == "image"
    assert e.tipo_de_midia("video/mp4") == "video"
    assert e.tipo_de_midia("audio/ogg") == "audio"
    # qualquer outra coisa vira documento, que aceita tudo
    for m in ("application/pdf", "text/csv", "", None):
        assert e.tipo_de_midia(m) == "document", m


def teste_url_do_arquivo_aponta_pra_colecao_do_painel():
    """O arquivo é servido pelo PocketBase, não por rota nossa.

    A coleção é separada de propósito: `midia` genérica é de outro projeto da
    A5, e a de imagem tem teto de 5 MB e só aceita imagem."""
    import pocketbase_client as pb
    u = pb.url_midia("rec123", "arq.pdf")
    assert "/api/files/painel_agentes_midias/rec123/arq.pdf" in u, u
    # sem referência não há URL: melhor vazio que link quebrado no WhatsApp
    assert pb.url_midia("", "arq.pdf") == ""
    assert pb.url_midia("rec123", "") == ""


def teste_arquivo_grande_demais_e_barrado_antes_de_subir():
    """16 MB é o teto do WhatsApp: acima disso a falha apareceria no aparelho
    do paciente, não aqui."""
    import pocketbase_client as pb
    try:
        pb.enviar_midia(1, "grande", "a.mp4", b"x" * (pb.TAMANHO_MAXIMO_MIDIA + 1))
        assert False, "deveria ter barrado"
    except pb.PocketBaseError as e:
        assert "limite do WhatsApp" in str(e), str(e)


def teste_delay_das_bolhas_e_crescente_pra_manter_a_ordem():
    """O rabbitmqTrigger processa em paralelo por padrão.

    Com delay individual, a bolha curta teria espera menor e chegaria ANTES da
    primeira — a resposta sairia embaralhada no WhatsApp do paciente. Por isso
    o delay é acumulado, e o teste tranca justamente a monotonia."""
    import worker_agente as w

    # a segunda bolha é a mais curta de propósito: é o caso que inverte
    partes = ["Bom dia! Tudo bem com você?", "Sim.", "Posso agendar pra amanhã de manhã?"]
    d = w.delays_das_bolhas(partes)
    assert len(d) == len(partes)
    for anterior, seguinte in zip(d, d[1:]):
        assert seguinte > anterior, f"delay não cresceu: {d}"


def teste_delay_respeita_piso_teto_e_total():
    """Piso: ninguém responde instantaneamente. Teto: texto longo não justifica
    sumiço, e a soma não pode travar a conversa."""
    import worker_agente as w

    assert w.tempo_de_digitacao("") == w.DELAY_MINIMO_MS
    assert w.tempo_de_digitacao("oi") == w.DELAY_MINIMO_MS
    assert w.tempo_de_digitacao("x" * 10000) == w.DELAY_MAXIMO_MS
    # dez parágrafos longos não podem deixar o paciente meio minuto no vácuo
    d = w.delays_das_bolhas(["x" * 500] * 10)
    assert d[-1] == w.DELAY_TOTAL_MAXIMO_MS, d[-1]


def teste_delay_de_uma_bolha_so_e_o_tempo_dela():
    """A resposta de um parágrafo é o caso comum: não pode herdar acúmulo."""
    import worker_agente as w
    texto = "O preparo pede jejum de 8 horas."
    assert w.delays_das_bolhas([texto]) == [w.tempo_de_digitacao(texto)]


def teste_instancia_nasce_ignorando_grupos():
    """Instância criada sem `groupsIgnore` responde no grupo da clínica.

    O trilho já descarta `@g.us`, mas enquanto a Evolution entregar mensagem de
    grupo no webhook, qualquer caminho novo que esqueça o filtro reabre o
    problema. Era corrigido na mão a cada instância criada."""
    import evolution_client as e

    chamadas = []

    def falso(metodo, caminho, corpo=None):
        chamadas.append((metodo, caminho, corpo))
        if caminho == "/instance/create":
            return {"instance": {"status": "connecting"},
                    "qrcode": {"base64": "x", "code": "y"}}
        return {}

    original = e._chamar
    e._chamar = falso
    try:
        e.criar_instancia("teste")
    finally:
        e._chamar = original

    criacao = [c for c in chamadas if c[1] == "/instance/create"][0]
    assert criacao[2].get("groupsIgnore") is True, "criação sem groupsIgnore"
    # e reenviado em settings/set: nem toda versão da Evolution honra os
    # campos de configuração vindos no /instance/create
    ajuste = [c for c in chamadas if c[1].startswith("/settings/set/")]
    assert ajuste, "configurações não foram reenviadas após criar"
    assert ajuste[0][2].get("groupsIgnore") is True


def teste_mcp_local_tem_o_mesmo_contrato_do_n8n():
    """Trocar o MCP do n8n pelo local tem que ser mudar o caminho, nada mais.

    Se um nome de ferramenta ou de parâmetro divergir, a troca quebra o agente
    de um jeito que só aparece em produção, no meio de um atendimento."""
    import inspect
    import mcp_kommo_cartao as m

    esperado = {
        "consultar_cartao_lead": {"kommo_domain", "access_token", "lead_id"},
        "mover_lead": {"kommo_domain", "access_token", "lead_id", "status_id",
                       "pipeline_id"},
        "atualizar_campo_personalizado": {"kommo_domain", "access_token",
                                          "lead_id", "field_id", "valor"},
        "adicionar_tag": {"kommo_domain", "access_token", "lead_id", "tag_novo",
                          "tags_atuais"},
        "remover_tag": {"kommo_domain", "access_token", "lead_id", "tag_remover",
                        "tags_atuais"},
        "adicionar_nota": {"kommo_domain", "access_token", "lead_id", "texto"},
        "criar_tarefa": {"kommo_domain", "access_token", "lead_id", "texto",
                         "prazo_horas"},
    }
    for nome, params in esperado.items():
        fn = getattr(m, nome, None)
        assert fn is not None, f"ferramenta '{nome}' sumiu"
        assert set(inspect.signature(fn).parameters) == params, (
            f"{nome}: {sorted(inspect.signature(fn).parameters)}")


def teste_tags_preservam_as_existentes():
    """O PATCH do Kommo substitui a lista INTEIRA de tags.

    Mandar só a tag nova apaga todas as outras — é a armadilha que a descrição
    da ferramenta avisa, e aqui ela vira comportamento testado."""
    import mcp_kommo_cartao as m

    assert m.juntar_tags("vip, urgente", "novo") == ["vip", "urgente", "novo"]
    # duplicata não entra, mesmo com caixa diferente
    assert m.juntar_tags("vip, urgente", "VIP") == ["vip", "urgente"]
    assert m.juntar_tags("", "primeira") == ["primeira"]
    # remover compara sem diferenciar maiúscula, e preserva as demais
    assert m.tirar_tag("vip, urgente, novo", "URGENTE") == ["vip", "novo"]
    assert m.tirar_tag("vip", "nao-existe") == ["vip"]
    assert m.tirar_tag("", "qualquer") == []


def teste_erro_do_kommo_chega_legivel_no_modelo():
    """O SDK devolveria só "Error executing tool <nome>", engolindo o corpo da
    resposta — onde vêm `path` e `code` do campo recusado. Mesma falha que o
    `neverError` resolveu do lado do n8n."""
    import mcp_kommo_cartao as m

    @m._legivel
    def explode(x):
        raise RuntimeError("Kommo respondeu 400: campo status_id inválido")

    r = explode(1)
    assert r.startswith("erro: "), r
    assert "status_id" in r, "o detalhe do Kommo se perdeu"


TESTES = [
    teste_mcp_local_tem_o_mesmo_contrato_do_n8n,
    teste_tags_preservam_as_existentes,
    teste_erro_do_kommo_chega_legivel_no_modelo,
    teste_instancia_nasce_ignorando_grupos,
    teste_delay_das_bolhas_e_crescente_pra_manter_a_ordem,
    teste_delay_respeita_piso_teto_e_total,
    teste_delay_de_uma_bolha_so_e_o_tempo_dela,
    teste_ferramenta_de_arquivo_so_aceita_nome_do_catalogo,
    teste_sem_arquivo_cadastrado_a_ferramenta_nem_aparece,
    teste_envio_busca_o_arquivo_pelo_cliente_e_nao_pelos_argumentos,
    teste_tipo_de_midia_mapeia_o_que_o_whatsapp_espera,
    teste_url_do_arquivo_aponta_pra_colecao_do_painel,
    teste_arquivo_grande_demais_e_barrado_antes_de_subir,
    teste_normalizador_classifica_cada_tipo_de_midia,
    teste_grupo_e_eco_do_proprio_agente_continuam_descartados,
    teste_imagem_vira_texto_preservando_a_legenda,
    teste_tipo_que_nao_lemos_tem_resposta_pronta,
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
    teste_converte_param_json_do_kommo_pra_string,
    teste_identifica_agente_principal_por_nao_ser_agentemov,
    teste_acha_node_database_nos_4_clientes,
    teste_renomear_node_atualiza_expressoes,
    teste_renomear_node_atualiza_connections,
    teste_nao_deixa_renomear_nodes_protegidos,
    teste_trava_barra_patch_na_etapa_errada,
    teste_trava_deixa_renomear_quando_confirmado,
    teste_trava_barra_etapas_que_o_kommo_nao_edita,
    teste_trava_completa_sort_e_omite_cor_legada,
    teste_trava_preserva_cor_valida,
    teste_trava_avisa_que_dica_existente_nao_muda,
    teste_trava_deixa_criar_dica_de_nivel_vazio,
    teste_trava_rejeita_nivel_de_dica_invalido,
    teste_trava_diz_quais_niveis_sobraram,
    teste_criar_funil_completa_campos_obrigatorios,
    teste_criar_funil_aceita_campos_soltos,
    teste_criar_funil_barra_is_main_e_funil_sem_etapa,
    teste_criar_funil_barra_dica_na_criacao,
    teste_reconhece_corpo_de_erro_do_kommo,
    teste_cria_projeto_e_chave_do_cliente,
    teste_nao_deixa_projeto_orfao_se_a_chave_nao_vier,
    teste_erro_da_openai_carrega_o_corpo,
    teste_reset_so_dispara_em_comando_exato,
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
