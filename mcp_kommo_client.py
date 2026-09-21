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


if __name__ == "__main__":
    for f in listar_ferramentas():
        print(f["name"], "-", (f["description"] or "")[:70])
