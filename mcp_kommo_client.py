"""Cliente MCP pro Kommo MCP hospedado dentro do próprio n8n (workflow
"Kommo MCP - Completo (Funil, Etapas, Leads, Contatos)", id `bSZFMIchpke716zJ`,
node `Kommo MCP Server` com `path: "kommo-completo"`) — decisão registrada no
README: nunca construir uma integração direta com o Kommo aqui, sempre
proxiar por esse MCP já existente.

Descoberta (18/09/2026, testado ao vivo): o node MCP não tem autenticação
própria — `GET {N8N_URL}/mcp/kommo-completo/sse` responde 200 direto, sem
token. Cada ferramenta do Kommo pede `kommo_domain`/`access_token` como
PARÂMETRO da chamada (é multi-tenant por design) — nada fixo aqui, quem chama
(agente_chat.py) passa o domínio/token do cliente atual em cada chamada.
"""
import asyncio
import json
import pathlib
import sys

RAIZ = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(RAIZ / ".pylibs"))

from config import carregar_env  # noqa: E402

from mcp import ClientSession  # noqa: E402
from mcp.client.sse import sse_client  # noqa: E402




def _url_mcp() -> str:
    return carregar_env()["N8N_URL"].rstrip("/") + "/mcp/kommo-completo/sse"


async def _listar_ferramentas_async() -> list:
    async with sse_client(_url_mcp()) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            resultado = await session.list_tools()
            return [
                {"name": t.name, "description": t.description, "input_schema": t.input_schema}
                for t in resultado.tools
            ]


def listar_ferramentas() -> list:
    return asyncio.run(_listar_ferramentas_async())


async def _chamar_ferramenta_async(nome: str, argumentos: dict) -> str:
    async with sse_client(_url_mcp()) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            resultado = await session.call_tool(nome, argumentos)
            partes = [c.text for c in resultado.content if hasattr(c, "text")]
            return "\n".join(partes) if partes else str(resultado.content)


def chamar_ferramenta(nome: str, argumentos: dict) -> str:
    """Abre uma sessão MCP nova por chamada — simples e robusto (o custo de
    reabrir a conexão a cada tool-call é aceitável pro volume de um chat)."""
    return asyncio.run(_chamar_ferramenta_async(nome, argumentos))


def validar_credenciais(subdominio: str, token: str) -> dict:
    """Confere se subdomínio+token do Kommo realmente funcionam, listando os
    funis (só leitura, não altera nada na conta).

    Sem isso um token errado só aparece quando um paciente manda mensagem e o
    agente falha calado — o painel diria "clonado com sucesso" do mesmo jeito.

    Formatos observados no MCP em 21/09/2026:
        erro    -> {"error": {"message": "...", "name": "NodeApiError"}}
        sucesso -> [{"data": "<string JSON com _embedded.pipelines>"}]
    """
    dominio = f"{subdominio}.kommo.com"
    try:
        bruto = chamar_ferramenta(
            "kommo_listar_funis", {"kommo_domain": dominio, "access_token": token}
        )
    except Exception as e:  # noqa: BLE001 — MCP fora do ar / rede
        return {"ok": False, "mensagem": f"não consegui falar com o MCP do Kommo: {e}", "funis": None}

    try:
        dados = json.loads(bruto)
    except json.JSONDecodeError:
        return {"ok": False, "mensagem": f"resposta inesperada do Kommo: {bruto[:200]}", "funis": None}

    if isinstance(dados, dict) and dados.get("error"):
        erro = dados["error"]
        msg = erro.get("message") if isinstance(erro, dict) else str(erro)
        return {"ok": False, "mensagem": msg or "credenciais recusadas pelo Kommo", "funis": None}

    # Sucesso: conta os funis só pra dar um retorno útil ("conectado, 12 funis").
    funis = None
    try:
        if isinstance(dados, list) and dados and isinstance(dados[0], dict):
            interno = json.loads(dados[0].get("data", "{}"))
            funis = len(interno.get("_embedded", {}).get("pipelines", []))
    except (json.JSONDecodeError, AttributeError, TypeError):
        pass  # contar é bônus — o que importa é não ter vindo erro

    return {"ok": True, "mensagem": "conexão com o Kommo confirmada", "funis": funis}


if __name__ == "__main__":
    for f in listar_ferramentas():
        print(f["name"], "-", (f["description"] or "")[:70])
