"""Cria o vhost, o usuário e as filas do agente no RabbitMQ. Idempotente.

Rodar: python3 infra_filas.py            (mostra o que existe)
       python3 infra_filas.py --aplicar  (cria o que faltar)

Por que um vhost próprio: o broker é compartilhado com outros projetos da A5 —
`scraping_queue`, `crawl_queue` e `corridas_scraping` são do coletor do CJPG.
Permissão no RabbitMQ é POR VHOST, então com todo mundo no `/` as credenciais
do agente conseguiriam apagar as filas do outro projeto, e vice-versa. Vhost
separado + usuário escopado resolve os dois lados.

As filas e o dead-letter são declarados aqui, e não no worker, de propósito: um
bug no worker não pode redeclarar fila com argumento diferente (o RabbitMQ
recusa com PRECONDITION_FAILED e derruba o consumidor).
"""
import argparse
import base64
import json
import secrets
import sys
import urllib.error
import urllib.parse
import urllib.request

import config

VHOST = "agente"
USUARIO = "agente"

# A fila de entrada aponta pro dead-letter: mensagem que falhar além do limite
# vai pra `agente.entrada.dlq` em vez de sumir. Mensagem de paciente não pode
# desaparecer calada — é pior que um erro visível.
TROCA_DLQ = "agente.dlx"
FILAS = {
    "agente.entrada": {
        "durable": True,
        "arguments": {
            "x-dead-letter-exchange": TROCA_DLQ,
            "x-dead-letter-routing-key": "agente.entrada.dlq",
        },
    },
    "agente.saida": {"durable": True, "arguments": {}},
    "agente.entrada.dlq": {"durable": True, "arguments": {}},
}


def _painel() -> str:
    return config.carregar_env().get("RABBITMQ_PAINEL", "").rstrip("/")


def _credenciais_admin() -> tuple:
    url = config.carregar_env().get("RABBITMQ_URL", "")
    p = urllib.parse.urlparse(url)
    if not p.username:
        raise SystemExit("RABBITMQ_URL sem usuário/senha no .env")
    return urllib.parse.unquote(p.username), urllib.parse.unquote(p.password or "")


def _api(metodo: str, caminho: str, corpo=None):
    usuario, senha = _credenciais_admin()
    cred = base64.b64encode(f"{usuario}:{senha}".encode()).decode()
    dados = json.dumps(corpo).encode() if corpo is not None else None
    req = urllib.request.Request(
        f"{_painel()}/api{caminho}",
        data=dados,
        method=metodo,
        headers={"Authorization": f"Basic {cred}", "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            corpo_resp = r.read()
            return json.loads(corpo_resp) if corpo_resp else {}
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        raise SystemExit(f"RabbitMQ respondeu {e.code}: {e.read().decode()[:300]}")


def diagnosticar():
    print(f"painel: {_painel()}\n")
    vhosts = [v["name"] for v in _api("GET", "/vhosts")]
    print(f"vhost '{VHOST}':", "existe" if VHOST in vhosts else "NÃO existe")
    usuarios = [u["name"] for u in _api("GET", "/users")]
    print(f"usuário '{USUARIO}':", "existe" if USUARIO in usuarios else "NÃO existe")

    if VHOST in vhosts:
        filas = _api("GET", f"/queues/{urllib.parse.quote(VHOST, safe='')}") or []
        print(f"\nfilas em '{VHOST}': {len(filas)}")
        for f in filas:
            print(f"   {f['name']:<24} mensagens={f.get('messages', 0)}")
    print("\nfilas no vhost '/' (outros projetos — não tocamos):")
    for f in _api("GET", "/queues/%2F") or []:
        print(f"   {f['name']}")


def aplicar():
    criados = []
    vhosts = [v["name"] for v in _api("GET", "/vhosts")]
    if VHOST not in vhosts:
        _api("PUT", f"/vhosts/{VHOST}")
        criados.append(f"vhost {VHOST}")

    usuarios = [u["name"] for u in _api("GET", "/users")]
    senha = ""
    if USUARIO not in usuarios:
        senha = secrets.token_urlsafe(24)
        _api("PUT", f"/users/{USUARIO}", {"password": senha, "tags": ""})
        criados.append(f"usuário {USUARIO}")

    # permissão só neste vhost: este usuário não enxerga as filas do CJPG
    _api(
        "PUT",
        f"/permissions/{VHOST}/{USUARIO}",
        {"configure": ".*", "write": ".*", "read": ".*"},
    )

    vh = urllib.parse.quote(VHOST, safe="")
    _api("PUT", f"/exchanges/{vh}/{TROCA_DLQ}", {"type": "direct", "durable": True})
    for nome, opcoes in FILAS.items():
        _api("PUT", f"/queues/{vh}/{nome}", opcoes)
        criados.append(f"fila {nome}")
    _api(
        "POST",
        f"/bindings/{vh}/e/{TROCA_DLQ}/q/agente.entrada.dlq",
        {"routing_key": "agente.entrada.dlq"},
    )

    print("criado/garantido:", ", ".join(criados) or "nada (já existia)")
    if senha:
        print(
            f"\nUsuário '{USUARIO}' criado. Guarde no .env e no CREDENCIAIS.md:\n"
            f"  RABBITMQ_AGENTE_URL=amqp://{USUARIO}:{senha}@85.31.63.37:5672/{VHOST}"
        )
    else:
        print(f"\nUsuário '{USUARIO}' já existia — senha não é recuperável pela API.")


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--aplicar", action="store_true", help="cria o que faltar")
    args = p.parse_args()
    if not _painel():
        raise SystemExit("RABBITMQ_PAINEL não configurado no .env")
    aplicar() if args.aplicar else diagnosticar()
    if not args.aplicar:
        print("\n(somente diagnóstico — rode com --aplicar pra criar)")
