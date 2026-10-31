"""Ferramentas do Cartão do Lead (Kommo): consultar, mover de etapa, atualizar
campo personalizado, adicionar/remover tag, registrar nota, criar tarefa.

Só biblioteca padrão, sem dependência de MCP — pensado pra ser chamado direto
em processo (`worker_agente.py` importa e chama como função Python comum, sem
sessão de rede nenhuma no meio). Quem precisa expor isso como servidor MCP de
verdade (Claude Desktop, n8n, outro processo) usa `mcp_kommo_cartao.py`, que
só aplica `@servidor.tool()` em cima destas mesmas funções.

## Nenhuma credencial mora aqui

`kommo_domain` e `access_token` chegam em CADA chamada, como parâmetro. Quem
chama é que injeta a credencial certa — nunca fica nada de segredo cifrado
neste módulo.

## A armadilha das tags

O Kommo **substitui a lista inteira de tags** a cada alteração. Mandar só a
tag nova apaga todas as outras. Por isso `adicionar_tag` e `remover_tag`
pedem `tags_atuais`: chame `consultar_cartao_lead` antes — ele já devolve o
campo `tags_atuais` pronto, no formato que as outras duas esperam.
"""
import functools
import json
import time
import urllib.error
import urllib.parse
import urllib.request

TIMEOUT = 30


def _legivel(fn):
    """Transforma exceção em texto de resultado.

    Sem isto quem chama recebe só "Error executing tool <nome>" (ou um
    traceback cru, se for o worker a chamar direto) e perde o corpo da
    resposta do Kommo — onde vêm `path` e `code` do campo recusado. É a
    mesma falha que o `neverError` resolveu do lado do n8n: sem o detalhe, o
    modelo repete a mesma chamada errada achando que foi azar.
    """
    @functools.wraps(fn)
    def dentro(*a, **kw):
        try:
            return fn(*a, **kw)
        except Exception as e:  # noqa: BLE001 — vira resultado, não derruba
            return f"erro: {e}"
    return dentro


def _chamar(dominio: str, token: str, metodo: str, caminho: str,
            corpo=None) -> dict:
    """Uma porta só pro Kommo. Erro volta como texto legível em vez de
    exceção: quem consome é um modelo, e traceback não ajuda ele a corrigir."""
    base = (dominio or "").strip().replace("https://", "").replace("http://", "").strip("/")
    if not base:
        raise ValueError("kommo_domain vazio")
    if "." not in base:
        base = f"{base}.kommo.com"   # aceita 'empresa' e 'empresa.kommo.com'

    dados = json.dumps(corpo).encode() if corpo is not None else None
    req = urllib.request.Request(
        f"https://{base}{caminho}", data=dados, method=metodo,
        headers={"Authorization": f"Bearer {(token or '').strip()}",
                 "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            bruto = r.read()
            return json.loads(bruto) if bruto else {}
    except urllib.error.HTTPError as e:
        # O corpo do erro do Kommo traz `path` e `code` do campo exato que ele
        # recusou. Engolir isso foi o que fez o agente repetir a mesma chamada
        # errada várias vezes no n8n, antes do neverError.
        detalhe = e.read().decode("utf-8", "replace")[:600]
        raise RuntimeError(f"Kommo respondeu {e.code}: {detalhe}") from None
    except urllib.error.URLError as e:
        raise RuntimeError(f"não consegui falar com o Kommo: {e.reason}") from None


def _tags_do_lead(lead: dict) -> list:
    return [t.get("name", "") for t in
            ((lead.get("_embedded") or {}).get("tags") or []) if t.get("name")]


def juntar_tags(atuais: str, nova: str) -> list:
    """Lista final ao ADICIONAR, sem duplicar e preservando a ordem.

    Separado em função porque é a parte que erra: o PATCH do Kommo troca a
    lista inteira, então o que sai daqui é o que o lead passa a ter."""
    nomes = [t.strip() for t in f"{atuais or ''},{nova or ''}".split(",")]
    final, vistos = [], set()
    for n in nomes:
        if n and n.lower() not in vistos:
            vistos.add(n.lower())
            final.append(n)
    return final


def tirar_tag(atuais: str, remover: str) -> list:
    """Lista final ao REMOVER. Compara sem diferenciar maiúscula."""
    alvo = (remover or "").strip().lower()
    return [t.strip() for t in (atuais or "").split(",")
            if t.strip() and t.strip().lower() != alvo]


@_legivel
def consultar_cartao_lead(kommo_domain: str, access_token: str, lead_id: str) -> str:
    """Consulta o cartão completo de um lead no Kommo: campos personalizados,
    tags, etapa do funil (status_id), funil (pipeline_id) e contato principal.
    Use SEMPRE antes de adicionar_tag ou remover_tag, pra saber exatamente
    quais tags o lead já tem."""
    d = _chamar(kommo_domain, access_token, "GET",
                f"/api/v4/leads/{urllib.parse.quote(str(lead_id))}?with=contacts")
    # devolve também as tags já achatadas: é o formato que adicionar_tag e
    # remover_tag pedem em `tags_atuais`, e poupa o modelo de montar a string
    d["tags_atuais"] = ", ".join(_tags_do_lead(d))
    return json.dumps(d, ensure_ascii=False)


@_legivel
def mover_lead(kommo_domain: str, access_token: str, lead_id: str,
               status_id: str, pipeline_id: str = "") -> str:
    """Move um lead para outra etapa do funil. Informe status_id (obrigatório)
    e pipeline_id apenas se estiver mudando o lead para outro funil (deixe
    vazio pra mover só a etapa dentro do funil atual)."""
    corpo = {"status_id": int(status_id)}
    if str(pipeline_id or "").strip():
        corpo["pipeline_id"] = int(pipeline_id)
    d = _chamar(kommo_domain, access_token, "PATCH",
                f"/api/v4/leads/{urllib.parse.quote(str(lead_id))}", corpo)
    return json.dumps(d, ensure_ascii=False)


@_legivel
def atualizar_campo_personalizado(kommo_domain: str, access_token: str,
                                  lead_id: str, field_id: str, valor: str) -> str:
    """Atualiza UM campo personalizado do cartão do lead (informe field_id e
    valor). Não mexe em tags nem na etapa do funil. Pra atualizar vários
    campos, chame uma vez para cada."""
    corpo = {"custom_fields_values": [
        {"field_id": int(field_id), "values": [{"value": valor}]}]}
    d = _chamar(kommo_domain, access_token, "PATCH",
                f"/api/v4/leads/{urllib.parse.quote(str(lead_id))}", corpo)
    return json.dumps(d, ensure_ascii=False)


@_legivel
def adicionar_tag(kommo_domain: str, access_token: str, lead_id: str,
                  tag_novo: str, tags_atuais: str = "") -> str:
    """Adiciona uma tag ao lead, preservando as que já existem. IMPORTANTE: o
    Kommo substitui a lista inteira de tags a cada chamada — chame
    consultar_cartao_lead ANTES e informe todas as atuais em tags_atuais,
    separadas por vírgula."""
    final = juntar_tags(tags_atuais, tag_novo)
    d = _chamar(kommo_domain, access_token, "PATCH",
                f"/api/v4/leads/{urllib.parse.quote(str(lead_id))}",
                {"_embedded": {"tags": [{"name": n} for n in final]}})
    return json.dumps({"tags_agora": final, "resposta": d}, ensure_ascii=False)


@_legivel
def remover_tag(kommo_domain: str, access_token: str, lead_id: str,
                tag_remover: str, tags_atuais: str = "") -> str:
    """Remove uma tag do lead, preservando as demais. IMPORTANTE: o Kommo
    substitui a lista inteira de tags a cada chamada — chame
    consultar_cartao_lead ANTES e informe todas as atuais em tags_atuais."""
    final = tirar_tag(tags_atuais, tag_remover)
    d = _chamar(kommo_domain, access_token, "PATCH",
                f"/api/v4/leads/{urllib.parse.quote(str(lead_id))}",
                {"_embedded": {"tags": [{"name": n} for n in final]}})
    return json.dumps({"tags_agora": final, "resposta": d}, ensure_ascii=False)


@_legivel
def adicionar_nota(kommo_domain: str, access_token: str, lead_id: str,
                   texto: str) -> str:
    """Adiciona uma nota/comentário interno no cartão do lead — visível pra
    quem abrir o lead no Kommo, mas NUNCA enviada pro lead. Use pra registrar
    resumo de qualificação ou observação importante da conversa."""
    d = _chamar(kommo_domain, access_token, "POST",
                f"/api/v4/leads/{urllib.parse.quote(str(lead_id))}/notes",
                [{"note_type": "common", "params": {"text": texto}}])
    return json.dumps(d, ensure_ascii=False)


@_legivel
def criar_tarefa(kommo_domain: str, access_token: str, lead_id: str,
                 texto: str, prazo_horas: int = 24) -> str:
    """Cria uma tarefa no Kommo vinculada ao lead, com prazo — é como avisar de
    verdade a pessoa pra assumir, em vez de só deixar o resumo escrito na
    conversa. Use ao final de uma qualificação, ou quando o lead precisar de
    ação humana marcada (ligar, enviar orçamento, confirmar pagamento)."""
    horas = int(prazo_horas or 24)
    corpo = [{
        "text": texto,
        "complete_till": int(time.time()) + horas * 3600,
        "entity_id": int(lead_id),
        "entity_type": "leads",
    }]
    d = _chamar(kommo_domain, access_token, "POST", "/api/v4/tasks", corpo)
    return json.dumps(d, ensure_ascii=False)
