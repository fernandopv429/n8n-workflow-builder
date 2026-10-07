"""Cria projeto e chave da OpenAI por cliente, pela Administration API.

Antes, pra clonar um agente era preciso ir no painel da OpenAI, criar uma
chave à mão e colar no formulário. Isso tinha dois problemas além do trabalho:
a chave costumava ser a mesma pra todo mundo (um cliente conseguia gastar o
crédito dos outros, e não dava pra saber quanto cada um custava) e ninguém
lembrava de revogá-la quando o cliente saía.

Aqui cada cliente ganha um PROJETO próprio na organização e uma chave de
service account dentro dele. O faturamento da OpenAI separa por projeto, e
arquivar o projeto revoga todas as chaves dele de uma vez.

A `OPENAI_ADMIN_KEY` (prefixo `sk-admin-`) é de ORGANIZAÇÃO: cria e apaga
projetos, chaves e membros de toda a conta. Ela mora só no .env/variável de
ambiente, nunca no banco e nunca num manifesto salvo — ao contrário das
chaves de projeto, ela não é escopada a nada.

Quando `OPENAI_ADMIN_KEY` não está configurada, nada aqui é usado: o painel
continua pedindo a chave no formulário, como antes.
"""
import json
import urllib.error
import urllib.request

import config

BASE = "https://api.openai.com/v1/organization"
TIMEOUT = 40


class OpenAiAdminError(RuntimeError):
    """Falha na Administration API, já com o corpo do erro junto — a OpenAI
    explica o motivo no corpo, e perder isso foi o que fez a gente passar horas
    chutando no Kommo (ver PADROES-AGENTES-IA-A5ECO.md)."""

    def __init__(self, codigo: int, corpo: str):
        self.codigo = codigo
        self.corpo = corpo
        super().__init__(f"OpenAI Admin API respondeu {codigo}: {corpo}")


def disponivel() -> bool:
    """Se não houver chave de admin, o painel volta a pedir a chave no
    formulário — o recurso é opcional, não quebra quem não configurou."""
    return bool(config.carregar_env().get("OPENAI_ADMIN_KEY", "").strip())


def _chamar(metodo: str, caminho: str, corpo: dict | None = None) -> dict:
    chave = config.carregar_env().get("OPENAI_ADMIN_KEY", "").strip()
    if not chave:
        raise OpenAiAdminError(0, "OPENAI_ADMIN_KEY não configurada")

    dados = json.dumps(corpo).encode() if corpo is not None else None
    req = urllib.request.Request(
        f"{BASE}{caminho}",
        data=dados,
        method=metodo,
        headers={"Authorization": f"Bearer {chave}", "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resposta:
            return json.loads(resposta.read() or "{}")
    except urllib.error.HTTPError as e:
        raise OpenAiAdminError(e.code, e.read().decode("utf-8", "replace")[:500]) from None


def nome_do_projeto(cliente_nome: str) -> str:
    """Prefixo pra dar pra olhar o painel da OpenAI e saber na hora o que é
    projeto de cliente da agência e o que é projeto interno."""
    return f"A5 {cliente_nome}".strip()[:100]


def criar_projeto_e_chave(cliente_nome: str) -> dict:
    """Devolve {projeto_id, projeto_nome, api_key}.

    A chave só é devolvida UMA vez, na criação do service account — a OpenAI
    não permite lê-la de novo depois. Por isso ela vai direto pra credencial do
    n8n e não é guardada no nosso banco: se precisar de outra, cria-se outro
    service account.
    """
    projeto = _chamar("POST", "/projects", {"name": nome_do_projeto(cliente_nome)})
    projeto_id = projeto["id"]

    conta = _chamar(
        "POST", f"/projects/{projeto_id}/service_accounts", {"name": "agente-n8n"}
    )
    api_key = (conta.get("api_key") or {}).get("value")
    if not api_key:
        # projeto sem chave não serve pra nada e ficaria órfão no painel
        arquivar_projeto(projeto_id)
        raise OpenAiAdminError(0, f"service account criado sem api_key: {conta}")

    return {
        "projeto_id": projeto_id,
        "projeto_nome": projeto.get("name"),
        "api_key": api_key,
    }


def arquivar_projeto(projeto_id: str) -> dict:
    """Arquivar revoga todas as chaves do projeto de uma vez. A OpenAI não
    oferece exclusão de projeto — arquivado é o estado final."""
    return _chamar("POST", f"/projects/{projeto_id}/archive")


def listar_projetos(limite: int = 100) -> list:
    return _chamar("GET", f"/projects?limit={limite}").get("data", [])


def consumo_do_projeto(projeto_id: str, dias: int = 30) -> dict:
    """Quanto este cliente consumiu: US$ no período e tokens por modelo.

    Só é possível porque cada cliente tem projeto próprio desde 29/09/2026 —
    antes toda a organização usava um projeto só, e a pergunta "quanto esse
    cliente custa" não tinha resposta.

    Devolve `disponivel: False` em vez de levantar exceção: isto alimenta uma
    tela, e cliente antigo (sem projeto) ou Admin API fora do ar são informação,
    não erro do painel.
    """
    import time

    if not projeto_id:
        return {"disponivel": False,
                "motivo": "este cliente não tem projeto próprio na OpenAI — foi "
                          "criado antes da separação por cliente, ou com chave colada à mão.",
                "dias": dias}
    if not disponivel():
        return {"disponivel": False, "motivo": "OPENAI_ADMIN_KEY não configurada", "dias": dias}

    # `limit` é o número de BALDES, não de linhas, e o padrão é um balde por
    # dia. A OpenAI recusa acima de 31 ("Limit exceeds the maximum allowed value
    # of 31 for the given bucket_width") — com 180 a aba Consumo falhava sempre,
    # pra todo cliente que tivesse projeto.
    dias = max(1, min(dias, 31))
    inicio = int(time.time()) - dias * 24 * 3600
    try:
        custos = _chamar(
            "GET", f"/costs?start_time={inicio}&group_by[]=project_id&limit={dias}")
        uso = _chamar(
            "GET",
            f"/usage/completions?start_time={inicio}&group_by[]=project_id"
            f"&group_by[]=model&limit={dias}")
    except OpenAiAdminError as e:
        return {"disponivel": False, "motivo": str(e), "dias": dias}

    total = 0.0
    for balde in custos.get("data", []):
        for r in balde.get("results", []):
            if r.get("project_id") == projeto_id:
                total += float((r.get("amount") or {}).get("value") or 0)

    por_modelo = {}
    for balde in uso.get("data", []):
        for r in balde.get("results", []):
            if r.get("project_id") != projeto_id:
                continue
            m = por_modelo.setdefault(
                r.get("model") or "?",
                {"entrada": 0, "saida": 0, "cache": 0, "requisicoes": 0})
            m["entrada"] += r.get("input_tokens") or 0
            m["saida"] += r.get("output_tokens") or 0
            m["cache"] += r.get("input_cached_tokens") or 0
            m["requisicoes"] += r.get("num_model_requests") or 0

    return {
        "disponivel": True,
        "dias": dias,
        "projeto_id": projeto_id,
        "custo_usd": round(total, 4),
        "por_modelo": [{"modelo": k, **v} for k, v in
                       sorted(por_modelo.items(), key=lambda kv: -kv[1]["entrada"])],
    }


if __name__ == "__main__":  # diagnóstico: só leitura
    if not disponivel():
        raise SystemExit("OPENAI_ADMIN_KEY não configurada")
    for p in listar_projetos():
        print(f"{p['id']}  {p['name']:<32} {p.get('status')}")
