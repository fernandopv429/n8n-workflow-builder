"""Cria e configura instância de WhatsApp na Evolution API, do painel.

Fecha o ciclo de provisionamento: ao cadastrar um cliente, o painel já cria o
projeto na OpenAI (openai_admin.py), guarda as credenciais do Kommo (cofre.py)
e agora também cria a instância do WhatsApp e aponta o webhook dela pro trilho.
Antes isso era trabalho manual no manager da Evolution, e o passo mais fácil de
esquecer era justamente o webhook — instância conectada sem webhook recebe
mensagem e não avisa ninguém, o que parece "o agente não respondeu".

Evolution v2 (2.3.7 em 06/10/2026). A `EVOLUTION_APIKEY` é a key GLOBAL: lê e
administra TODAS as instâncias da conta, inclusive as de clientes reais. Mora
só no .env/variável de ambiente.

Ponto de atenção: conectar o WhatsApp exige escanear um QR code, que expira em
cerca de 40 segundos. Por isso `criar_instancia` devolve o QR e existe
`obter_qr` pra gerar outro — não dá pra automatizar essa parte, alguém precisa
estar com o celular na mão.
"""
import json
import urllib.error
import urllib.parse
import urllib.request

import config

TIMEOUT = 40

# Só os eventos que o trilho usa. Assinar tudo faria o webhook receber dezenas
# de eventos por minuto (presença, status de entrega, QR) que o trilho descarta
# — gasto de rede e log sujo pra nada.
EVENTOS = ["MESSAGES_UPSERT"]


class EvolutionError(RuntimeError):
    def __init__(self, codigo: int, corpo: str):
        self.codigo = codigo
        self.corpo = corpo
        super().__init__(f"Evolution API respondeu {codigo}: {corpo}")


def disponivel() -> bool:
    env = config.carregar_env()
    return bool(env.get("EVOLUTION_URL", "").strip() and env.get("EVOLUTION_APIKEY", "").strip())


def _chamar(metodo: str, caminho: str, corpo: dict | None = None):
    env = config.carregar_env()
    base = env.get("EVOLUTION_URL", "").strip().rstrip("/")
    chave = env.get("EVOLUTION_APIKEY", "").strip()
    if not base or not chave:
        raise EvolutionError(0, "EVOLUTION_URL/EVOLUTION_APIKEY não configuradas")

    dados = json.dumps(corpo).encode() if corpo is not None else None
    req = urllib.request.Request(
        f"{base}{caminho}",
        data=dados,
        method=metodo,
        headers={"apikey": chave, "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            bruto = r.read()
            return json.loads(bruto) if bruto else {}
    except urllib.error.HTTPError as e:
        raise EvolutionError(e.code, e.read().decode("utf-8", "replace")[:400]) from None


def url_do_trilho() -> str:
    """Webhook do trilho genérico (trilho_n8n.py). UM só pra todos os clientes
    — é a instância no payload que diz de quem é a mensagem."""
    base = config.carregar_env().get("N8N_URL", "").strip().rstrip("/")
    return f"{base}/webhook/a5-trilho-entrada"


def nome_da_instancia(cliente_nome: str) -> str:
    """Nome previsível, pra dar pra olhar o manager da Evolution e saber de quem
    é cada instância sem consultar o banco."""
    limpo = "".join(c if (c.isalnum() or c in " -_") else "" for c in cliente_nome).strip()
    return f"a5-{limpo.lower().replace(' ', '-')}"[:50]


def listar_instancias() -> list:
    d = _chamar("GET", "/instance/fetchInstances")
    itens = d if isinstance(d, list) else d.get("instances", [])
    saida = []
    for i in itens:
        inst = i.get("instance", i)
        saida.append({
            "nome": inst.get("instanceName") or inst.get("name"),
            "status": inst.get("connectionStatus") or inst.get("status"),
            "numero": (inst.get("ownerJid") or "").split("@")[0],
            "perfil": inst.get("profileName"),
        })
    return saida


def existe(nome: str) -> bool:
    return any(i["nome"] == nome for i in listar_instancias())


def criar_instancia(nome: str) -> dict:
    """Cria a instância JÁ com o webhook do trilho apontado.

    Apontar o webhook na criação, e não num passo seguinte, é de propósito: uma
    instância conectada sem webhook recebe mensagem de paciente e não entrega a
    ninguém — falha silenciosa, que parece "o agente não respondeu".
    """
    corpo = {
        "instanceName": nome,
        "qrcode": True,
        "integration": "WHATSAPP-BAILEYS",
        "webhook": {
            "url": url_do_trilho(),
            "byEvents": False,   # um endpoint só; quem separa evento é o trilho
            "base64": False,
            "events": EVENTOS,
        },
    }
    d = _chamar("POST", "/instance/create", corpo)
    qr = d.get("qrcode") or {}
    return {
        "nome": nome,
        "webhook": url_do_trilho(),
        "qr_base64": qr.get("base64") or "",
        "qr_codigo": qr.get("code") or "",
        "status": (d.get("instance") or {}).get("status") or "criada",
    }


def definir_webhook(nome: str, url: str = "") -> dict:
    """Aponta (ou reaponta) o webhook. Sem `url`, usa o do trilho.

    CUIDADO: trocar o webhook de uma instância em produção redireciona as
    mensagens dos pacientes daquele cliente. Quem chama tem que saber disso —
    aqui não há como distinguir instância de teste de instância real.
    """
    return _chamar("POST", f"/webhook/set/{urllib.parse.quote(nome)}", {
        "webhook": {
            "enabled": True,
            "url": url or url_do_trilho(),
            "byEvents": False,
            "base64": False,
            "events": EVENTOS,
        }
    })


def obter_webhook(nome: str) -> str:
    d = _chamar("GET", f"/webhook/find/{urllib.parse.quote(nome)}") or {}
    return (d or {}).get("url") or ""


def obter_qr(nome: str) -> dict:
    """QR novo pra conectar. O anterior expira em ~40s, então a tela precisa
    poder pedir outro sem recriar a instância (o que perderia a conexão)."""
    d = _chamar("GET", f"/instance/connect/{urllib.parse.quote(nome)}")
    return {"qr_base64": d.get("base64") or "", "qr_codigo": d.get("code") or "",
            "ja_conectada": bool(d.get("instance"))}


def status(nome: str) -> str:
    try:
        d = _chamar("GET", f"/instance/connectionState/{urllib.parse.quote(nome)}")
    except EvolutionError:
        return "inexistente"
    return ((d or {}).get("instance") or {}).get("state") or "desconhecido"


def remover_instancia(nome: str) -> dict:
    """Desconecta e apaga. Usado quando o cliente sai — instância órfã continua
    conectada ao WhatsApp dele, recebendo mensagem que ninguém lê."""
    try:
        _chamar("DELETE", f"/instance/logout/{urllib.parse.quote(nome)}")
    except EvolutionError:
        pass  # já desconectada é o estado que queremos
    return _chamar("DELETE", f"/instance/delete/{urllib.parse.quote(nome)}")


if __name__ == "__main__":  # diagnóstico: só leitura
    if not disponivel():
        raise SystemExit("EVOLUTION_URL/EVOLUTION_APIKEY não configuradas")
    print(f"trilho: {url_do_trilho()}\n")
    for i in listar_instancias():
        print(f"  {str(i['nome']):<30} {str(i['status']):<8} {i['numero']}")
