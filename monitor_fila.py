"""Quantas mensagens estão esperando, e quantas falharam.

Sem isto, worker morto é uma falha silenciosa: as mensagens entram em
`agente.entrada`, ninguém consome, e o único sintoma é um paciente reclamando
que não foi respondido. Mensagem na dead-letter é pior ainda — já falhou, e sem
alguém olhando fica lá pra sempre.

Usa a credencial ADMIN (`RABBITMQ_URL`), não a do worker: o usuário `agente` foi
criado sem tags de management de propósito — ele precisa de AMQP, não de API de
gerenciamento. O painel é quem administra; o worker só trabalha.
"""
import base64
import json
import urllib.error
import urllib.parse
import urllib.request

import config

VHOST = "agente"
FILAS = ("agente.entrada", "agente.saida", "agente.entrada.dlq")
TIMEOUT = 8


def _credenciais() -> tuple:
    p = urllib.parse.urlparse(config.carregar_env().get("RABBITMQ_URL", ""))
    return urllib.parse.unquote(p.username or ""), urllib.parse.unquote(p.password or "")


def resumo() -> dict:
    """Nunca levanta exceção: isto alimenta uma tela, e broker fora do ar é
    informação, não motivo pra devolver 500 no painel inteiro.

    Os números atrasam alguns segundos: a API de gerenciamento do RabbitMQ serve
    estatísticas coletadas em intervalo, não o estado instantâneo da fila
    (verificado em 02/10/2026 — publiquei uma mensagem e o contador só subiu na
    consulta seguinte). Para uma tela de acompanhamento isso é irrelevante, mas
    não serve pra teste automatizado de "publiquei agora, apareceu agora".
    """
    painel = config.carregar_env().get("RABBITMQ_PAINEL", "").rstrip("/")
    usuario, senha = _credenciais()
    if not painel or not usuario:
        return {"disponivel": False, "motivo": "RabbitMQ não configurado", "filas": []}

    cred = base64.b64encode(f"{usuario}:{senha}".encode()).decode()
    filas = []
    for nome in FILAS:
        caminho = f"/api/queues/{urllib.parse.quote(VHOST, safe='')}/{urllib.parse.quote(nome, safe='')}"
        req = urllib.request.Request(f"{painel}{caminho}",
                                     headers={"Authorization": f"Basic {cred}"})
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
                d = json.loads(r.read())
        except (urllib.error.URLError, json.JSONDecodeError, TimeoutError) as e:
            return {"disponivel": False, "motivo": f"não consegui falar com o RabbitMQ: {e}",
                    "filas": []}
        filas.append({
            "nome": nome,
            "mensagens": d.get("messages", 0),
            "sem_consumidor": d.get("messages_ready", 0),
            "consumidores": d.get("consumers", 0),
        })

    entrada = next((f for f in filas if f["nome"] == "agente.entrada"), {})
    dlq = next((f for f in filas if f["nome"] == "agente.entrada.dlq"), {})

    # Dois alertas que valem mais que os números crus, porque descrevem o que
    # está acontecendo com o paciente do outro lado.
    alertas = []
    esperando = entrada.get("mensagens", 0)
    # Avisar de worker ausente enquanto NINGUÉM publica na fila seria alarme
    # falso desde o primeiro dia — e alarme que toca sempre deixa de ser lido
    # justamente quando passa a valer. Só vira alerta quando há mensagem de
    # paciente parada esperando atendimento.
    if entrada.get("consumidores", 0) == 0 and esperando:
        alertas.append(
            f"{esperando} mensagem(ns) esperando e nenhum worker conectado — "
            "ninguém está respondendo. Confira o recurso com PAPEL=worker no Coolify."
        )
    elif entrada.get("mensagens", 0) > 20:
        alertas.append(
            f"{entrada['mensagens']} mensagens acumuladas — o worker não está "
            "dando conta do volume."
        )
    if dlq.get("mensagens", 0):
        alertas.append(
            f"{dlq['mensagens']} mensagem(ns) falharam e estão paradas na "
            "dead-letter. Elas NÃO serão respondidas até alguém agir."
        )

    return {"disponivel": True, "filas": filas, "alertas": alertas}
