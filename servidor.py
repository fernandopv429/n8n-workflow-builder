#!/usr/bin/env python3
"""Backend web do painel de gerenciamento de agentes de IA (A5 Ecossistema).

    python3 servidor.py                 # sobe em 0.0.0.0:8099
    python3 servidor.py --porta 9001

Biblioteca padrão só (mesmo padrão de catalogo-brtw/servidor_pagamento.py —
sem framework). Fluxo:

    GET  /saude                     liveness/readiness — ÚNICA rota sem auth
    GET  /                        index.html
    GET  /clientes                  lista clientes (dashboard)
    POST /clientes                  cria cliente "rascunho" (passo 1: só o nome)
    GET  /clientes/<id>              detalhe de um cliente
    GET  /clientes/<id>/logs          últimas entradas de log desse cliente
    PUT  /clientes/<id>/credenciais    grava Kommo + chave OpenAI e marca o
                                       cliente ativo — a credencial OpenAI é
                                       CRIADA aqui (projeto próprio), não
                                       selecionada de uma lista. Desde
                                       15/10/2026 não existe mais clone de
                                       workflow no n8n: o worker_agente.py lê
                                       tudo direto do Postgres.
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
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

RAIZ = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(RAIZ / ".pylibs"))
sys.path.insert(0, str(RAIZ))

import agente_chat  # noqa: E402
import agente_geral  # noqa: E402
import db  # noqa: E402
import mcp_cliente  # noqa: E402
import mcp_kommo_client  # noqa: E402
import monitor_fila  # noqa: E402
import openai_admin  # noqa: E402
import pocketbase_client  # noqa: E402
import cofre  # noqa: E402
import evolution_client  # noqa: E402
from config import carregar_env  # noqa: E402

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
ROTA_CLIENTE_IMAGEM = re.compile(r"^/clientes/(\d+)/imagem$")
# Atendimento (ARQUITETURA-AGENTE.md): conversas do agente com os pacientes,
# separadas do chat do painel — que é o operador falando SOBRE o cliente.
ROTA_CLIENTE_CONTATOS = re.compile(r"^/clientes/(\d+)/contatos$")
ROTA_CONTATO_MENSAGENS = re.compile(r"^/contatos/(\d+)/mensagens$")
ROTA_CONTATO_STATUS = re.compile(r"^/contatos/(\d+)/status$")
# Zera a memória de uma conversa sem apagar o contato (ver db.limpar_conversa).
ROTA_CONTATO_RESET = re.compile(r"^/contatos/(\d+)/reset$")
ROTA_FILA = re.compile(r"^/fila$")
ROTA_CLIENTE_CONSUMO = re.compile(r"^/clientes/(\d+)/consumo$")
ROTA_CLIENTE_PROMPT = re.compile(r"^/clientes/(\d+)/prompt$")
ROTA_CLIENTE_RESTAURAR_PROMPT = re.compile(r"^/clientes/(\d+)/restaurar-prompt$")
# WhatsApp: criar a instância e conectar pelo QR, sem sair do painel. O passo
# mais esquecido era o webhook — instância conectada sem webhook recebe
# mensagem e não entrega a ninguém, o que parece "o agente não respondeu".
ROTA_CLIENTE_WHATSAPP = re.compile(r"^/clientes/(\d+)/whatsapp$")
ROTA_CLIENTE_WHATSAPP_DESCONECTAR = re.compile(r"^/clientes/(\d+)/whatsapp/desconectar$")
ROTA_CLIENTE_ATIVO = re.compile(r"^/clientes/(\d+)/ativo$")
# Resumo periódico de atendimento, mandado num grupo do WhatsApp por cliente
# (quem dispara o envio é um Schedule Trigger no n8n, chamando estas rotas).
ROTA_CLIENTE_GRUPO_AVISO = re.compile(r"^/clientes/(\d+)/grupo-aviso$")
ROTA_CLIENTE_RESUMO = re.compile(r"^/clientes/(\d+)/resumo$")
ROTA_CLIENTES_AVISO = re.compile(r"^/clientes-aviso$")
# Ferramentas por cliente: integração nova vira MCP novo no n8n, e aqui se
# escolhe quem usa o quê. O catálogo é descoberto do próprio n8n.
# Agente geral: número da A5 por onde o próprio cliente pede mudanças no
# agente dele. A autorização é o cadastro de demandantes, nada mais.
ROTA_GERAL = re.compile(r"^/geral$")
ROTA_GERAL_DEMANDANTES = re.compile(r"^/geral/demandantes$")
# Grupos que a instância do agente geral participa — pra escolher o destino
# do resumo periódico de atendimento sem caçar JID na mão (ver index.html).
ROTA_GERAL_GRUPOS = re.compile(r"^/geral/grupos$")
ROTA_GERAL_DESCONECTAR = re.compile(r"^/geral/desconectar$")
ROTA_MCPS_CATALOGO = re.compile(r"^/mcps$")
ROTA_CLIENTE_MCPS = re.compile(r"^/clientes/(\d+)/mcps$")
# Arquivos que o agente envia. O arquivo em si fica no PocketBase, com leitura
# pública — por isso não há rota de download aqui: o painel tem senha e a
# Evolution, que baixa, não sabe autenticar.
ROTA_CLIENTE_MIDIAS = re.compile(r"^/clientes/(\d+)/midias$")
# Projeto da OpenAI por cliente, SEM depender de Kommo nem de clonagem. Antes
# isso só acontecia dentro de PUT /credenciais, que exige Kommo e dispara o
# clone inteiro — herança do desenho antigo. Quem só queria medir consumo
# ficava preso num 400, sem nem log pra explicar.
ROTA_CLIENTE_OPENAI = re.compile(r"^/clientes/(\d+)/openai$")


def _com_imagem(cliente: dict) -> dict:
    """Acrescenta a URL pública da imagem. Fica aqui, e não no db.py, porque
    montar a URL depende do PocketBase — o banco só guarda o vínculo."""
    if not cliente:
        return cliente
    cliente["imagem_url"] = pocketbase_client.url_publica(
        cliente.get("imagem_pb_record_id"), cliente.get("imagem_pb_filename")
    )
    return cliente


# Saber O QUE está rodando em produção não era possível: o Coolify não
# redeploya sozinho a cada push, e a única forma de descobrir se um commit
# tinha subido era reproduzir o bug. `/saude` passa a dizer.
#
# O commit vem de variável de ambiente porque a imagem não leva o .git dentro
# (ver .dockerignore). O Coolify injeta SOURCE_COMMIT nos deploys de git; se
# não vier, `no_ar_desde` já responde "houve redeploy depois de tal hora?".
VERSAO = (
    os.environ.get("SOURCE_COMMIT")
    or os.environ.get("APP_COMMIT")
    or "desconhecida"
)[:12]
SUBIU_EM = datetime.now(timezone.utc).isoformat(timespec="seconds")


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
                db.listar_clientes()
            except Exception as e:  # noqa: BLE001
                self._responder_json(503, {"status": "degradado", "banco": str(e)})
                return
            self._responder_json(200, {
                "status": "ok", "banco": "ok",
                "versao": VERSAO, "no_ar_desde": SUBIU_EM,
            })
            return

        if not self._autenticado():
            return

        if self.path == "/" or self.path == "/index.html":
            self._servir_arquivo(RAIZ / "index.html", "text/html; charset=utf-8")
            return
        if self.path == "/clientes":
            self._responder_json(200, {"clientes": [_com_imagem(c) for c in db.listar_clientes()]})
            return

        m = ROTA_CLIENTE_LOGS.match(self.path)
        if m:
            self._responder_json(200, {"logs": db.listar_logs(int(m.group(1)))})
            return

        m = ROTA_CLIENTE_MENSAGENS.match(self.path)
        if m:
            cliente_id = int(m.group(1))
            # Poll-on-read: não há worker em segundo plano, então a resposta de
            # um lote só é buscada quando alguém pede as mensagens. Quem chama
            # de tempos em tempos é a própria tela do chat (ver index.html),
            # enquanto `lotes_pendentes` for maior que zero.
            try:
                agente_chat.buscar_respostas_em_lote(cliente_id)
            except Exception as e:  # noqa: BLE001 — não impede de mostrar o histórico
                sys.stderr.write(f"[servidor] falha ao checar lotes do chat: {e}\n")
            self._responder_json(200, {
                "mensagens": db.listar_mensagens(cliente_id),
                "lotes_pendentes": len(db.listar_lotes_chat_pendentes(cliente_id)),
            })
            return

        m = ROTA_CLIENTE_CONTATOS.match(self.path)
        if m:
            self._responder_json(200, {"contatos": db.listar_contatos(int(m.group(1)))})
            return

        m = ROTA_CONTATO_MENSAGENS.match(self.path)
        if m:
            contato_id = int(m.group(1))
            contato = db.obter_contato(contato_id)
            if contato is None:
                self._responder_json(404, {"error": "contato não encontrado"})
                return
            self._responder_json(200, {
                "contato": contato,
                "mensagens": db.listar_mensagens_agente(contato_id, limite=200),
            })
            return

        if ROTA_FILA.match(self.path):
            self._responder_json(200, monitor_fila.resumo())
            return

        m = ROTA_CLIENTE_WHATSAPP.match(self.path)
        if m:
            cliente = db.obter_cliente(int(m.group(1)))
            nome = (cliente or {}).get("evolution_instancia") or ""
            if not nome or not evolution_client.disponivel():
                self._responder_json(200, {"instancia": nome, "status": "nao configurada"})
                return
            try:
                self._responder_json(200, {
                    "instancia": nome, "status": evolution_client.status(nome),
                    "webhook": evolution_client.obter_webhook(nome),
                    "webhook_esperado": evolution_client.url_do_trilho()})
            except evolution_client.EvolutionError as e:
                self._responder_json(200, {"instancia": nome, "status": f"erro: {e}"})
            return

        if ROTA_GERAL.match(self.path):
            inst = agente_geral.instancia_configurada()
            estado = "não configurada"
            if inst and evolution_client.disponivel():
                try:
                    estado = evolution_client.status(inst)
                except evolution_client.EvolutionError as e:
                    estado = f"erro: {e}"
            self._responder_json(200, {
                "instancia": inst, "status": estado,
                "demandantes": db.listar_demandantes(),
                "clientes": [{"id": c["id"], "nome": c["cliente_nome"]}
                             for c in db.listar_clientes()]})
            return

        if ROTA_GERAL_GRUPOS.match(self.path):
            inst = agente_geral.instancia_configurada()
            if not inst or not evolution_client.disponivel():
                self._responder_json(200, {"grupos": [], "erro":
                    "instância do agente geral ainda não configurada"})
                return
            try:
                self._responder_json(200, {"grupos": evolution_client.listar_grupos(inst)})
            except evolution_client.EvolutionError as e:
                # Vira lista vazia + motivo, não erro: a tela cai pro campo de
                # texto manual quando isto falha (ver index.html).
                self._responder_json(200, {"grupos": [], "erro": str(e)})
            return

        if ROTA_MCPS_CATALOGO.match(self.path):
            try:
                catalogo = mcp_cliente.catalogo()
            except Exception as e:  # noqa: BLE001 — n8n fora do ar é informação, não erro do painel
                self._responder_json(200, {"mcps": [], "erro": str(e)[:200]})
                return
            # Quem já usa cada MCP: a tela precisa disso pra não deixar ligar a
            # base de conhecimento de um cliente no agente de outro. O nome do
            # MCP não diz de quem ele é, e um clique vazaria dado entre clientes.
            usos = db.quem_usa_cada_mcp()
            for m in catalogo:
                m["em_uso_por"] = usos.get(m["path"], [])
            self._responder_json(200, {"mcps": catalogo})
            return

        m = ROTA_CLIENTE_MCPS.match(self.path)
        if m:
            cliente_id = int(m.group(1))
            ligados = db.listar_mcps_do_cliente(cliente_id)
            # o token não volta pra tela: ela não precisa dele pra nada
            for x in ligados:
                x["tem_token"] = bool(x.pop("token", ""))
            self._responder_json(200, {"mcps": ligados})
            return

        m = ROTA_CLIENTE_MIDIAS.match(self.path)
        if m:
            itens = db.listar_midias_do_cliente(int(m.group(1)))
            for x in itens:
                x["url"] = pocketbase_client.url_midia(
                    x.pop("pb_record_id", ""), x.pop("pb_filename", ""))
            self._responder_json(200, {"midias": itens})
            return

        m = ROTA_CLIENTE_CONSUMO.match(self.path)
        if m:
            cliente = db.obter_cliente(int(m.group(1)))
            if cliente is None:
                self._responder_json(404, {"error": "cliente não encontrado"})
                return
            # Sem projeto, a causa muda conforme o estado do cliente, e dizer
            # a errada manda a pessoa procurar no lugar errado: num cliente
            # recém-criado não é herança nenhuma, é só um passo que falta.
            projeto = cliente.get("openai_projeto_id") or ""
            if not projeto:
                if cliente["status"] == "rascunho":
                    motivo = ("Este cliente ainda não tem credenciais salvas. Abra a "
                              "engrenagem, deixe o campo da chave da OpenAI VAZIO e salve — "
                              "aí o painel cria o projeto dele e o consumo passa a aparecer aqui.")
                elif not openai_admin.disponivel():
                    motivo = ("OPENAI_ADMIN_KEY não está configurada no servidor, então o "
                              "painel não consegue criar projeto por cliente.")
                else:
                    motivo = ("Este cliente foi configurado com uma chave da OpenAI colada à "
                              "mão, ou antes da separação por projeto. Salve as credenciais de "
                              "novo com o campo da chave VAZIO pra ele ganhar projeto próprio.")
                self._responder_json(200, {"disponivel": False, "motivo": motivo, "dias": 30})
                return
            self._responder_json(200, openai_admin.consumo_do_projeto(projeto))
            return

        if ROTA_CLIENTES_AVISO.match(self.path):
            self._responder_json(200, {"clientes": db.listar_clientes_com_grupo_aviso()})
            return

        m = ROTA_CLIENTE_RESUMO.match(self.path)
        if m:
            cliente_id = int(m.group(1))
            cliente = db.obter_cliente(cliente_id)
            if cliente is None:
                self._responder_json(404, {"error": "cliente não encontrado"})
                return
            self._responder_json(200, {
                "cliente_nome": cliente["cliente_nome"],
                "grupo": cliente.get("whatsapp_grupo_aviso") or "",
                **db.resumo_atendimento(cliente_id),
            })
            return

        m = ROTA_CLIENTE_ID.match(self.path)
        if m:
            cliente_id = int(m.group(1))
            cliente = db.obter_cliente(cliente_id)
            if cliente is None:
                self._responder_json(404, {"error": "cliente não encontrado"})
                return
            self._responder_json(200, _com_imagem(cliente))
            return

        self._responder_json(404, {"error": "não encontrado"})

    # --- POST --------------------------------------------------------

    def do_POST(self):
        if not self._autenticado():
            return

        if ROTA_GERAL.match(self.path):
            if not evolution_client.disponivel():
                self._responder_json(503, {"error": "EVOLUTION_URL/EVOLUTION_APIKEY não configuradas"})
                return
            nome = agente_geral.instancia_configurada() or "a5-central"
            try:
                if not evolution_client.existe(nome):
                    criada = evolution_client.criar_instancia(nome)
                    db.definir_config(agente_geral.CHAVE_INSTANCIA, nome)
                    self._responder_json(200, {**criada, "novo": True})
                    return
                evolution_client.definir_webhook(nome)
                db.definir_config(agente_geral.CHAVE_INSTANCIA, nome)
                estado = evolution_client.status(nome)
                if estado == "open":
                    self._responder_json(200, {"nome": nome, "status": estado, "conectada": True})
                    return
                self._responder_json(200, {"nome": nome, "status": estado,
                                           "conectada": False, **evolution_client.obter_qr(nome)})
                return
            except evolution_client.EvolutionError as e:
                self._responder_json(502, {"error": str(e)})
                return

        if ROTA_GERAL_DESCONECTAR.match(self.path):
            nome = agente_geral.instancia_configurada()
            if not nome or not evolution_client.disponivel():
                self._responder_json(400, {"error": "a5-central não tem WhatsApp conectado"})
                return
            try:
                evolution_client.desconectar_instancia(nome)
            except evolution_client.EvolutionError as e:
                self._responder_json(502, {"error": str(e)})
                return
            self._responder_json(200, {"desconectado": True})
            return

        if ROTA_GERAL_DEMANDANTES.match(self.path):
            corpo = self._ler_corpo()
            tel = "".join(ch for ch in str(corpo.get("id_whatsapp", "")) if ch.isdigit())
            cliente_id = int(corpo.get("cliente_id") or 0)
            if not tel or not cliente_id or db.obter_cliente(cliente_id) is None:
                self._responder_json(400, {"error": "telefone e cliente são obrigatórios"})
                return
            db.salvar_demandante(tel, cliente_id, str(corpo.get("nome", "")),
                                 bool(corpo.get("ativo", True)))
            db.registrar_log(
                cliente_id, "sistema",
                f"{tel} autorizado a pedir mudanças neste agente pelo WhatsApp da A5.")
            self._responder_json(200, {"salvo": True})
            return

        m = ROTA_CLIENTE_MIDIAS.match(self.path)
        if m:
            cliente_id = int(m.group(1))
            if db.obter_cliente(cliente_id) is None:
                self._responder_json(404, {"error": "cliente não encontrado"})
                return
            corpo = self._ler_corpo()
            nome = str(corpo.get("nome", "")).strip()
            if not nome:
                self._responder_json(400, {"error":
                    "dê um nome ao arquivo: é por ele que o agente escolhe o "
                    "que enviar, então 'vídeo de preparo do exame' funciona e "
                    "'arquivo1' não"})
                return
            try:
                conteudo = base64.b64decode(str(corpo.get("conteudo", "")), validate=True)
            except Exception:  # noqa: BLE001 — base64 quebrado vindo da tela
                self._responder_json(400, {"error": "conteúdo do arquivo inválido"})
                return
            if not conteudo:
                self._responder_json(400, {"error": "o arquivo chegou vazio"})
                return
            if not pocketbase_client.configurado():
                self._responder_json(503, {"error":
                    "PocketBase não configurado (POCKETBASE_URL/ADMIN_EMAIL/"
                    "ADMIN_PASSWORD) — é lá que os arquivos ficam"})
                return

            mimetype = str(corpo.get("mimetype", "")).strip() or "application/octet-stream"
            nome_original = str(corpo.get("nome_original", "")).strip() or nome
            try:
                enviado = pocketbase_client.enviar_midia(
                    cliente_id, nome, nome_original, conteudo)
            except pocketbase_client.PocketBaseError as e:
                # inclui o caso de passar de 16 MB, que o próprio cliente barra
                self._responder_json(400, {"error": str(e)})
                return
            db.salvar_midia(cliente_id, nome, str(corpo.get("descricao", "")).strip(),
                            enviado["record_id"], enviado["filename"],
                            nome_original, mimetype, len(conteudo))
            db.registrar_log(cliente_id, "sistema",
                             f"Arquivo '{nome}' ({mimetype}, "
                             f"{len(conteudo) // 1024} KB) disponível pro agente enviar.")
            self._responder_json(200, {"salvo": True, "nome": nome,
                                       "tamanho": len(conteudo)})
            return

        m = ROTA_CLIENTE_MCPS.match(self.path)
        if m:
            cliente_id = int(m.group(1))
            if db.obter_cliente(cliente_id) is None:
                self._responder_json(404, {"error": "cliente não encontrado"})
                return
            corpo = self._ler_corpo()
            apelido = str(corpo.get("apelido", "")).strip()
            path = str(corpo.get("path", "")).strip()
            if not apelido or not path:
                self._responder_json(400, {"error": "apelido e path são obrigatórios"})
                return

            # Testa ANTES de gravar: MCP que não responde cadastrado vira
            # ferramenta quebrada na mão do agente, e a falha só aparece no meio
            # de uma conversa com paciente.
            token = (str(corpo.get("token", "")).strip()
                     or ENV.get("MCP_TOKEN_INTERNO", "").strip())
            teste = mcp_cliente.testar(path, token)
            if not teste["ok"]:
                self._responder_json(400, {"error": f"o MCP não respondeu: {teste['motivo']}"})
                return

            # Trava server-side, não só na tela: ligar um MCP que já é de outro
            # cliente é vazamento de dado entre clientes, e a tela pode ser
            # contornada. `compartilhada` é a saída explícita pra MCP que
            # realmente serve todos (o do Kommo é multi-inquilino por desenho).
            # MCP multi-inquilino não precisa de ninguém marcar nada: ele não
            # guarda credencial dentro, recebe a de cada cliente na chamada.
            # Exigir a marcação manual aqui treinaria o operador a marcar
            # "serve a todos" por hábito — inclusive no MCP que não é.
            if not bool(corpo.get("compartilhada")) and not teste.get("multi_inquilino"):
                donos = [n for n in db.quem_usa_cada_mcp().get(path, [])
                         if n != db.obter_cliente(cliente_id)["cliente_nome"]]
                if donos:
                    self._responder_json(409, {"error":
                        f"esta ferramenta já é usada por {', '.join(donos)}. Se ela serve "
                        "a todos os clientes, marque 'serve a todos' — senão o agente "
                        "deste cliente passaria a ler dados do outro."})
                    return

            db.salvar_mcp_do_cliente(
                cliente_id, apelido, path,
                token=str(corpo.get("token", "")).strip(),
                ativo=bool(corpo.get("ativo", True)),
                no_atendimento=bool(corpo.get("no_atendimento", False)))
            db.registrar_log(
                cliente_id, "sistema",
                f"Ferramenta '{apelido}' ({path}) ligada — {teste['quantidade']} "
                f"ferramentas. No atendimento ao paciente: "
                f"{'sim' if corpo.get('no_atendimento') else 'não, só no chat do painel'}.")
            self._responder_json(200, {"salvo": True, **teste})
            return

        m = ROTA_CLIENTE_OPENAI.match(self.path)
        if m:
            cliente_id = int(m.group(1))
            cliente = db.obter_cliente(cliente_id)
            if cliente is None:
                self._responder_json(404, {"error": "cliente não encontrado"})
                return
            if cliente.get("openai_projeto_id"):
                self._responder_json(200, {"ja_existia": True,
                                           "projeto_id": cliente["openai_projeto_id"]})
                return
            if not openai_admin.disponivel():
                self._responder_json(503, {"error": "OPENAI_ADMIN_KEY não configurada no servidor"})
                return
            try:
                criado = openai_admin.criar_projeto_e_chave(cliente["cliente_nome"])
            except openai_admin.OpenAiAdminError as e:
                db.registrar_log(cliente_id, "erro", f"OpenAI Admin API: {e}")
                self._responder_json(502, {"error": str(e)})
                return
            if not cofre.disponivel():
                # sem cofre a chave não tem onde ser guardada, e um projeto sem
                # chave guardada é pior que nenhum: some do nosso alcance
                openai_admin.arquivar_projeto(criado["projeto_id"])
                self._responder_json(503, {"error":
                    "COFRE_CHAVE não configurada — sem ela a chave do cliente não pode "
                    "ser guardada com segurança. O projeto criado foi desfeito."})
                return
            db.definir_projeto_openai(cliente_id, criado["projeto_id"],
                                      cofre.cifrar(criado["api_key"]))
            db.registrar_log(
                cliente_id, "sistema",
                f"Projeto '{criado['projeto_nome']}' criado na OpenAI "
                f"({criado['projeto_id']}) com chave própria, guardada cifrada. "
                "O consumo deste cliente passa a ser medido separado.")
            self._responder_json(200, {"projeto_id": criado["projeto_id"],
                                       "projeto_nome": criado["projeto_nome"]})
            return

        m = ROTA_CLIENTE_WHATSAPP_DESCONECTAR.match(self.path)
        if m:
            cliente_id = int(m.group(1))
            cliente = db.obter_cliente(cliente_id)
            if cliente is None:
                self._responder_json(404, {"error": "cliente não encontrado"})
                return
            nome = cliente.get("evolution_instancia")
            if not nome or not evolution_client.disponivel():
                self._responder_json(400, {"error": "este cliente não tem WhatsApp conectado"})
                return
            try:
                evolution_client.desconectar_instancia(nome)
            except evolution_client.EvolutionError as e:
                db.registrar_log(cliente_id, "erro", f"Falha ao desconectar WhatsApp: {e}")
                self._responder_json(502, {"error": str(e)})
                return
            db.registrar_log(
                cliente_id, "sistema",
                f"WhatsApp ('{nome}') desconectado manualmente pelo painel — o cadastro e o "
                "histórico continuam, só falta escanear o QR de novo pra reconectar.")
            self._responder_json(200, {"desconectado": True})
            return

        m = ROTA_CLIENTE_WHATSAPP.match(self.path)
        if m:
            cliente_id = int(m.group(1))
            cliente = db.obter_cliente(cliente_id)
            if cliente is None:
                self._responder_json(404, {"error": "cliente não encontrado"})
                return
            if not evolution_client.disponivel():
                self._responder_json(503, {"error": "EVOLUTION_URL/EVOLUTION_APIKEY não configuradas"})
                return

            nome = (cliente.get("evolution_instancia")
                    or evolution_client.nome_da_instancia(cliente["cliente_nome"]))
            try:
                if not evolution_client.existe(nome):
                    criada = evolution_client.criar_instancia(nome)
                    db.definir_instancia_evolution(cliente_id, nome)
                    db.registrar_log(
                        cliente_id, "sistema",
                        f"Instância de WhatsApp '{nome}' criada na Evolution, com o webhook "
                        f"já apontado pro trilho ({criada['webhook']}). Falta escanear o QR.")
                    self._responder_json(200, {**criada, "novo": True})
                    return

                # já existe: reaponta o webhook (idempotente) e devolve QR novo —
                # o anterior expira em ~40s, então pedir outro é o caso comum
                evolution_client.definir_webhook(nome)
                db.definir_instancia_evolution(cliente_id, nome)
                estado = evolution_client.status(nome)
                if estado == "open":
                    self._responder_json(200, {"nome": nome, "status": estado, "conectada": True,
                                               "webhook": evolution_client.url_do_trilho()})
                    return
                qr = evolution_client.obter_qr(nome)
                self._responder_json(200, {"nome": nome, "status": estado, "conectada": False,
                                           "webhook": evolution_client.url_do_trilho(), **qr})
                return
            except evolution_client.EvolutionError as e:
                db.registrar_log(cliente_id, "erro", f"Evolution: {e}")
                self._responder_json(502, {"error": str(e)})
                return


        m = ROTA_CLIENTE_CHAT.match(self.path)
        if m:
            self._chat(int(m.group(1)))
            return

        m = ROTA_CLIENTE_RESTAURAR_PROMPT.match(self.path)
        if m:
            cliente_id = int(m.group(1))
            if db.obter_cliente(cliente_id) is None:
                self._responder_json(404, {"error": "cliente não encontrado"})
                return
            if not db.restaurar_prompt_anterior(cliente_id):
                self._responder_json(400, {"error": "não há versão anterior pra restaurar"})
                return
            db.registrar_log(cliente_id, "sistema",
                             "Prompt restaurado pra versão anterior, pelo painel.")
            self._responder_json(200, {"restaurado": True})
            return

        m = ROTA_CLIENTE_IMAGEM.match(self.path)
        if m:
            self._enviar_imagem(int(m.group(1)))
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

        if not cliente_nome:
            self._responder_json(400, {"error": "cliente_nome é obrigatório"})
            return

        # O briefing saiu daqui (25/09/2026): gerado neste passo, ele não tinha
        # credencial do Kommo pra consultar e a proposta de funil saía às cegas.
        # Agora é feito pelo chat, depois das credenciais — com a opção de ir
        # pela Batch API lá mesmo.
        cliente_id = db.criar_cliente_rascunho(cliente_nome)
        self._responder_json(200, _com_imagem(db.obter_cliente(cliente_id)))

    def _enviar_imagem(self, cliente_id: int):
        """Recebe a imagem em base64 num JSON — evita ter que fazer o parse de
        multipart aqui (o módulo `cgi`, que faria isso, saiu do Python 3.13).
        O upload pro PocketBase é que monta o multipart."""
        cliente = db.obter_cliente(cliente_id)
        if cliente is None:
            self._responder_json(404, {"error": "cliente não encontrado"})
            return
        if not pocketbase_client.configurado():
            self._responder_json(503, {"error": "PocketBase não configurado (ver POCKETBASE_* no ambiente)"})
            return

        try:
            corpo = self._ler_corpo()
        except json.JSONDecodeError:
            self._responder_json(400, {"error": "JSON inválido"})
            return

        nome = str(corpo.get("nome_arquivo", "imagem.png")).strip() or "imagem.png"
        try:
            conteudo = base64.b64decode(str(corpo.get("conteudo_base64", "")), validate=True)
        except Exception:  # noqa: BLE001
            self._responder_json(400, {"error": "conteudo_base64 inválido"})
            return
        if not conteudo:
            self._responder_json(400, {"error": "imagem vazia"})
            return

        anterior = cliente.get("imagem_pb_record_id")
        try:
            r = pocketbase_client.enviar_imagem(cliente_id, nome, conteudo)
        except pocketbase_client.PocketBaseError as e:
            db.registrar_log(cliente_id, "erro", f"Falha ao enviar imagem: {e}")
            self._responder_json(502, {"error": str(e)})
            return

        db.definir_imagem(cliente_id, r["record_id"], r["filename"])
        # A troca só apaga a antiga DEPOIS que a nova entrou — se a remoção
        # falhar, sobra um arquivo órfão, o que é melhor que ficar sem imagem.
        if anterior:
            try:
                pocketbase_client.remover_imagem(anterior)
            except pocketbase_client.PocketBaseError as e:
                db.registrar_log(cliente_id, "erro", f"Imagem trocada, mas a anterior ficou no PocketBase: {e}")

        db.registrar_log(cliente_id, "sistema", "Imagem do cliente atualizada.")
        self._responder_json(200, {"imagem_url": r["url"]})

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

        if bool(corpo.get("em_lote", False)):
            try:
                batch_id = agente_chat.enviar_em_lote(cliente_id, texto)
            except ValueError as e:
                self._responder_json(400, {"error": str(e)})
                return
            except Exception as e:  # noqa: BLE001
                db.registrar_log(cliente_id, "erro", f"Falha ao enviar pela Batch API: {e}")
                self._responder_json(502, {"error": f"falha ao enviar pra Batch API: {e}"})
                return
            self._responder_json(200, {"em_lote": True, "batch_id": batch_id})
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

    # --- DELETE ------------------------------------------------------

    def do_DELETE(self):
        if not self._autenticado():
            return

        if ROTA_GERAL_DEMANDANTES.match(self.path):
            tel = str(self._ler_corpo().get("id_whatsapp", "")).strip()
            db.remover_demandante(tel)
            self._responder_json(200, {"removido": True})
            return

        m = ROTA_CLIENTE_MIDIAS.match(self.path)
        if m:
            cliente_id = int(m.group(1))
            record = db.apagar_midia(cliente_id, int(self._ler_corpo().get("id", 0)))
            # o arquivo sai do PocketBase junto: linha removida com arquivo
            # órfão deixa lixo acumulando no hub da A5
            if record:
                try:
                    pocketbase_client.remover_midia(record)
                except pocketbase_client.PocketBaseError as e:
                    db.registrar_log(cliente_id, "erro",
                                     f"arquivo removido do painel mas não do PocketBase: {e}")
            self._responder_json(200, {"removido": bool(record)})
            return

        m = ROTA_CLIENTE_MCPS.match(self.path)
        if m:
            apelido = str(self._ler_corpo().get("apelido", "")).strip()
            db.remover_mcp_do_cliente(int(m.group(1)), apelido)
            db.registrar_log(int(m.group(1)), "sistema",
                             f"Ferramenta '{apelido}' desligada deste cliente.")
            self._responder_json(200, {"removido": True})
            return

        m = ROTA_CLIENTE_ID.match(self.path)
        if not m:
            self._responder_json(404, {"error": "não encontrado"})
            return
        cliente_id = int(m.group(1))

        cliente = db.obter_cliente(cliente_id)
        if cliente is None:
            self._responder_json(404, {"error": "cliente não encontrado"})
            return

        relatorio = []

        if cliente.get("imagem_pb_record_id"):
            try:
                pocketbase_client.remover_imagem(cliente["imagem_pb_record_id"])
                relatorio.append("imagem removida do PocketBase")
            except Exception as e:  # noqa: BLE001
                relatorio.append(f"imagem ficou no PocketBase: {e}")

        # Arquivar revoga as chaves do projeto. Sem isso a chave do cliente
        # continuaria valendo e gastando depois de ele sair do painel — e
        # ninguém lembra de limpar isso à mão no site da OpenAI.
        if cliente.get("evolution_instancia") and evolution_client.disponivel():
            try:
                evolution_client.remover_instancia(cliente["evolution_instancia"])
                relatorio.append(f"instância '{cliente['evolution_instancia']}' removida da Evolution")
            except Exception as e:  # noqa: BLE001
                relatorio.append(
                    f"ATENÇÃO: a instância '{cliente['evolution_instancia']}' continua na "
                    f"Evolution, conectada ao WhatsApp do cliente: {e}")

        if cliente.get("openai_projeto_id"):
            try:
                openai_admin.arquivar_projeto(cliente["openai_projeto_id"])
                relatorio.append(
                    f"projeto {cliente['openai_projeto_id']} arquivado na OpenAI (chaves revogadas)"
                )
            except Exception as e:  # noqa: BLE001 — não travar a remoção no painel
                relatorio.append(
                    f"ATENÇÃO: o projeto {cliente['openai_projeto_id']} continua ativo na "
                    f"OpenAI e a chave dele ainda funciona — arquive à mão: {e}"
                )

        db.remover_cliente(cliente_id)
        self._responder_json(200, {
            "removido": True,
            "cliente_nome": cliente["cliente_nome"],
            "n8n": relatorio,
        })

    # --- PUT ---------------------------------------------------------

    def do_PUT(self):
        if not self._autenticado():
            return

        m = ROTA_CONTATO_RESET.match(self.path)
        if m:
            contato_id = int(m.group(1))
            contato = db.obter_contato(contato_id)
            if contato is None:
                self._responder_json(404, {"error": "contato não encontrado"})
                return
            apagadas = db.limpar_conversa(contato_id)
            db.registrar_log(
                contato["cliente_id"], "sistema",
                f"Conversa com {contato['id_whatsapp']} zerada ({apagadas} mensagens "
                "apagadas). O agente volta a tratar como contato novo.")
            self._responder_json(200, {"apagadas": apagadas})
            return

        m = ROTA_CLIENTE_ATIVO.match(self.path)
        if m:
            cliente_id = int(m.group(1))
            if db.obter_cliente(cliente_id) is None:
                self._responder_json(404, {"error": "cliente não encontrado"})
                return
            ativo = bool(self._ler_corpo().get("ativo"))
            db.definir_agente_ativo(cliente_id, ativo)
            db.registrar_log(
                cliente_id, "sistema",
                "Agente LIGADO — volta a responder as mensagens que chegarem." if ativo
                else "Agente DESLIGADO — mensagens continuam sendo guardadas, "
                     "mas ninguém responde até religar.")
            self._responder_json(200, {"ativo": ativo})
            return

        m = ROTA_CLIENTE_GRUPO_AVISO.match(self.path)
        if m:
            cliente_id = int(m.group(1))
            if db.obter_cliente(cliente_id) is None:
                self._responder_json(404, {"error": "cliente não encontrado"})
                return
            grupo = str(self._ler_corpo().get("grupo", "")).strip()
            db.definir_grupo_aviso(cliente_id, grupo)
            db.registrar_log(
                cliente_id, "sistema",
                f"Grupo de aviso de atendimento definido: {grupo}" if grupo
                else "Grupo de aviso de atendimento removido — este cliente para de "
                     "entrar no resumo periódico.")
            self._responder_json(200, {"grupo": grupo})
            return

        m = ROTA_CLIENTE_PROMPT.match(self.path)
        if m:
            cliente_id = int(m.group(1))
            if db.obter_cliente(cliente_id) is None:
                self._responder_json(404, {"error": "cliente não encontrado"})
                return
            texto = (self._ler_corpo().get("prompt") or "").strip()
            db.definir_prompt(cliente_id, texto)
            # Fica no log porque muda como o agente fala com o paciente a partir
            # da próxima mensagem — e, diferente de um clone, vale na hora.
            db.registrar_log(
                cliente_id, "sistema",
                f"Prompt do agente alterado à mão ({len(texto)} caracteres). "
                "Vale a partir da próxima mensagem recebida.",
            )
            self._responder_json(200, {"salvo": True, "caracteres": len(texto)})
            return

        m = ROTA_CONTATO_STATUS.match(self.path)
        if m:
            contato_id = int(m.group(1))
            contato = db.obter_contato(contato_id)
            if contato is None:
                self._responder_json(404, {"error": "contato não encontrado"})
                return
            status = (self._ler_corpo().get("status") or "").upper()
            if status not in ("IA", "HUMANO"):
                self._responder_json(400, {"error": "status deve ser IA ou HUMANO"})
                return
            db.definir_status_contato(contato_id, status)
            # Fica no log do cliente porque é uma mudança com efeito visível pro
            # paciente: em HUMANO o agente para de responder, e sem registro
            # ninguém lembra quem silenciou a conversa nem quando.
            db.registrar_log(
                contato["cliente_id"], "sistema",
                f"Atendimento de {contato['id_whatsapp']} passou para "
                f"{'ATENDENTE HUMANO (o agente não responde mais)' if status == 'HUMANO' else 'IA'}.",
            )
            self._responder_json(200, {"status": status})
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

        # Conexão com o Kommo é testada ANTES de gravar qualquer coisa: com
        # token errado o worker atenderia achando que a credencial vale, e a
        # falha só apareceria quando chegasse mensagem de paciente de verdade.
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
            # Guarda só DEPOIS de o Kommo confirmar: credencial inválida no
            # banco é pior que ausente — o worker a usaria achando que vale.
            db.definir_credenciais_kommo(cliente_id, subdominio, token)
            funis = checagem.get("funis")
            db.registrar_log(
                cliente_id, "sistema",
                f"Conexão com o Kommo '{subdominio}' confirmada"
                + (f" ({funis} funis na conta)." if funis is not None else "."),
            )

        # Sem chave digitada e sem projeto ainda, cria um projeto próprio do
        # cliente na OpenAI e tira a chave de lá — é o que torna o custo
        # rastreável por cliente (ver _cliente_openai em worker_agente.py).
        # Já tendo projeto, não mexe: trocar a chave aqui invalidaria a antiga
        # sem motivo.
        chave_openai = corpo.get("openai_api_key", "").strip()
        if not chave_openai and not cliente.get("openai_projeto_id"):
            if not openai_admin.disponivel():
                self._responder_json(400, {"error":
                    "preencha a chave da OpenAI — a criação automática exige a variável "
                    "OPENAI_ADMIN_KEY configurada no servidor."})
                return
            try:
                criado = openai_admin.criar_projeto_e_chave(cliente["cliente_nome"])
            except openai_admin.OpenAiAdminError as e:
                db.registrar_log(cliente_id, "erro", f"OpenAI Admin API: {e}")
                self._responder_json(502, {"error": f"não consegui criar a chave da OpenAI: {e}"})
                return
            db.definir_projeto_openai(
                cliente_id, criado["projeto_id"],
                cofre.cifrar(criado["api_key"]) if cofre.disponivel() else "")
            if not cofre.disponivel():
                db.registrar_log(
                    cliente_id, "erro",
                    "COFRE_CHAVE não configurada: a chave da OpenAI deste cliente NÃO "
                    "foi guardada, então o worker vai usar a chave global e o consumo "
                    "dele não aparecerá separado.")
            db.registrar_log(
                cliente_id, "sistema",
                f"Projeto '{criado['projeto_nome']}' criado na OpenAI ({criado['projeto_id']}) "
                "com chave própria — o consumo deste cliente passa a ser medido separado.",
            )
        elif chave_openai:
            # Chave colada à mão: guarda sem projeto rastreado (comportamento
            # antigo, pra quem quiser reaproveitar uma chave que já existe).
            db.definir_projeto_openai(
                cliente_id, "", cofre.cifrar(chave_openai) if cofre.disponivel() else "")

        db.marcar_cliente_ativo(cliente_id)
        self._responder_json(200, {"ativo": True})


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
