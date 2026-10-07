"""Cria no n8n o trilho genérico: WhatsApp → fila → worker → WhatsApp.

    python3 trilho_n8n.py            # mostra o que existe
    python3 trilho_n8n.py --aplicar  # cria/atualiza (fica DESATIVADO)

São dois workflows, um por sentido:

  "A5 Trilho - Entrada"  webhook do Evolution → publica em `agente.entrada`
  "A5 Trilho - Saida"    consome `agente.saida` → envia pelo Evolution

Um só pra TODOS os clientes — é essa a diferença em relação ao que existe hoje.
O cliente é identificado pela `instance` que vem no payload do Evolution, e o
worker resolve instância → cliente no Postgres. Sem isso, voltaríamos a ter um
workflow por cliente, que é a origem de quase todo bug deste projeto
(ver ARQUITETURA-AGENTE.md).

Áudio: o trilho encaminha a REFERÊNCIA, não os bytes — o webhook do Evolution
não os traz. Quem busca o arquivo e transcreve é o worker, porque lá a chamada
usa a chave do próprio cliente e o custo da transcrição cai no projeto dele.

O que ainda falta, de propósito: agrupar mensagens seguidas (debounce) e tratar
imagem/documento. Imagem hoje é registrada como não-tratada, em vez de virar
resposta vazia pro paciente.
"""
import argparse
import json
import sys
import urllib.parse

import config
from n8n_client import N8nClient

NOME_ENTRADA = "A5 Trilho - Entrada"
NOME_SAIDA = "A5 Trilho - Saida"
CAMINHO_WEBHOOK = "a5-trilho-entrada"

CRED_RABBIT_NOME = "A5 Agente RabbitMQ"
CRED_RABBIT_TIPO = "rabbitmq"

# Credencial do Evolution já existente no n8n, usada pelos workflows atuais.
CRED_EVOLUTION_ID = "kDkNqoImm6fZEdkx"
CRED_EVOLUTION_NOME = "Evolution account"


def _conf_rabbit() -> dict:
    """Credencial do RabbitMQ pro n8n, a partir da URL do worker.

    Usa o MESMO usuário escopado (`agente`), não o de admin: se o n8n for
    comprometido, o estrago fica no vhost do agente e não alcança as filas do
    coletor do CJPG, que dividem o broker.
    """
    url = config.carregar_env().get("RABBITMQ_AGENTE_URL", "")
    p = urllib.parse.urlparse(url)
    if not p.hostname:
        raise SystemExit("RABBITMQ_AGENTE_URL não configurada (ver infra_filas.py)")
    return {
        "hostname": p.hostname,
        "port": p.port or 5672,
        "username": urllib.parse.unquote(p.username or ""),
        "password": urllib.parse.unquote(p.password or ""),
        "vhost": (p.path or "/").lstrip("/") or "/",
        "ssl": False,
    }


# O Evolution manda formatos diferentes conforme o tipo de mensagem; este Code
# normaliza tudo num payload só e descarta o que não interessa. Fica em Code e
# não numa cadeia de if/set porque é lógica de dados, que em node vira dezenas
# de caixas difíceis de ler — e foi assim que os workflows atuais chegaram a 128.
CODIGO_NORMALIZA = r"""
const body = $input.first().json.body || $input.first().json || {};
const dados = body.data || body;
const chave = dados.key || {};

// fromMe: eco da própria resposta do agente. Processar isso faria o agente
// conversar consigo mesmo em loop.
if (chave.fromMe === true) return [];

const msg = dados.message || {};
const texto = (
  msg.conversation ||
  (msg.extendedTextMessage && msg.extendedTextMessage.text) ||
  (msg.imageMessage && msg.imageMessage.caption) ||
  (msg.videoMessage && msg.videoMessage.caption) ||
  ''
).trim();

const remoteJid = chave.remoteJid || '';
// grupo (@g.us) e status não são atendimento individual
if (!remoteJid || remoteJid.endsWith('@g.us') || remoteJid.startsWith('status@')) return [];

// Áudio vai pra fila SEM os bytes: o webhook não os traz, e quem busca na
// Evolution e transcreve é o worker — lá a chamada usa a chave do cliente, e o
// custo da transcrição aparece no projeto dele (ver worker_agente.transcrever).
const ehAudio = !!(msg.audioMessage || msg.pttMessage);
if (!texto && ehAudio) {
  return [{ json: {
    tipo: 'audio',
    instancia: body.instance || dados.instance || '',
    id_whatsapp: remoteJid.split('@')[0],
    nome: dados.pushName || '',
    texto: '',
    mensagem_id: chave.id || '',
  }}];
}

// Imagem, documento, figurinha: ainda não tratados. Sinalizar é melhor que
// silenciar — o paciente mandou algo e ninguém viu.
if (!texto) {
  return [{ json: {
    ignorado: true,
    motivo: Object.keys(msg)[0] || 'sem texto',
    instancia: body.instance || dados.instance || '',
    id_whatsapp: remoteJid.split('@')[0],
  }}];
}

return [{ json: {
  tipo: 'texto',
  instancia: body.instance || dados.instance || '',
  id_whatsapp: remoteJid.split('@')[0],
  nome: dados.pushName || '',
  texto,
  // id da mensagem no WhatsApp: é a chave de idempotência do worker. Sem ele,
  // uma reentrega do Evolution vira resposta repetida pro paciente.
  mensagem_id: chave.id || '',
}}];
"""


def _workflow_entrada(cred_id: str) -> dict:
    return {
        "name": NOME_ENTRADA,
        "settings": {"executionOrder": "v1"},
        "nodes": [
            {
                "id": "webhook-entrada",
                "name": "Webhook Evolution",
                "type": "n8n-nodes-base.webhook",
                "typeVersion": 2,
                "position": [0, 0],
                # webhookId igual ao path: sem isso a URL de produção dá 404
                # mesmo com o workflow ativo (memória n8n-workflow-por-api-webhookid)
                "webhookId": CAMINHO_WEBHOOK,
                "parameters": {
                    "path": CAMINHO_WEBHOOK,
                    "httpMethod": "POST",
                    "responseMode": "onReceived",
                    "options": {},
                },
            },
            {
                "id": "normaliza",
                "name": "Normaliza",
                "type": "n8n-nodes-base.code",
                "typeVersion": 2,
                "position": [220, 0],
                "parameters": {"jsCode": CODIGO_NORMALIZA},
            },
            {
                "id": "tem-texto",
                "name": "Tem texto?",
                "type": "n8n-nodes-base.if",
                "typeVersion": 2,
                "position": [440, 0],
                "parameters": {
                    "conditions": {
                        "options": {"caseSensitive": True, "version": 2},
                        "conditions": [{
                            "id": "c1",
                            "operator": {"type": "boolean", "operation": "false", "singleValue": True},
                            "leftValue": "={{ $json.ignorado === true }}",
                            "rightValue": "",
                        }],
                        "combinator": "and",
                    },
                    "options": {},
                },
            },
            {
                "id": "publica",
                "name": "Publica na fila",
                "type": "n8n-nodes-base.rabbitmq",
                "typeVersion": 1.1,
                "position": [680, -80],
                "parameters": {
                    "queue": "agente.entrada",
                    "options": {},
                },
                "credentials": {"rabbitmq": {"id": cred_id, "name": CRED_RABBIT_NOME}},
            },
            {
                "id": "nao-tratado",
                "name": "Tipo nao tratado",
                "type": "n8n-nodes-base.noOp",
                "typeVersion": 1,
                "position": [680, 120],
                "parameters": {},
            },
        ],
        "connections": {
            "Webhook Evolution": {"main": [[{"node": "Normaliza", "type": "main", "index": 0}]]},
            "Normaliza": {"main": [[{"node": "Tem texto?", "type": "main", "index": 0}]]},
            "Tem texto?": {"main": [
                [{"node": "Publica na fila", "type": "main", "index": 0}],
                [{"node": "Tipo nao tratado", "type": "main", "index": 0}],
            ]},
        },
    }


def _workflow_saida(cred_id: str) -> dict:
    return {
        "name": NOME_SAIDA,
        "settings": {"executionOrder": "v1"},
        "nodes": [
            {
                "id": "consome",
                "name": "Fila de saida",
                "type": "n8n-nodes-base.rabbitmqTrigger",
                "typeVersion": 1.1,
                "position": [0, 0],
                "parameters": {
                    "queue": "agente.saida",
                    "options": {
                        # ack só depois do envio: se o Evolution falhar, a
                        # mensagem volta pra fila em vez de sumir
                        "acknowledge": "executionFinishes",
                        "parallelMessages": 1,
                    },
                },
                "credentials": {"rabbitmq": {"id": cred_id, "name": CRED_RABBIT_NOME}},
            },
            {
                "id": "envia",
                "name": "Enviar texto",
                # Node da COMUNIDADE, não do core: `n8n-nodes-evolution-api`.
                # Com o prefixo errado o n8n mostra o node como desconhecido e o
                # workflow não roda — conferido contra os workflows reais.
                "type": "n8n-nodes-evolution-api.evolutionApi",
                "typeVersion": 1,
                "position": [240, 0],
                "parameters": {
                    "resource": "messages-api",
                    "instanceName": "={{ $json.content ? JSON.parse($json.content).instancia : $json.instancia }}",
                    "remoteJid": "={{ $json.content ? JSON.parse($json.content).id_whatsapp : $json.id_whatsapp }}",
                    "messageText": "={{ $json.content ? JSON.parse($json.content).texto : $json.texto }}",
                    "options_message": {},
                },
                # A credencial do Evolution é COMPARTILHADA entre os clientes —
                # quem separa um do outro é a `instanceName` acima, que vem da
                # fila. É a mesma que os workflows atuais usam.
                "credentials": {"evolutionApi": {"id": CRED_EVOLUTION_ID,
                                                 "name": CRED_EVOLUTION_NOME}},
            },
        ],
        "connections": {
            "Fila de saida": {"main": [[{"node": "Enviar texto", "type": "main", "index": 0}]]},
        },
    }


def _garantir_credencial(cliente: N8nClient) -> str:
    conf = _conf_rabbit()
    existentes = cliente.listar_credenciais() if hasattr(cliente, "listar_credenciais") else []
    for c in existentes:
        if c.get("name") == CRED_RABBIT_NOME:
            return c["id"]
    criada = cliente.criar_credencial(CRED_RABBIT_NOME, CRED_RABBIT_TIPO, conf)
    return criada["id"] if isinstance(criada, dict) else criada


def _achar(cliente: N8nClient, nome: str):
    for w in cliente._request("GET", "/workflows?limit=250").get("data", []):
        if w["name"] == nome:
            return w
    return None


def diagnosticar():
    c = N8nClient()
    for nome in (NOME_ENTRADA, NOME_SAIDA):
        w = _achar(c, nome)
        print(f"  {nome:<24} {'existe (ativo=' + str(w.get('active')) + ')' if w else 'NÃO existe'}")
    print(f"\n  webhook de entrada seria: {config.carregar_env().get('N8N_URL','')}/webhook/{CAMINHO_WEBHOOK}")


def aplicar():
    c = N8nClient()
    cred_id = _garantir_credencial(c)
    print("credencial RabbitMQ no n8n:", cred_id)

    for nome, montar in ((NOME_ENTRADA, _workflow_entrada), (NOME_SAIDA, _workflow_saida)):
        payload = montar(cred_id)
        existente = _achar(c, nome)
        if existente:
            c.update_workflow(existente["id"], payload)
            print(f"atualizado: {nome} ({existente['id']})")
        else:
            novo = c.create_workflow(payload)
            print(f"criado:     {nome} ({novo['id']})")

    print(
        "\nOs dois ficam DESATIVADOS. Ativar só depois de:\n"
        "  1. o worker estar de pé (PAPEL=worker no Coolify);\n"
        "  2. a instância do Evolution estar vinculada a um cliente no painel;\n"
        "  3. testar com um número seu, nunca com o de um cliente real.\n"
        f"\nURL pro webhook do Evolution:\n"
        f"  {config.carregar_env().get('N8N_URL','')}/webhook/{CAMINHO_WEBHOOK}"
    )


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--aplicar", action="store_true")
    args = p.parse_args()
    aplicar() if args.aplicar else (diagnosticar(), print("\n(diagnóstico — rode com --aplicar)"))
