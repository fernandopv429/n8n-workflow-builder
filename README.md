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
inteiro (chat de gerenciamento). Dois caminhos diferentes, de propósito:

- **Agente do WhatsApp (`worker_agente.py`)** — as 8 ferramentas do "Cartão do
  Lead" (`consultar_cartao_lead`, `mover_lead`, `atualizar_campo_personalizado`,
  `adicionar_tag`, `remover_tag`, `adicionar_nota`, `criar_tarefa`,
  `buscar_lead_por_telefone`) são **nativas** (`kommo_cartao.py`, chamada HTTP
  direta, sem MCP) — eram um MCP até 30/10/2026; virou nativo porque é o
  grosso do tráfego de ferramenta do atendimento e cada chamada via MCP pagava
  handshake + negociação de transporte à toa.
- **Chat de gerenciamento (`agente_chat.py`)** — continua via MCP já hospedado
  no n8n (`Kommo MCP - Completo`, 17 ferramentas de estruturação: funil,
  etapas, campos personalizados), chamado por `mcp_kommo_client.py` com a
  credencial do cliente injetada por chamada — nunca fixa no MCP. Fica
  escondido da lista de ferramentas que aparece pro cliente (sempre ativo,
  só pro chat).

## Ativação por tag no Kommo + webhook

O agente do WhatsApp só responde um contato se o lead dele no Kommo tiver a
tag **"IA ATIVA"** e não tiver a tag **"LQIA"** (transferido pra humano) —
convenção geral, vale pra qualquer cliente com Kommo. Sem Kommo cadastrado,
sem lead encontrado, ou erro de rede: fail-open, responde normal (nunca quebra
quem não usa essa convenção).

Um **webhook do Kommo** (`POST /clientes/<id>/kommo/webhook` registra;
`POST /webhooks/kommo/<cliente_id>/<token>` recebe os eventos `add_lead`,
`update_lead` e `status_lead`) mantém um espelho local (`kommo_leads`:
telefone → lead_id, tags, etapa) atualizado em tempo real, e já marca "IA
ATIVA" sozinho quando um lead novo aparece — só processa leads cujo telefone
já tem contato nosso (`contatos`), nunca mexe em lead de outro funil do
cliente. Sem webhook registrado, ou lead criado antes dele existir, cai numa
busca ao vivo (telefone → contato → lead, via `kommo_cartao.buscar_lead_por_telefone`)
como reserva — mais lenta, mas cobre o caso.

Esse mesmo cache local alimenta o chat de gerenciamento: se a mensagem citar
um telefone, o lead correspondente é resolvido ANTES do modelo agir (cache
primeiro, busca ao vivo só se faltar) — evita o modelo inventar um `lead_id`
(telefone, zero, `status_id`) quando não sabe o de verdade.

## QA, métricas e análise de atendimento

- **QA de atendimento** (`qa_atendimento.py`) — um juiz (LLM) relê a conversa
  de um contato contra o prompt do próprio cliente e aponta severidade/
  problemas. Acumulado, vira sugestão de melhoria de prompt (humano aprova,
  nunca aplica sozinho).
- **Métricas de ferramentas** — quantas vezes cada ferramenta é chamada,
  tempo médio e taxa de erro, nos três agentes (worker/chat/geral). Painel:
  um ranking de barra por métrica (chamadas, tempo médio, taxa de erro — cada
  um com sua escala, não misturado numa barra só).
- **Análise de atendimento** — quando o agente pediu um atendente e quanto
  tempo levou até uma pessoa responder de verdade no WhatsApp (detecção via
  `fromMe: true` do Evolution — mensagem mandada pela nossa API não volta
  assim, só mensagem mandada por um humano de verdade). Painel: lista de
  handoffs (barra) + linha de tendência do tempo médio de resposta por dia.
- **Follow-up automático** — se o paciente não responde depois de uma
  mensagem da IA, e passa o tempo configurado por cliente
  (`clientes.follow_up_horas`, 0/vazio desliga), a IA gera e manda **uma**
  mensagem tentando retomar a conversa, no próprio tom/persona — sem repetir
  pergunta genérica, respeitando o mesmo gating de tag do Kommo. Disparado por
  um Schedule Trigger do n8n chamando `POST /follow-up/rodar-todos`.

## Decisão histórica — escopo original (18/09/2026)

> Fica só o agente do n8n. Estruturar o Kommo (pipeline/etapas/campos do CRM)
> é fora de escopo — assume-se que o CRM do cliente já foi montado à mão antes
> da clonagem.

Superada pela mudança de arquitetura e pelo pedido do Fernando de dar ao
agente e ao chat acesso ao Kommo (ver seção acima).

Ver `~/Área de trabalho/Conexão_geral/CREDENCIAIS.md` para o registro
completo de credenciais e decisões.
