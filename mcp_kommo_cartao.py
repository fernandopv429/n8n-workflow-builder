"""MCP do Cartão do Lead (Kommo) — servidor local.

Casca fina sobre `kommo_cartao.py`: a lógica de verdade (as 7 ferramentas,
chamada HTTP pro Kommo) mora lá, sem nenhuma dependência de MCP — é o que
permite `worker_agente.py` chamar essas mesmas funções direto, em processo,
sem abrir sessão MCP nenhuma (30/10/2026: é a maioria do tráfego de
ferramenta do atendimento, e cada chamada via MCP pagava handshake +
negociação de transporte só pra no fim fazer a mesma requisição HTTP que
está aqui dentro).

Este arquivo só existe pra quem precisa de verdade do protocolo MCP: outro
processo, outra máquina, ou Claude Desktop/Code via stdio.

## Instalação

    pip install --target .pylibs mcp uvicorn starlette

## Como rodar

    python3 mcp_kommo_cartao.py                      # stdio (Claude Code, Claude Desktop)
    python3 mcp_kommo_cartao.py --http               # só nesta máquina, 127.0.0.1:8765
    python3 mcp_kommo_cartao.py --http --rede        # acessível na rede, exige token

## Na rede, token é obrigatório

Quem alcança a porta manda no Kommo de todos os clientes — `--rede` sem token
não sobe, de propósito. O token vem de `MCP_TOKEN`:

    MCP_TOKEN=$(python3 -c "import secrets;print(secrets.token_urlsafe(32))")
    MCP_TOKEN=$MCP_TOKEN python3 mcp_kommo_cartao.py --http --rede

Quem chama manda `Authorization: Bearer <token>` — mesmo esquema dos MCPs do
n8n, então o painel aceita a URL sem adaptação.

Em `127.0.0.1` o token é opcional: ali só alcança quem já está na máquina.
"""
import argparse
import os
import pathlib
import secrets
import sys

RAIZ = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(RAIZ / ".pylibs"))
sys.path.insert(0, str(RAIZ))

import uvicorn  # noqa: E402
from mcp.server.mcpserver import MCPServer  # noqa: E402
from mcp.server.transport_security import (  # noqa: E402
    TransportSecuritySettings,
)
from starlette.middleware.base import BaseHTTPMiddleware  # noqa: E402
from starlette.responses import JSONResponse  # noqa: E402

import kommo_cartao  # noqa: E402

servidor = MCPServer(
    name="kommo-cartao-lead-local",
    instructions=(
        "Ferramentas do cartão do lead no Kommo. Toda chamada exige "
        "kommo_domain e access_token da conta. Antes de mexer em tags, chame "
        "consultar_cartao_lead: o Kommo substitui a lista inteira a cada "
        "alteração."
    ),
)

# As 7 ferramentas vêm prontas de kommo_cartao.py (lógica + @_legivel já
# aplicado) — aqui só registramos cada uma no servidor MCP, sem reescrever
# nada. `servidor.tool()` preserva nome/docstring/assinatura da função
# original, então a descrição que o modelo vê continua a mesma.
consultar_cartao_lead = servidor.tool()(kommo_cartao.consultar_cartao_lead)
mover_lead = servidor.tool()(kommo_cartao.mover_lead)
atualizar_campo_personalizado = servidor.tool()(kommo_cartao.atualizar_campo_personalizado)
adicionar_tag = servidor.tool()(kommo_cartao.adicionar_tag)
remover_tag = servidor.tool()(kommo_cartao.remover_tag)
adicionar_nota = servidor.tool()(kommo_cartao.adicionar_nota)
criar_tarefa = servidor.tool()(kommo_cartao.criar_tarefa)

# Compatibilidade com quem importava utilitário daqui (ex: testes.py).
juntar_tags = kommo_cartao.juntar_tags
tirar_tag = kommo_cartao.tirar_tag
_legivel = kommo_cartao._legivel


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
