# n8n workflow builder

Frente separada (agência A5 Ecossistema, `n8n.a5ecossistema.tech`), sem relação
funcional com o projeto "Petição api chatwott". Objetivo: automatizar a
criação/clonagem de workflows de agente de IA no n8n para novos clientes da A5
(hoje feito manualmente na UI — ver "Armadilhas ao clonar workflow de outro
cliente" em `~/Área de trabalho/Conexão_geral/PADROES-AGENTES-IA-A5ECO.md`).

Escopo confirmado com o Fernando em 18/09/2026: um agente construído com o SDK
da OpenAI, monitorado via AgentOps, que executa a clonagem (troca `base-url`/
`kommo-token` no node `Database`, reescreve prompts do agente principal e do
`AgenteMov`, confere `field_id`/`status_id` de sub-workflow) em vez de fazer
isso manualmente na UI do n8n.

Dependências Python vão em `.pylibs/` (sem pip global nesta máquina — ver
`python-sem-pip-usa-pylibs` na memória do projeto "Petição api chatwott").

## Estado (18/09/2026)

Implementado e testado: `cloner.py` (motor determinístico — 8/8 testes em
`testes.py` contra 4 workflows reais), `servidor.py`/`index.html` (dashboard
web em `localhost:8099`, histórico em Postgres via `db.py`),
`prompt_gerador.py` (OpenAI + AgentOps, testado isolado). Falta: plugar
`prompt_gerador.py` no formulário web (precisa de campo "nome do agente"); e
rodar a primeira clonagem REAL de teste ponta a ponta (só dry-run testado
contra o n8n até agora).

**Correção 18/09/2026, a pedido do Fernando ("faça uma verificação, pra ver o
que realmente precisa")**: o manifesto original pedia `evolution_instancia`/
`evolution_api_key`/`evolution_server_url` por cliente — verificado contra os
4 workflows reais que isso é falso. Os nodes que enviam mensagem usam uma
única credencial n8n compartilhada entre todos os clientes; os campos de
mesmo nome no node `Database`, quando existem, vêm dinamicamente do payload
do webhook em tempo de execução, e nada no workflow os lê depois — não é dado
de cliente. Campos removidos do formulário. Também corrigido: a nota antiga
dizendo que a API do n8n não lista credencial por nome estava errada —
`GET /api/v1/credentials` lista sim — então o campo de credencial OpenAI virou
um dropdown ao vivo (`servidor.py` → `/credenciais-openai`) em vez de exigir
colar o ID manualmente.

## Escopo — decisão do Fernando (18/09/2026)

Fica só o agente do n8n. Estruturar o Kommo (pipeline/etapas/campos do CRM) é
**fora de escopo** — assume-se que o CRM do cliente já foi montado à mão antes
da clonagem. Se isso mudar no futuro, **não construir um cliente Kommo direto
aqui** — usar o MCP do Kommo que já existe hospedado no próprio n8n (workflow
`Kommo MCP - Completo (Funil, Etapas, Leads, Contatos)`, id `bSZFMIchpke716zJ`
em `n8n.a5ecossistema.tech`), não duplicar a integração.

Ver `~/Área de trabalho/Conexão_geral/CREDENCIAIS.md` para o registro
completo de credenciais e decisões.
