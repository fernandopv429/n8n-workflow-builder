"""MCP do Cartão do Lead (Kommo) — servidor local.

Sete ferramentas pra mexer no cartão de um lead no Kommo: consultar, mover de
etapa, atualizar campo personalizado, adicionar e remover tag, registrar nota e
criar tarefa.

## Nenhuma credencial mora aqui

`kommo_domain` e `access_token` chegam em CADA chamada, como parâmetro. Um
servidor só atende quantas contas Kommo você quiser, e não há segredo guardado
em disco pra vazar. Quem chama é que injeta a credencial certa.

## A armadilha das tags

O Kommo **substitui a lista inteira de tags** a cada alteração. Mandar só a tag
nova apaga todas as outras. Por isso `adicionar_tag` e `remover_tag` pedem
`tags_atuais`: chame `consultar_cartao_lead` antes — ele já devolve o campo
`tags_atuais` pronto, no formato que as outras duas esperam.

## Instalação

    pip install --target .pylibs mcp

## Como rodar

    python3 servidor.py                      # stdio (Claude Code, Claude Desktop)
    python3 servidor.py --http               # só nesta máquina, 127.0.0.1:8765
    python3 servidor.py --http --rede        # acessível na rede, exige token

## Na rede, token é obrigatório

Quem alcança a porta manda no Kommo de todos os clientes — `--rede` sem token
não sobe, de propósito. O token vem de `MCP_TOKEN`:

    MCP_TOKEN=$(python3 -c "import secrets;print(secrets.token_urlsafe(32))")
    MCP_TOKEN=$MCP_TOKEN python3 servidor.py --http --rede

Quem chama manda `Authorization: Bearer <token>` — mesmo esquema dos MCPs do
n8n, então o painel aceita a URL sem adaptação.

Em `127.0.0.1` o token é opcional: ali só alcança quem já está na máquina.
"""
import argparse
import functools
import json
import os
import secrets
import pathlib
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

RAIZ = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(RAIZ / ".pylibs"))

import uvicorn  # noqa: E402
from mcp.server.mcpserver import MCPServer  # noqa: E402
from mcp.server.transport_security import (  # noqa: E402
    TransportSecuritySettings,
)
from starlette.middleware.base import BaseHTTPMiddleware  # noqa: E402
from starlette.responses import JSONResponse  # noqa: E402

TIMEOUT = 30

servidor = MCPServer(
    name="kommo-cartao-lead-local",
    instructions=(
        "Ferramentas do cartão do lead no Kommo. Toda chamada exige "
        "kommo_domain e access_token da conta. Antes de mexer em tags, chame "
        "consultar_cartao_lead: o Kommo substitui a lista inteira a cada "
        "alteração."
    ),
)


def _legivel(fn):
    """Transforma exceção em texto de resultado.

    Sem isto o SDK devolve só "Error executing tool <nome>" e engole o corpo da
    resposta do Kommo — que é onde vêm `path` e `code` do campo recusado. É
    exatamente a falha que o `neverError` resolveu do lado do n8n: sem o
    detalhe, o modelo repete a mesma chamada errada achando que foi azar.
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
    lista inteira, então o que sai daqui é o que o lead passa a ter.
    """
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


@servidor.tool()
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


@servidor.tool()
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


@servidor.tool()
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


@servidor.tool()
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


@servidor.tool()
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


@servidor.tool()
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


@servidor.tool()
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


class _ExigeToken(BaseHTTPMiddleware):
    """Bearer token na frente do MCP.

    Mesmo cabeçalho que os MCPs do n8n usam (`Authorization: Bearer ...`), pra
    o painel e qualquer cliente MCP falarem com os dois sem adaptação.

    Compara com `secrets.compare_digest`: comparação comum vaza o tamanho do
    prefixo correto pelo tempo de resposta.
    """

    def __init__(self, app, token: str):
        super().__init__(app)
        self.token = token

    async def dispatch(self, request, call_next):
        enviado = request.headers.get("authorization", "")
        prefixo = "bearer "
        bruto = enviado[len(prefixo):] if enviado.lower().startswith(prefixo) else ""
        if not secrets.compare_digest(bruto, self.token):
            return JSONResponse({"error": "token inválido ou ausente"}, status_code=401)
        return await call_next(request)


if __name__ == "__main__":
    p = argparse.ArgumentParser(description="MCP do Cartão do Lead (Kommo)")
    p.add_argument("--http", action="store_true",
                   help="sobe em HTTP em vez de stdio")
    p.add_argument("--porta", type=int, default=8765)
    p.add_argument("--rede", action="store_true",
                   help="aceita conexão de outras máquinas (exige MCP_TOKEN)")
    p.add_argument("--host", default="",
                   help="endereço de escuta; o padrão vem de --rede")
    p.add_argument("--aceitar", nargs="*", default=[],
                   help="outros nomes/IPs pelos quais este servidor é chamado "
                        "(ex: o IP da LAN, um hostname). Necessário porque o "
                        "SDK valida o cabeçalho Host contra uma lista.")
    args = p.parse_args()

    if not args.http:
        servidor.run()
        raise SystemExit

    host = args.host or ("0.0.0.0" if args.rede else "127.0.0.1")
    token = os.environ.get("MCP_TOKEN", "").strip()
    na_rede = host not in ("127.0.0.1", "localhost", "::1")

    # Falha fechada: subir na rede sem token entregaria o Kommo de todos os
    # clientes a quem alcançasse a porta. Melhor não subir do que subir aberto
    # — aberto ninguém percebe até ser tarde.
    if na_rede and not token:
        print("ERRO: --rede exige um token. Gere e exporte antes:\n"
              '  export MCP_TOKEN=$(python3 -c "import secrets;print(secrets.token_urlsafe(32))")',
              file=sys.stderr)
        raise SystemExit(2)

    # O SDK traz proteção contra DNS rebinding e só aceita `Host: localhost`
    # por padrão — na rede, ela derruba a sessão DEPOIS de autenticar, com
    # "Invalid Host header" só no log do servidor. Quem chama vê uma falha
    # muda. Liberar o host/IP em que estamos escutando é o que falta; a
    # proteção continua de pé pros demais.
    seguranca = TransportSecuritySettings(
        allowed_hosts=[f"{host}:{args.porta}", f"{host}", "localhost",
                       f"localhost:{args.porta}", f"127.0.0.1:{args.porta}"]
        + [f"{e}:{args.porta}" for e in args.aceitar],
        allowed_origins=["*"],
    )
    app = servidor.streamable_http_app(transport_security=seguranca)
    if token:
        app.add_middleware(_ExigeToken, token=token)

    print(f"MCP em http://{host}:{args.porta}/mcp"
          f" ({'com token' if token else 'SEM token, só nesta máquina'})",
          file=sys.stderr)
    uvicorn.run(app, host=host, port=args.porta, log_level="warning")
