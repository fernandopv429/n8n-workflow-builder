"""Cliente fino para a API pública do n8n (só biblioteca padrão — sem `requests`).

Cobre só o que o cloner.py precisa: ler/criar/atualizar/ativar/desativar workflow.
Autenticação: header X-N8N-API-KEY, lido de .env nesta mesma pasta.
"""
import json
import os
import pathlib
import urllib.error
import urllib.request

RAIZ = pathlib.Path(__file__).resolve().parent

from config import carregar_env  # noqa: E402




class N8nError(RuntimeError):
    def __init__(self, status, body):
        super().__init__(f"n8n respondeu {status}: {body}")
        self.status = status
        self.body = body


class N8nClient:
    def __init__(self, base_url: str = None, api_key: str = None):
        env = carregar_env()
        self.base_url = (base_url or env.get("N8N_URL", "")).rstrip("/")
        self.api_key = api_key or env.get("N8N_API_KEY", "")
        if not self.base_url or not self.api_key:
            raise RuntimeError("N8N_URL/N8N_API_KEY não configurados (ver .env)")

    def _request(self, method: str, path: str, body: dict = None) -> dict:
        url = f"{self.base_url}/api/v1{path}"
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("X-N8N-API-KEY", self.api_key)
        req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                raw = resp.read()
                return json.loads(raw) if raw else {}
        except urllib.error.HTTPError as e:
            raise N8nError(e.code, e.read().decode("utf-8", "replace")) from None

    def get_workflow(self, workflow_id: str) -> dict:
        return self._request("GET", f"/workflows/{workflow_id}")

    def criar_credencial(self, nome: str, tipo: str, dados: dict) -> dict:
        """POST /api/v1/credentials. Devolve {id, name, type, ...} — nunca devolve
        o segredo de volta (a API não lê credencial, só cria)."""
        return self._request("POST", "/credentials", {"name": nome, "type": tipo, "data": dados})

    def listar_credenciais(self, tipo: str = None) -> list:
        """GET /api/v1/credentials — ao contrário do que constava antes, a API
        pública LISTA credencial sim (confirmado 18/09/2026, n8n.a5ecossistema.tech
        respondeu 200 com nome/id/tipo de todas). Não devolve o segredo, só metadado."""
        dados = self._request("GET", "/credentials")
        itens = dados.get("data", [])
        if tipo:
            itens = [c for c in itens if c.get("type") == tipo]
        return [{"id": c["id"], "name": c["name"]} for c in itens]

    def listar_workflows(self) -> list:
        return self._request("GET", "/workflows?limit=250").get("data", [])

    def paths_de_webhook_em_uso(self) -> set:
        """Todo `path` de node webhook já existente na instância. Serve pra
        escolher um path livre antes de criar — ativar um workflow cujo path
        já pertence a outro devolve 409 'conflict with one of the webhooks'."""
        usados = set()
        for w in self.listar_workflows():
            for n in w.get("nodes", []):
                if n.get("type") == "n8n-nodes-base.webhook":
                    caminho = n.get("parameters", {}).get("path")
                    if caminho:
                        usados.add(caminho)
        return usados

    def create_workflow(self, payload: dict) -> dict:
        return self._request("POST", "/workflows", payload)

    def delete_workflow(self, workflow_id: str) -> dict:
        return self._request("DELETE", f"/workflows/{workflow_id}")

    def delete_credencial(self, credencial_id: str) -> dict:
        return self._request("DELETE", f"/credentials/{credencial_id}")

    def update_workflow(self, workflow_id: str, payload: dict) -> dict:
        return self._request("PUT", f"/workflows/{workflow_id}", payload)

    def activate_workflow(self, workflow_id: str) -> dict:
        return self._request("POST", f"/workflows/{workflow_id}/activate")

    def deactivate_workflow(self, workflow_id: str) -> dict:
        return self._request("POST", f"/workflows/{workflow_id}/deactivate")
