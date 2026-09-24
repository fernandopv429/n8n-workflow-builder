#!/usr/bin/env python3
"""Backend web da ferramenta de clonagem de agente de IA no n8n.

    python3 servidor.py                 # sobe em 0.0.0.0:8099
    python3 servidor.py --porta 9001

Biblioteca padrão só (mesmo padrão de catalogo-brtw/servidor_pagamento.py —
sem framework). Fluxo (Fase 2 do plano):

    GET  /saude                     liveness/readiness — ÚNICA rota sem auth
    GET  /                        index.html
    GET  /config                   {"nichos": {...}, "origens": [...]}
    GET  /clientes                  lista clientes (dashboard)
    POST /clientes                  cria cliente "rascunho" (passo 1: nome+nicho)
    GET  /clientes/<id>              detalhe de um cliente
    GET  /clientes/<id>/logs          últimas entradas de log desse cliente
    PUT  /clientes/<id>/credenciais    grava Kommo + chave OpenAI e dispara a
                                       clonagem real no n8n (ou só simula, com
                                       dry_run) — a credencial OpenAI é CRIADA
                                       no n8n nesse passo, não selecionada de
                                       uma lista.
    GET  /clientes/<id>/mensagens      histórico do chat
    POST /clientes/<id>/chat           manda mensagem pro chat (agente_chat.py)

Todas as rotas (menos /saude) exigem basic auth — este sistema edita workflow
n8n e CRM de cliente real, e gasta crédito da OpenAI. Sem PAINEL_SENHA
configurada, o servidor recusa tudo (falha fechada, igual à API de ingestão do
projeto Petição).
"""
import argparse
import base64
import hmac
import json
import os
import pathlib
import re
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

RAIZ = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(RAIZ / ".pylibs"))
sys.path.insert(0, str(RAIZ))

import agente_chat  # noqa: E402
import briefing_batch  # noqa: E402
import db  # noqa: E402
import mcp_kommo_client  # noqa: E402
import n8n_edicao  # noqa: E402
from cloner import ClonagemInvalida, clonar  # noqa: E402
from config import carregar_env  # noqa: E402
from manifest import ClienteManifest  # noqa: E402
from n8n_client import N8nError  # noqa: E402
from templates_nicho import NICHOS  # noqa: E402

ENV = carregar_env()
USUARIO_ESPERADO = ENV.get("PAINEL_USUARIO", "admin").strip()
SENHA_ESPERADA = ENV.get("PAINEL_SENHA", "").strip()

# O schema é criado na subida, mas um banco fora do ar não pode derrubar o
# container em loop — /saude reporta o problema e o Coolify mostra como não
# saudável, o que é mais fácil de diagnosticar que um crash-loop.
try:
    db.garantir_schema()
except Exception as e:  # noqa: BLE001
    sys.stderr.write(f"[servidor] AVISO: falha ao garantir schema no banco: {e}\n")

ROTA_CLIENTE_ID = re.compile(r"^/clientes/(\d+)$")
ROTA_CLIENTE_LOGS = re.compile(r"^/clientes/(\d+)/logs$")
ROTA_CLIENTE_CREDENCIAIS = re.compile(r"^/clientes/(\d+)/credenciais$")
ROTA_CLIENTE_MENSAGENS = re.compile(r"^/clientes/(\d+)/mensagens$")
ROTA_CLIENTE_CHAT = re.compile(r"^/clientes/(\d+)/chat$")


class Handler(BaseHTTPRequestHandler):
    def log_message(self, formato, *args):
        sys.stderr.write(f"[servidor] {self.address_string()} - {formato % args}\n")

    def _responder_json(self, status: int, corpo: dict):
        payload = json.dumps(corpo, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _ler_corpo(self) -> dict:
        tamanho = int(self.headers.get("Content-Length", 0))
        bruto = self.rfile.read(tamanho) if tamanho else b"{}"
        return json.loads(bruto)

    # --- autenticação -------------------------------------------------

    def _credencial_confere(self) -> bool:
        if not SENHA_ESPERADA:
            return False  # falha fechada: sem senha configurada, ninguém entra
        cabecalho = self.headers.get("Authorization", "")
        if not cabecalho.startswith("Basic "):
            return False
        try:
            usuario, _, senha = base64.b64decode(cabecalho[6:]).decode("utf-8").partition(":")
        except Exception:  # noqa: BLE001 — header malformado é só credencial inválida
            return False
        # compare_digest nos dois: evita vazar o tamanho/prefixo por tempo de resposta
        return (
            hmac.compare_digest(usuario, USUARIO_ESPERADO)
            and hmac.compare_digest(senha, SENHA_ESPERADA)
        )

    def _autenticado(self) -> bool:
        """Chamada no início de todo handler. Devolve False já tendo respondido
        401 — quem chamou só precisa dar `return`."""
        if self._credencial_confere():
            return True
        if not SENHA_ESPERADA:
            sys.stderr.write("[servidor] PAINEL_SENHA não configurada — recusando tudo\n")
        corpo = b'{"error":"nao autorizado"}'
        self.send_response(401)
        self.send_header("WWW-Authenticate", 'Basic realm="Agentes de IA"')
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(corpo)))
        self.end_headers()
        self.wfile.write(corpo)
        return False

    def _servir_arquivo(self, caminho: pathlib.Path, content_type: str):
        if not caminho.exists():
            self._responder_json(404, {"error": "não encontrado"})
            return
        conteudo = caminho.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(conteudo)))
        self.end_headers()
        self.wfile.write(conteudo)

    # --- GET ---------------------------------------------------------

    def do_GET(self):
        # /saude fica ANTES da auth: o healthcheck do Coolify/Docker não tem
        # credencial, e um health que responde 401 marcaria o container como
        # quebrado mesmo estando de pé.
        if self.path == "/saude":
            try:
                db.listar_templates_nicho()
            except Exception as e:  # noqa: BLE001
                self._responder_json(503, {"status": "degradado", "banco": str(e)})
                return
            self._responder_json(200, {"status": "ok", "banco": "ok"})
            return

        if not self._autenticado():
            return

        if self.path == "/" or self.path == "/index.html":
            self._servir_arquivo(RAIZ / "index.html", "text/html; charset=utf-8")
            return
        if self.path == "/config":
            self._responder_json(200, {"nichos": NICHOS, "origens": db.listar_templates_nicho()})
            return
        if self.path == "/clientes":
            self._responder_json(200, {"clientes": db.listar_clientes()})
            return

        m = ROTA_CLIENTE_LOGS.match(self.path)
        if m:
            self._responder_json(200, {"logs": db.listar_logs(int(m.group(1)))})
            return

        m = ROTA_CLIENTE_MENSAGENS.match(self.path)
        if m:
            self._responder_json(200, {"mensagens": db.listar_mensagens(int(m.group(1)))})
            return

        m = ROTA_CLIENTE_ID.match(self.path)
        if m:
            cliente_id = int(m.group(1))
            cliente = db.obter_cliente(cliente_id)
            if cliente is None:
                self._responder_json(404, {"error": "cliente não encontrado"})
                return
            if cliente["batch_status"] == "in_progress":
                cliente = self._checar_batch(cliente_id, cliente)
            self._responder_json(200, cliente)
            return

        self._responder_json(404, {"error": "não encontrado"})

    def _checar_batch(self, cliente_id: int, cliente: dict) -> dict:
        """Poll-on-read: sem worker em segundo plano, então a checagem do lote
        só acontece quando alguém pede o detalhe desse cliente."""
        try:
            resultado = briefing_batch.verificar_batch(cliente["batch_id"])
        except Exception as e:  # noqa: BLE001 — não derruba a resposta por falha ao checar o lote
            db.registrar_log(cliente_id, "erro", f"Falha ao checar Batch API: {e}")
            return cliente

        if resultado["status"] != cliente["batch_status"]:
            db.atualizar_resultado_batch(
                cliente_id, resultado["status"], resultado.get("prompt_agente"), resultado.get("estrutura_kommo")
            )
            cliente = db.obter_cliente(cliente_id)
        return cliente

    # --- POST --------------------------------------------------------

    def do_POST(self):
        if not self._autenticado():
            return

        m = ROTA_CLIENTE_CHAT.match(self.path)
        if m:
            self._chat(int(m.group(1)))
            return

        if self.path != "/clientes":
            self._responder_json(404, {"error": "não encontrado"})
            return

        try:
            corpo = self._ler_corpo()
        except json.JSONDecodeError:
            self._responder_json(400, {"error": "JSON inválido"})
            return

        cliente_nome = str(corpo.get("cliente_nome", "")).strip()
        nicho = str(corpo.get("nicho", "")).strip()
        workflow_origem_id = str(corpo.get("workflow_origem_id", "")).strip()

        if not cliente_nome:
            self._responder_json(400, {"error": "cliente_nome é obrigatório"})
            return
        if nicho not in NICHOS:
            self._responder_json(400, {"error": f"nicho '{nicho}' desconhecido"})
            return
        template = db.obter_template_nicho(nicho)
        if template is None or template["workflow_origem_id"] != workflow_origem_id:
            self._responder_json(400, {"error": "workflow_origem_id não corresponde ao template cadastrado desse nicho"})
            return

        briefing = str(corpo.get("briefing", "")).strip()
        resposta_rapida = bool(corpo.get("resposta_rapida", False))

        batch_id = ""
        resultado_sincrono = None
        if briefing:
            try:
                if resposta_rapida:
                    resultado_sincrono = briefing_batch.processar_briefing_sincrono(nicho, cliente_nome, briefing)
                else:
                    batch_id = briefing_batch.submeter_briefing(nicho, cliente_nome, briefing)
            except Exception as e:  # noqa: BLE001 — devolve pro form em vez de criar cliente sem o que foi pedido
                self._responder_json(502, {"error": f"falha ao gerar a partir do briefing: {e}"})
                return

        cliente_id = db.criar_cliente_rascunho(cliente_nome, nicho, workflow_origem_id, briefing, batch_id)

        if resultado_sincrono:
            db.atualizar_resultado_batch(
                cliente_id, "completed",
                resultado_sincrono.get("prompt_agente"), resultado_sincrono.get("estrutura_kommo"),
            )

        self._responder_json(200, db.obter_cliente(cliente_id))

    def _atualizar_cliente_clonado(self, cliente_id: int, cliente: dict, corpo: dict):
        """Atualiza as credenciais do Kommo no workflow que já existe. A chave da
        OpenAI não é reaproveitada aqui: a credencial no n8n já foi criada na
        clonagem, e trocá-la exigiria criar outra (sobra credencial órfã)."""
        workflow_id = cliente["workflow_novo_id"]
        alterados = []
        try:
            for campo, chave_corpo in (("base-url", "kommo_subdominio"), ("kommo-token", "kommo_token")):
                valor = str(corpo.get(chave_corpo, "")).strip()
                if valor and valor != n8n_edicao.ler_campo_database(workflow_id, campo):
                    n8n_edicao.atualizar_campo_database(workflow_id, campo, valor)
                    alterados.append(campo)
        except Exception as e:  # noqa: BLE001
            db.registrar_log(cliente_id, "erro", f"Falha ao atualizar credenciais: {e}")
            self._responder_json(502, {"error": f"falha ao atualizar: {e}"})
            return

        if alterados:
            db.registrar_log(cliente_id, "sistema", f"Credenciais atualizadas no workflow existente: {', '.join(alterados)}.")
        else:
            db.registrar_log(cliente_id, "sistema", "Nada a atualizar — as credenciais enviadas já são as do workflow.")

        self._responder_json(200, {
            "dry_run": False,
            "workflow_id": workflow_id,
            "atualizado": True,
            "campos_alterados": alterados,
        })

    def _chat(self, cliente_id: int):
        try:
            corpo = self._ler_corpo()
        except json.JSONDecodeError:
            self._responder_json(400, {"error": "JSON inválido"})
            return

        texto = str(corpo.get("mensagem", "")).strip()
        if not texto:
            self._responder_json(400, {"error": "mensagem é obrigatória"})
            return

        try:
            resposta = agente_chat.processar_mensagem(cliente_id, texto)
        except ValueError as e:
            self._responder_json(400, {"error": str(e)})
            return
        except Exception as e:  # noqa: BLE001 — não derruba o processo por erro de chat
            db.registrar_log(cliente_id, "erro", f"Chat falhou: {e}")
            self._responder_json(500, {"error": f"erro inesperado: {e}"})
            return

        self._responder_json(200, {"resposta": resposta})

    # --- PUT ---------------------------------------------------------

    def do_PUT(self):
        if not self._autenticado():
            return

        m = ROTA_CLIENTE_CREDENCIAIS.match(self.path)
        if not m:
            self._responder_json(404, {"error": "não encontrado"})
            return
        cliente_id = int(m.group(1))

        cliente = db.obter_cliente(cliente_id)
        if cliente is None:
            self._responder_json(404, {"error": "cliente não encontrado"})
            return

        try:
            corpo = self._ler_corpo()
        except json.JSONDecodeError:
            self._responder_json(400, {"error": "JSON inválido"})
            return

        dry_run = bool(corpo.get("dry_run", True))
        forcar_novo = bool(corpo.get("forcar_novo", False))

        # Conexão com o Kommo é testada ANTES de gravar qualquer coisa: com
        # token errado o agente é criado, o painel diz "sucesso" e a falha só
        # aparece quando um paciente manda mensagem. Só leitura (lista funis).
        subdominio = str(corpo.get("kommo_subdominio", "")).strip()
        token = str(corpo.get("kommo_token", "")).strip()
        if subdominio and token:
            checagem = mcp_kommo_client.validar_credenciais(subdominio, token)
            if not checagem["ok"]:
                db.registrar_log(cliente_id, "erro", f"Credenciais do Kommo recusadas: {checagem['mensagem']}")
                self._responder_json(400, {
                    "error": f"Kommo recusou a conexão: {checagem['mensagem']}",
                    "kommo_invalido": True,
                })
                return
            funis = checagem.get("funis")
            db.registrar_log(
                cliente_id, "sistema",
                f"Conexão com o Kommo '{subdominio}' confirmada"
                + (f" ({funis} funis na conta)." if funis is not None else "."),
            )

        # Cliente já clonado: por padrão ATUALIZA o workflow existente em vez de
        # clonar de novo. Sem isso, clicar "Salvar e clonar" duas vezes cria um
        # segundo workflow que colide no path do webhook contra o primeiro — foi
        # o que gerou 7 órfãos e uma sequência de 409 no 'Will teste'
        # (21/09/2026). Quem quiser mesmo um segundo agente marca `forcar_novo`.
        ja_tem_agente = cliente["status"] == "clonado" and cliente["workflow_novo_id"]
        if ja_tem_agente and not forcar_novo:
            self._atualizar_cliente_clonado(cliente_id, cliente, corpo)
            return
        if ja_tem_agente and forcar_novo and not dry_run:
            db.registrar_log(
                cliente_id, "sistema",
                f"Criando agente NOVO a pedido — o anterior ({cliente['workflow_novo_id']}) "
                "continua no n8n e deixa de ser o agente deste cliente no painel.",
            )

        dados_manifesto = {
            "cliente_nome": cliente["cliente_nome"],
            "nicho": cliente["nicho"],
            "workflow_origem_id": cliente["workflow_origem_id"],
            "kommo_subdominio": corpo.get("kommo_subdominio", ""),
            "kommo_token": corpo.get("kommo_token", ""),
            "openai_api_key": corpo.get("openai_api_key", ""),
        }
        try:
            manifesto = ClienteManifest.de_dict(dados_manifesto)
        except ValueError as e:
            self._responder_json(400, {"error": str(e)})
            return

        try:
            resultado = clonar(manifesto, dry_run=dry_run)
        except ClonagemInvalida as e:
            if not dry_run:
                db.marcar_cliente_erro(cliente_id, str(e))
            self._responder_json(422, {"error": str(e)})
            return
        except N8nError as e:
            if not dry_run:
                db.marcar_cliente_erro(cliente_id, f"n8n: {e}")
            self._responder_json(502, {"error": f"n8n: {e}"})
            return
        except Exception as e:  # noqa: BLE001 — devolve pro form em vez de derrubar o processo
            if not dry_run:
                db.marcar_cliente_erro(cliente_id, f"erro inesperado: {e}")
            self._responder_json(500, {"error": f"erro inesperado: {e}"})
            return

        # Detalhe técnico (nodes, sub-workflows, credencial criada) só aparece
        # nos logs — a resposta pro form fica só com um resumo, sem payload.
        if dry_run:
            db.registrar_log(
                cliente_id, "sistema",
                f"Dry-run: '{resultado['payload']['name']}' seria criado "
                f"({len(resultado['payload']['nodes'])} nodes) — nada foi criado no n8n.",
            )
            for aviso in resultado.get("avisos", []):
                db.registrar_log(cliente_id, "tool_call", aviso)
        else:
            for aviso in resultado.get("avisos", []):
                db.registrar_log(cliente_id, "tool_call", aviso)
            try:
                db.marcar_cliente_clonado(cliente_id, manifesto, resultado)
            except Exception as e:  # noqa: BLE001 — clone no n8n já aconteceu; não falhar a resposta por causa do registro
                db.registrar_log(cliente_id, "erro", f"Clonado no n8n, mas falhou ao registrar no histórico: {e}")

        self._responder_json(200, {"dry_run": dry_run, "workflow_id": resultado.get("workflow_id")})


def main():
    parser = argparse.ArgumentParser()
    # Em container a porta vem do ambiente (PORT é o nome que o Coolify usa);
    # --porta continua valendo pra rodar local.
    parser.add_argument("--porta", type=int, default=int(os.environ.get("PORT", "8099")))
    args = parser.parse_args()

    if not SENHA_ESPERADA:
        sys.stderr.write(
            "[servidor] ATENÇÃO: PAINEL_SENHA não configurada — todas as rotas vão responder 401.\n"
        )

    servidor = ThreadingHTTPServer(("0.0.0.0", args.porta), Handler)
    print(f"servindo em http://0.0.0.0:{args.porta}")
    try:
        servidor.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
