"""Testes do sistema de agentes (fora do motor de clonagem, removido em
30/10/2026 junto com cloner.py/manifest.py/n8n_edicao.py — ver
ARQUITETURA-AGENTE.md). Rodar: python3 testes.py

Cada teste cita o achado real da investigação (ver plano) que ele confirma.
"""
import json


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


def teste_catalogo_esconde_o_kommo_completo_sempre_ativo():
    """"Kommo MCP - Completo" é usado direto pelo chat de gerenciamento, pra
    todo cliente, sem passar por cliente_mcps (ver agente_chat.py). Listar
    ele na aba Ferramentas (30/10/2026, a pedido do Fernando) não ajuda — já
    está ligado, não tem o que marcar — e marcar "no_atendimento" nele por
    engano entregaria as ferramentas de estruturação de funil pro agente que
    fala com o paciente, contra a decisão de 07/10/2026."""
    import n8n_client
    import mcp_cliente as m

    class FalsoN8n:
        def _request(self, metodo, caminho):
            return {"data": [{"id": "1", "name": "Kommo MCP - Completo"},
                             {"id": "2", "name": "Agenda"}]}
        def get_workflow(self, wid):
            path = "kommo-completo" if wid == "1" else "agenda"
            return {"active": True, "nodes": [
                {"type": "n8n-nodes-langchain.mcpTrigger",
                 "parameters": {"path": path, "authentication": "headerAuth"}}]}

    original = n8n_client.N8nClient
    n8n_client.N8nClient = FalsoN8n
    try:
        achados = {c["path"] for c in m.catalogo()}
    finally:
        n8n_client.N8nClient = original
    assert achados == {"agenda"}, achados


# --- cache de MCP (mcp_cliente.py, 30/10/2026) ------------------------------
# worker_agente.py chama listar_ferramentas a cada mensagem do paciente, pra
# cada MCP ligado — sem cache isso é uma sessão MCP inteira por mensagem, e
# pior ainda pra MCP antigo (SSE), que pagava uma tentativa de Streamable
# HTTP fadada a falhar toda vez antes de cair pro certo.

def teste_listar_ferramentas_usa_cache_e_nao_vaza_apelido_entre_chamadas():
    """Duas chamadas seguidas (mesmo path) não podem abrir duas sessões MCP —
    e dois apelidos diferentes pro MESMO path não podem ver o prefixo um do
    outro (bug possível se o cache devolvesse a mesma lista por referência,
    em vez de uma cópia pra cada chamador prefixar à vontade)."""
    import mcp_cliente as m

    chamadas = []
    async def _listar_falso(path, token):
        chamadas.append(1)
        return [{"name": "buscar_horarios", "description": "x", "input_schema": {}}]

    original = m._listar_async
    m._listar_async = _listar_falso
    m._cache_ferramentas.clear()
    try:
        a = m.listar_ferramentas("agenda", apelido="clinica")
        b = m.listar_ferramentas("agenda", apelido="dentista")
    finally:
        m._listar_async = original
        m._cache_ferramentas.clear()
    assert len(chamadas) == 1, "segunda chamada não devia ter reaberto sessão MCP"
    assert a[0]["name"] == "clinica__buscar_horarios"
    assert b[0]["name"] == "dentista__buscar_horarios"


def teste_cache_de_ferramentas_expira_pelo_ttl():
    import mcp_cliente as m

    chamadas = []
    async def _listar_falso(path, token):
        chamadas.append(1)
        return [{"name": "x", "description": "", "input_schema": {}}]

    original = m._listar_async
    m._listar_async = _listar_falso
    m._cache_ferramentas.clear()
    try:
        m.listar_ferramentas("p")
        # força a entrada a já ter expirado, sem precisar esperar o TTL de verdade
        _, ferramentas = m._cache_ferramentas[("p", "")]
        m._cache_ferramentas[("p", "")] = (0, ferramentas)
        m.listar_ferramentas("p")
    finally:
        m._listar_async = original
        m._cache_ferramentas.clear()
    assert len(chamadas) == 2, "entrada expirada devia ter buscado de novo"


def teste_cache_de_transporte_e_autocurativo():
    """Lembrar o transporte que funcionou da última vez acelera a próxima
    chamada — mas se ele parar de funcionar (node trocou de versão no n8n),
    cai pro outro em vez de ficar preso numa memória desatualizada."""
    import asyncio
    import mcp_cliente as m

    tentativas = []
    async def falso_sse(path, cab, acao):
        tentativas.append("sse")
        raise RuntimeError("sse não responde mais")
    async def falso_http(path, cab, acao):
        tentativas.append("http")
        return "ok"

    m._cache_transporte["algumpath"] = "sse"   # memória desatualizada
    originais = dict(m._TRANSPORTES)
    m._TRANSPORTES["sse"] = falso_sse
    m._TRANSPORTES["http"] = falso_http
    try:
        r = asyncio.run(m._com_sessao("algumpath", "", lambda s: None))
        assert tentativas == ["sse", "http"], tentativas
        assert r == "ok"
        assert m._cache_transporte["algumpath"] == "http", "devia ter atualizado a memória"
    finally:
        m._TRANSPORTES.clear()
        m._TRANSPORTES.update(originais)
        m._cache_transporte.pop("algumpath", None)


# --- métricas de ferramenta (metricas.py, 30/10/2026) -----------------------
# Dado pra decidir onde vale repetir o que já foi feito com o Kommo (tirar do
# MCP, cachear) — sem saber o que é mais chamado/mais lento, é chute.

def _com_metrica_dublada():
    """Troca db.registrar_metrica_ferramenta por um dublê que só guarda as
    chamadas — nenhum teste aqui precisa de Postgres de verdade."""
    import db
    gravadas = []
    original = db.registrar_metrica_ferramenta
    def falso(cliente_id, origem, ferramenta, sucesso, duracao_ms):
        gravadas.append((cliente_id, origem, ferramenta, sucesso, duracao_ms))
    db.registrar_metrica_ferramenta = falso
    return gravadas, original


def teste_medir_grava_sucesso_e_devolve_o_resultado_intacto():
    import db
    import metricas

    gravadas, original = _com_metrica_dublada()
    try:
        r = metricas.medir("worker", 7, "consultar_cartao_lead", lambda: "ok: 1 lead")
    finally:
        db.registrar_metrica_ferramenta = original
    assert r == "ok: 1 lead", "o resultado não pode vir alterado"
    assert len(gravadas) == 1
    cliente_id, origem, ferramenta, sucesso, duracao_ms = gravadas[0]
    assert (cliente_id, origem, ferramenta) == (7, "worker", "consultar_cartao_lead")
    assert sucesso is True
    assert duracao_ms >= 0


def teste_medir_reconhece_erro_sem_mudar_o_retorno():
    """Convenção do sistema: "erro: ..." é falha sem levantar exceção. A
    métrica tem que reconhecer isso como sucesso=False, mas o texto de erro
    ainda tem que chegar intacto em quem chamou — é o que o paciente/painel
    precisa ver."""
    import db
    import metricas

    gravadas, original = _com_metrica_dublada()
    try:
        r = metricas.medir("chat", 3, "kommo_criar_funil", lambda: "erro: campo sort faltando")
    finally:
        db.registrar_metrica_ferramenta = original
    assert r == "erro: campo sort faltando"
    assert gravadas[0][3] is False, "erro: ... tinha que marcar sucesso=False"


def teste_medir_deixa_excecao_subir_e_ainda_assim_grava():
    """agente_geral.py não captura exceção no dispatch (ao contrário de
    worker_agente.py/agente_chat.py) — medir() não pode mudar isso: a
    exceção tem que subir igual a antes, só que com a métrica já gravada."""
    import db
    import metricas

    gravadas, original = _com_metrica_dublada()
    def explode():
        raise RuntimeError("Kommo fora do ar")
    try:
        try:
            metricas.medir("geral", 9, "mudar_prompt", explode)
            raise AssertionError("devia ter deixado a exceção subir")
        except RuntimeError as e:
            assert "Kommo fora do ar" in str(e)
    finally:
        db.registrar_metrica_ferramenta = original
    assert len(gravadas) == 1
    assert gravadas[0][3] is False, "exceção tinha que marcar sucesso=False"


def teste_medir_nao_derruba_se_a_gravacao_falhar():
    """Falha ao gravar métrica (banco fora do ar, etc) não pode virar falha
    de atendimento — o resultado da ferramenta tem que chegar igual."""
    import db
    import metricas

    original = db.registrar_metrica_ferramenta
    def explode_ao_gravar(*a, **kw):
        raise RuntimeError("banco fora do ar")
    db.registrar_metrica_ferramenta = explode_ao_gravar
    try:
        r = metricas.medir("worker", 1, "chamar_humano", lambda: "ok: chamado")
    finally:
        db.registrar_metrica_ferramenta = original
    assert r == "ok: chamado"


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


# --- Kommo nativo no worker (30/10/2026) --------------------------------
# As 7 ferramentas do Cartão do Lead saíram do caminho MCP (sessão + handshake
# a cada mensagem) e viraram chamada direta: `kommo_cartao.py` tem só a
# lógica (zero dependência de MCP), `mcp_kommo_cartao.py` é casca fina em
# cima pra quem ainda precisa do protocolo (Claude Desktop, rede), e
# `worker_agente.py` importa `kommo_cartao` e chama as funções direto.

def teste_kommo_cartao_nao_depende_de_mcp():
    """`kommo_cartao.py` tem que importar em qualquer ambiente, mesmo sem o
    pacote `mcp` instalado (como este sandbox, de propósito) — é a prova de
    que a extração realmente não carregou nada do SDK de MCP. Se alguém
    importar `mcp`/`uvicorn`/`starlette` aqui por engano, este teste some da
    lista de "OK" e vira um ImportError explícito."""
    import kommo_cartao as k
    assert callable(k.consultar_cartao_lead)
    assert callable(k.mover_lead)


def teste_ferramentas_kommo_cartao_do_worker_batem_com_kommo_cartao():
    """O schema que o worker mostra pro modelo (FERRAMENTAS_KOMMO_CARTAO) tem
    que pedir exatamente os parâmetros que `kommo_cartao.py` espera — menos
    kommo_domain/access_token, que a ferramenta nativa nunca expõe ao modelo
    (ver worker_agente.py: `_executar` injeta do cadastro, não do schema)."""
    import inspect
    import kommo_cartao as k
    import worker_agente as w

    assert w.NOMES_KOMMO_CARTAO == {
        "consultar_cartao_lead", "mover_lead", "atualizar_campo_personalizado",
        "adicionar_tag", "remover_tag", "adicionar_nota", "criar_tarefa",
    }
    for f in w.FERRAMENTAS_KOMMO_CARTAO:
        nome = f["function"]["name"]
        do_schema = set(f["function"]["parameters"]["properties"])
        da_funcao = set(inspect.signature(getattr(k, nome)).parameters) - {"kommo_domain", "access_token"}
        assert do_schema == da_funcao, f"{nome}: schema={do_schema} função={da_funcao}"


def teste_executar_kommo_cartao_ignora_credencial_do_modelo():
    """Mesma trava que já existia pro MCP genérico: kommo_domain/access_token
    que o modelo tentar mandar são descartados, só o que vem de `credenciais`
    (o cadastro do cliente) chega na chamada de verdade."""
    import kommo_cartao as k
    import worker_agente as w

    vistos = {}
    original = k._chamar
    def falso(dominio, token, metodo, caminho, corpo=None):
        vistos.update(dominio=dominio, token=token)
        return {"id": 1}
    k._chamar = falso
    try:
        r = w._executar(
            "consultar_cartao_lead",
            {"lead_id": "42", "kommo_domain": "invasor.kommo.com", "access_token": "roubado"},
            {"kommo_domain": "clientereal", "access_token": "token-de-verdade"},
            cliente_id=1,
        )
    finally:
        k._chamar = original
    assert vistos["dominio"] == "clientereal", vistos
    assert vistos["token"] == "token-de-verdade", vistos
    assert '"id": 1' in r


def teste_executar_kommo_cartao_sem_credencial_nao_chama_o_kommo():
    import worker_agente as w
    r = w._executar("mover_lead", {"lead_id": "1", "status_id": "2"}, {}, cliente_id=1)
    assert r.startswith("erro:") and "Kommo" in r, r


# --- QA de atendimento (qa_atendimento.py) ------------------------------
# O prompt do juiz tem um exemplo de JSON literal no meio do template —
# achado ao escrever o módulo (30/10/2026): `str.format()` tenta resolver
# QUALQUER `{...}` como placeholder, não só `{prompt_cliente}`/`{conversa}`,
# e isso quebrava toda chamada com KeyError, engolido pelo `except Exception`
# de `avaliar_conversa` — nunca apareceria como erro óbvio, só como "toda
# avaliação falha" sem explicação. Este teste tranca que o template renderiza.

def _com_openai_dublado(conteudo_resposta):
    """Troca a classe OpenAI usada por qa_atendimento por uma dublê que devolve
    `conteudo_resposta` (string) como se fosse `choices[0].message.content`."""
    import os
    import config
    import qa_atendimento as qa

    # a chave em si nunca é usada (a classe toda é dublê) — só precisa existir
    # pra carregar_env() não estourar KeyError antes de chegar na dublagem
    os.environ.setdefault("OPENAI_API_KEY", "sk-teste-fake")
    config._cache = None

    class _Resposta:
        choices = [type("Escolha", (), {
            "message": type("Msg", (), {"content": conteudo_resposta})()})]

    class _ChatDublado:
        def create(self, **kwargs):
            return _Resposta()

    class _OpenAIDublado:
        def __init__(self, **kwargs):
            self.chat = type("Chat", (), {"completions": _ChatDublado()})()

    original = qa.OpenAI
    qa.OpenAI = _OpenAIDublado
    return qa, original


def teste_qa_renderiza_o_prompt_sem_quebrar_no_json_de_exemplo():
    import qa_atendimento as qa
    texto = qa.INSTRUCAO.format(prompt_cliente="atenda bem", conversa="paciente: oi")
    assert "atenda bem" in texto and "paciente: oi" in texto
    # o exemplo de formato tem que sobreviver literal, chaves incluídas
    assert '"severidade"' in texto, "o JSON de exemplo sumiu ou quebrou o format()"


def teste_qa_aceita_veredito_valido():
    qa, orig = _com_openai_dublado(
        '{"severidade": "atencao", "problemas": ["repetiu a pergunta"], "resumo": "x"}')
    try:
        r = qa.avaliar_conversa("prompt", [{"role": "user", "conteudo": "oi"},
                                           {"role": "assistant", "conteudo": "olá"}])
    finally:
        qa.OpenAI = orig
    assert r == {"severidade": "atencao", "problemas": ["repetiu a pergunta"], "resumo": "x"}


def teste_qa_rejeita_severidade_fora_do_enum():
    """Veredito mal-formado não pode virar dado gravado — melhor None (quem
    chama loga erro e tenta de novo depois) que uma severidade inventada."""
    qa, orig = _com_openai_dublado('{"severidade": "ruim", "problemas": [], "resumo": "x"}')
    try:
        assert qa.avaliar_conversa("prompt", [{"role": "user", "conteudo": "oi"}]) is None
    finally:
        qa.OpenAI = orig


def teste_qa_ignora_conversa_vazia_sem_chamar_a_openai():
    import qa_atendimento as qa
    assert qa.avaliar_conversa("prompt", []) is None


# --- sugestão de melhoria de prompt baseada no QA (30/10/2026) --------------
# Fecha o ciclo do QA sem deixar a IA aplicar nada sozinha — só propõe, quem
# aplica é a rota de prompt que já existe (com backup automático).

def teste_sugestao_melhoria_renderiza_o_prompt_sem_quebrar_no_json_de_exemplo():
    """Mesmo cuidado do bug já achado em INSTRUCAO: o exemplo de JSON no meio
    do texto tem que sobreviver ao str.format()."""
    import qa_atendimento as qa
    texto = qa.INSTRUCAO_MELHORIA.format(prompt_atual="atenda bem", problemas="- repetiu pergunta")
    assert "atenda bem" in texto and "repetiu pergunta" in texto
    assert '"prompt_sugerido"' in texto, "o JSON de exemplo sumiu ou quebrou o format()"


def teste_sugestao_melhoria_aceita_resposta_valida():
    qa, orig = _com_openai_dublado(
        '{"prompt_sugerido": "Você é Bia...", "motivo": "parou de repetir pergunta"}')
    try:
        r = qa.sugerir_melhoria_prompt("prompt atual", [{"resumo": "repetiu pergunta", "problemas": []}])
    finally:
        qa.OpenAI = orig
    assert r == {"prompt_sugerido": "Você é Bia...", "motivo": "parou de repetir pergunta"}


def teste_sugestao_melhoria_rejeita_resposta_incompleta():
    """prompt_sugerido ou motivo vazio não pode virar sugestão salva — melhor
    None (painel mostra erro, pessoa tenta de novo) que meio-feito."""
    qa, orig = _com_openai_dublado('{"prompt_sugerido": "", "motivo": "x"}')
    try:
        assert qa.sugerir_melhoria_prompt("prompt", [{"resumo": "x", "problemas": []}]) is None
    finally:
        qa.OpenAI = orig


def teste_sugestao_melhoria_ignora_lista_vazia_sem_chamar_a_openai():
    """Sem problema nenhum pra resolver, não tem o que sugerir — e não gasta
    chamada de API à toa."""
    import qa_atendimento as qa
    assert qa.sugerir_melhoria_prompt("prompt", []) is None


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
    teste_converte_param_json_do_kommo_pra_string,
    teste_catalogo_esconde_o_kommo_completo_sempre_ativo,
    teste_listar_ferramentas_usa_cache_e_nao_vaza_apelido_entre_chamadas,
    teste_cache_de_ferramentas_expira_pelo_ttl,
    teste_cache_de_transporte_e_autocurativo,
    teste_medir_grava_sucesso_e_devolve_o_resultado_intacto,
    teste_medir_reconhece_erro_sem_mudar_o_retorno,
    teste_medir_deixa_excecao_subir_e_ainda_assim_grava,
    teste_medir_nao_derruba_se_a_gravacao_falhar,
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
    teste_kommo_cartao_nao_depende_de_mcp,
    teste_ferramentas_kommo_cartao_do_worker_batem_com_kommo_cartao,
    teste_executar_kommo_cartao_ignora_credencial_do_modelo,
    teste_executar_kommo_cartao_sem_credencial_nao_chama_o_kommo,
    teste_qa_renderiza_o_prompt_sem_quebrar_no_json_de_exemplo,
    teste_qa_aceita_veredito_valido,
    teste_qa_rejeita_severidade_fora_do_enum,
    teste_qa_ignora_conversa_vazia_sem_chamar_a_openai,
    teste_sugestao_melhoria_renderiza_o_prompt_sem_quebrar_no_json_de_exemplo,
    teste_sugestao_melhoria_aceita_resposta_valida,
    teste_sugestao_melhoria_rejeita_resposta_incompleta,
    teste_sugestao_melhoria_ignora_lista_vazia_sem_chamar_a_openai,
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
