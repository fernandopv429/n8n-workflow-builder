"""Descreve os dados de um cliente novo necessários para clonar o agente no n8n."""
from dataclasses import dataclass, field

CAMPOS_OBRIGATORIOS = (
    "cliente_nome",
    "nicho",
    "kommo_subdominio",
    "kommo_token",
    "openai_api_key",
    "workflow_origem_id",
)

# Campos que o manifesto NÃO pede, verificado contra os 4 workflows reais
# (18/09/2026, a pedido do Fernando — "faça uma verificação, pra ver o que
# realmente precisa"):
#
# - evolution_instancia / evolution_api_key / evolution_server_url: os nodes
#   evolutionApi (envio de mensagem etc.) usam uma ÚNICA credencial n8n
#   compartilhada ("Evolution account", id kDkNqoImm6fZEdkx) nos 4 workflows —
#   não muda por cliente. Os campos de mesmo nome no node Database, quando
#   existem, são expressão dinâmica lida do payload do webhook em tempo de
#   execução (`{{ $('Webhook').first().json.body.apikey }}`), não configuração
#   estática — e nenhum node em nenhum dos 4 workflows sequer LÊ esses dois
#   campos do Database depois. Não são um dado de cliente real.
# - openai_credential_id/name: não são mais digitados pelo usuário (18/09/2026,
#   "as credenciais vai ser cadastrada, então não vai ter opção de seleção") —
#   o usuário cola a chave da OpenAI (`openai_api_key`) e o cloner.py cria a
#   credencial de verdade no n8n (`POST /api/v1/credentials`) durante a
#   clonagem real. `openai_credential_id` só é preenchido DEPOIS disso — fica
#   vazio até lá (nunca chamado no caminho de dry-run, que não cria nada).


@dataclass
class ClienteManifest:
    cliente_nome: str
    nicho: str
    kommo_subdominio: str
    kommo_token: str
    openai_api_key: str
    workflow_origem_id: str
    # base-url do node Database é o subdomínio do Kommo, já confirmado
    # intercambiável entre .kommo.com/.amocrm.com (ver PADROES-AGENTES-IA-A5ECO.md)
    kommo_base_url: str = field(init=False)
    # nome padrão da credencial OpenAI, mesmo padrão já usado pelos clientes
    # reais ("Dr. Marcos Key OpenIA", "Dra. Fabiana Key OpenIA", etc.)
    openai_credential_name: str = field(init=False)
    # preenchido por executar_clone_real() depois de criar a credencial de
    # verdade — não vem do formulário.
    openai_credential_id: str = ""

    def __post_init__(self):
        self.kommo_base_url = self.kommo_subdominio
        self.openai_credential_name = f"{self.cliente_nome} Key OpenIA"

    def validar(self):
        faltando = [c for c in CAMPOS_OBRIGATORIOS if not getattr(self, c, "").strip()]
        if faltando:
            raise ValueError(f"Campos obrigatórios faltando: {', '.join(faltando)}")

    @classmethod
    def de_dict(cls, dados: dict) -> "ClienteManifest":
        valores = {c: str(dados.get(c, "")).strip() for c in CAMPOS_OBRIGATORIOS}
        m = cls(**valores)
        m.validar()
        return m
