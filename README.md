# n8n workflow builder

Frente separada (agência A5 Ecossistema, `n8n.a5ecossistema.tech`), sem relação
funcional com o projeto "Petição api chatwott". Gerencia os agentes de IA que
atendem no WhatsApp pelos clientes da A5: cadastro de cliente, credenciais
(Kommo CRM + OpenAI), prompt, ferramentas MCP e monitoramento, tudo pelo
painel web (`servidor.py`/`index.html`).

Dependências Python vão em `.pylibs/` (sem pip global nesta máquina — ver
`python-sem-pip-usa-pylibs` na memória do projeto "Petição api chatwott").

## Arquitetura (desde 15/10/2026)

Ver [ARQUITETURA-AGENTE.md](ARQUITETURA-AGENTE.md) para o desenho completo e o
porquê da mudança. Resumo:

```
WhatsApp → n8n (trilho genérico)            → RabbitMQ        → Python (worker)
           webhook · mídia→texto · debounce    agente.entrada    agente · travas · MCP
                                                      ↓
           Evolution envia  ←  n8n consome  ←  agente.saida
```

- **`servidor.py`/`index.html`** — painel web (`localhost:8099`): cadastro de
  cliente, credenciais, prompt, ferramentas MCP, logs e consumo. Processo
  único, sem tráfego de paciente passando por ele.
- **`worker_agente.py`** — processo separado que atende o paciente de verdade:
  consome `agente.entrada`, responde em `agente.saida`. Lê prompt/Kommo/OpenAI
  direto do Postgres por cliente — **não existe mais clonagem de workflow no
  n8n**. O trilho no n8n é UM só, genérico pra todos os clientes.
- **`agente_chat.py`** — chat de gerenciamento do painel: edita o prompt do
  agente e estrutura/analisa o Kommo do cliente (funil, etapas, campos,
  notas, tarefas) via o MCP do Kommo hospedado no próprio n8n.
- **`agente_geral.py`** — número de WhatsApp da própria A5, por onde o cliente
  pede mudança no agente dele (ver prompt, trocar prompt, ligar/desligar).
- **`db.py`** — todo o estado fica no Postgres: cliente, credenciais
  (cifradas via `cofre.py`), prompt (com backup/restauração da versão
  anterior), ferramentas MCP habilitadas por cliente, histórico de chat/logs.

Até 15/10/2026 cada cliente ganhava uma cópia de um workflow de ~128 nodes no
n8n — era a origem de quase todo bug do projeto (sub-workflow apontando pro
cliente errado, credencial esquecida, prompt do template sobrando no clone) e
não dava pra testar. O motor de clonagem (`cloner.py` e companhia) foi
removido do repositório depois que todos os clientes migraram pro desenho
novo (30/10/2026).

## Kommo CRM — em escopo

Diferente da decisão de 18/09/2026 (abaixo, mantida só como histórico): hoje
o agente e o chat de gerenciamento **usam** o Kommo — mover lead de etapa,
preencher campo, criar nota/tarefa (agente do WhatsApp) e estruturar funil
inteiro (chat de gerenciamento). A integração não é feita aqui: é um MCP já
hospedado no próprio n8n (`Kommo MCP - Completo` para estruturação,
`MCP Kommo — Cartão do Lead` para ação do agente), chamado por
`mcp_kommo_client.py` com a credencial do cliente injetada por chamada — nunca
fixa no MCP.

## Decisão histórica — escopo original (18/09/2026)

> Fica só o agente do n8n. Estruturar o Kommo (pipeline/etapas/campos do CRM)
> é fora de escopo — assume-se que o CRM do cliente já foi montado à mão antes
> da clonagem.

Superada pela mudança de arquitetura e pelo pedido do Fernando de dar ao
agente e ao chat acesso ao Kommo (ver seção acima).

Ver `~/Área de trabalho/Conexão_geral/CREDENCIAIS.md` para o registro
completo de credenciais e decisões.
