"""Cliente MCP genérico: fala com QUALQUER MCP hospedado no n8n.

O `mcp_kommo_client.py` nasceu preso num caminho só (`/mcp/kommo-completo/sse`).
Mas a instância já tem oito MCPs — agendamento, Clinicorp, bases de informação
por cliente — e a ideia é que integração nova vire MCP novo no n8n, sem código
aqui (ver ARQUITETURA-AGENTE.md). Pra isso o cliente precisa receber a URL.

## Dois tipos de MCP, confirmados na instância em 07/10/2026

**Multi-inquilino** (`kommo-completo`): as ferramentas pedem `kommo_domain` e
`access_token` como PARÂMETRO, e o mesmo MCP serve todos os clientes. Quem
chama injeta a credencial do cliente atual.

**De um cliente só** (`informacoes-performance`, `clinicorp-agenda-evandro`):
a credencial mora dentro do próprio MCP, nos nodes. Não há nada a injetar — e
por isso cada um desses só pode ser ligado ao cliente a que pertence.

A diferença some aqui: quem chama passa `credenciais` quando houver, e o resto
é igual.

## Nomes de ferramenta

Dois MCPs podem ter `buscar_horarios`. Pro modelo, nome repetido é ambiguidade
— ele chama um achando que é o outro. Por isso `listar_ferramentas` devolve o
nome prefixado pelo apelido do MCP (`agenda__buscar_horarios`), e
`chamar_ferramenta` desfaz o prefixo antes de mandar pro n8n.
"""
import asyncio
import pathlib
import sys

RAIZ = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(RAIZ / ".pylibs"))

from config import carregar_env  # noqa: E402

from mcp import ClientSession  # noqa: E402
from mcp.client.sse import sse_client  # noqa: E402
from mcp.client.streamable_http import (  # noqa: E402
    create_mcp_http_client,
    streamable_http_client,
)

SEPARADOR = "__"


class McpIndisponivel(RuntimeError):
    """MCP fora do ar, sem autorização ou caminho errado. Vira resultado de
    ferramenta pro modelo, não exceção que derruba o atendimento."""


# O node mcpTrigger tem duas versões na instância, com TRANSPORTES diferentes
# (descoberto em 07/10/2026 — os MCPs v2 davam 404 no caminho do v1.1):
#
#   typeVersion 1.1  ->  SSE               ->  /mcp/<path>/sse
#   typeVersion 2    ->  Streamable HTTP   ->  /mcp/<path>
#
# Em vez de guardar a versão em cada registro — informação que envelhece quando
# alguém atualiza o node no n8n — tentamos o moderno e caímos no antigo. Custa
# uma requisição a mais só quando o MCP é antigo.
def url_do_path(path: str, sse: bool = False) -> str:
    """Guardamos o path, não a URL inteira: se o n8n mudar de domínio, muda só
    a variável de ambiente e nenhum registro de cliente precisa ser editado."""
    base = carregar_env()["N8N_URL"].rstrip("/")
    caminho = f"{base}/mcp/{path.strip('/')}"
    return f"{caminho}/sse" if sse else caminho


def _cabecalhos(token: str = "") -> dict:
    return {"Authorization": f"Bearer {token}"} if token else {}


async def _com_sessao(path: str, token: str, acao):
    """Abre a sessão no transporte certo e executa `acao(session)`.

    Tenta Streamable HTTP (node v2) e cai pro SSE (v1.1) quando o moderno não
    responde. O erro guardado é o do SSE: é o que importa quando os dois falham,
    porque o 404 do primeiro só diz "este MCP não é v2".
    """
    cab = _cabecalhos(token)
    # O resultado é guardado FORA do `async with`: estes transportes costumam
    # levantar no fechamento do contexto, depois de a ação já ter dado certo.
    # Um `except` em volta do bloco inteiro jogava o resultado bom fora e caía
    # pro SSE, que então dava 404 — e o MCP v2 parecia estar fora do ar.
    obtido = []
    try:
        # Nesta versão do SDK o `streamable_http_client` NÃO aceita `headers` —
        # o cabeçalho vai no cliente HTTP que a gente passa. Chamar com
        # headers= levanta TypeError ANTES de tentar a conexão, o que fazia todo
        # MCP v2 parecer fora do ar.
        async with create_mcp_http_client(headers=cab) as http:
            async with streamable_http_client(url_do_path(path), http_client=http) as fluxos:
                async with ClientSession(fluxos[0], fluxos[1]) as s:
                    await s.initialize()
                    obtido.append(await acao(s))
    except Exception:
        if obtido:
            return obtido[0]
        async with sse_client(url_do_path(path, sse=True), headers=cab) as (read, write):
            async with ClientSession(read, write) as s:
                await s.initialize()
                return await acao(s)
    return obtido[0]


async def _listar_async(path: str, token: str) -> list:
    async def acao(s):
        r = await s.list_tools()
        return [{"name": t.name, "description": t.description,
                 "input_schema": t.input_schema} for t in r.tools]
    return await _com_sessao(path, token, acao)


async def _chamar_async(path: str, token: str, nome: str, args: dict) -> str:
    async def acao(s):
        r = await s.call_tool(nome, args)
        partes = [c.text for c in r.content if hasattr(c, "text")]
        return "\n".join(partes) if partes else str(r.content)
    return await _com_sessao(path, token, acao)


def listar_ferramentas(path: str, token: str = "", apelido: str = "") -> list:
    """Ferramentas de um MCP, com o nome prefixado pelo apelido.

    Erro vira McpIndisponivel com o motivo legível: um MCP fora do ar não pode
    derrubar o carregamento dos outros nem o atendimento inteiro.
    """
    try:
        ferramentas = asyncio.run(_listar_async(path, token))
    except Exception as e:  # noqa: BLE001 — rede, 403, path errado
        raise McpIndisponivel(f"MCP '{path}': {type(e).__name__}: {str(e)[:160]}") from None
    if apelido:
        for f in ferramentas:
            f["name"] = f"{apelido}{SEPARADOR}{f['name']}"
    return ferramentas


def chamar_ferramenta(path: str, nome: str, argumentos: dict, token: str = "",
                      apelido: str = "", credenciais: dict | None = None) -> str:
    """Chama a ferramenta. `nome` pode vir prefixado pelo apelido.

    `credenciais` é o que o MCP multi-inquilino espera como parâmetro (no Kommo,
    kommo_domain/access_token). MCP de cliente único não recebe nada — passar
    sobra não quebra, mas é ruído na chamada.
    """
    if apelido and nome.startswith(f"{apelido}{SEPARADOR}"):
        nome = nome[len(apelido) + len(SEPARADOR):]
    args = {**(argumentos or {}), **(credenciais or {})}
    try:
        return asyncio.run(_chamar_async(path, token, nome, args))
    except Exception as e:  # noqa: BLE001
        raise McpIndisponivel(f"MCP '{path}', ferramenta '{nome}': {str(e)[:160]}") from None


def catalogo() -> list:
    """Todos os MCPs que existem no n8n, descobertos pelos próprios workflows.

    Evita digitar caminho na tela — e digitar caminho erra. O n8n é a fonte da
    verdade: MCP novo aparece aqui sozinho, MCP renomeado aparece com o nome
    novo.

    Também devolve `autenticado`, porque hoje só o do Kommo exige token: os
    outros respondem pra qualquer um que saiba a URL, e isso precisa ficar
    visível na hora de escolher.
    """
    from n8n_client import N8nClient

    cliente = N8nClient()
    achados = []
    for w in cliente._request("GET", "/workflows?limit=250").get("data", []):
        try:
            wf = cliente.get_workflow(w["id"])
        except Exception:  # noqa: BLE001 — um workflow ilegível não derruba a lista
            continue
        for n in wf.get("nodes", []):
            if "mcpTrigger" not in n.get("type", ""):
                continue
            achados.append({
                "nome": w["name"],
                "path": (n.get("parameters") or {}).get("path", ""),
                "ativo": bool(wf.get("active")),
                "autenticado": bool((n.get("parameters") or {}).get("authentication")),
            })
    return sorted(achados, key=lambda x: x["nome"])


def testar(path: str, token: str = "") -> dict:
    """Diagnóstico pro painel: o MCP responde? quantas ferramentas tem?

    Devolve dicionário em vez de levantar exceção — isto alimenta uma tela, e
    "não respondeu" é informação, não erro do painel.
    """
    try:
        fs = listar_ferramentas(path, token)
    except McpIndisponivel as e:
        return {"ok": False, "motivo": str(e), "ferramentas": []}
    return {"ok": True, "quantidade": len(fs),
            "ferramentas": [f["name"] for f in fs]}


if __name__ == "__main__":  # diagnóstico
    import json
    alvo = sys.argv[1] if len(sys.argv) > 1 else "kommo-completo"
    tok = carregar_env().get("MCP_KOMMO_TOKEN", "") if alvo == "kommo-completo" else ""
    print(json.dumps(testar(alvo, tok), ensure_ascii=False, indent=2))
