# Deploy no Coolify

## 1. Repositório

O Coolify puxa de um repositório git. Este projeto ainda não tem um — criar um
repo (privado) e subir. **Confira antes do primeiro push que o `.env` não está
sendo versionado** (ele está no `.gitignore`, mas vale conferir com
`git status`): esse arquivo tem token do n8n, chave da OpenAI e string de
conexão do Postgres em texto puro.

## 2. Criar o recurso

No Coolify: **New Resource → Application → Dockerfile** (o `Dockerfile` está na
raiz, não precisa de build pack). Porta interna: `8099`.

## 3. Variáveis de ambiente

Cadastrar em **Environment Variables**. Os valores reais estão no `.env` local
e em `~/Área de trabalho/Conexão_geral/CREDENCIAIS.md`.

| Variável | Para quê | Obrigatória |
|---|---|---|
| `PAINEL_USUARIO` | usuário do basic auth (padrão `admin` se omitida) | não |
| `PAINEL_SENHA` | senha do basic auth | **sim** |
| `N8N_URL` | `https://n8n.a5ecossistema.tech` | **sim** |
| `N8N_API_KEY` | API pública do n8n (`aud: public-api`) | **sim** |
| `DATABASE_URL` | Postgres do estado (clientes/chat/logs) | **sim** |
| `OPENAI_API_KEY` | chat do painel e geração a partir do briefing | **sim** |
| `OPENAI_ADMIN_KEY` | cria projeto + chave por cliente (`sk-admin-...`) | não |
| `AGENTOPS_API_KEY` | rastreio das chamadas de IA | não |
| `POCKETBASE_URL` | `https://db.a5ecossistema.tech` | só p/ imagem |
| `POCKETBASE_ADMIN_EMAIL` | superusuário do PocketBase | só p/ imagem |
| `POCKETBASE_ADMIN_PASSWORD` | senha desse superusuário | só p/ imagem |
| `SOURCE_COMMIT` | commit mostrado em `/saude` (o Coolify costuma injetar) | não |
| `PORT` | o Coolify costuma injetar; padrão `8099` | não |

**Sem `PAINEL_SENHA` o painel responde 401 em tudo.** É de propósito — falha
fechada, porque este sistema edita workflow n8n e CRM de cliente real e gasta
crédito da OpenAI. Melhor ficar inacessível do que aberto.

**`OPENAI_ADMIN_KEY` é opcional, mas sem ela o campo "Chave da API" volta a ser
obrigatório no formulário** — e aí todos os clientes acabam compartilhando a
mesma chave, que é o que impedia medir consumo por cliente. Com ela, deixar o
campo vazio cria um projeto `A5 {cliente}` com chave exclusiva.

Atenção ao poder dessa chave: ela é de **organização**, não é escopada a
projeto nenhum. Cria e apaga projetos, chaves e membros de toda a conta OpenAI
do Grupo A5. Quem tiver acesso às variáveis do Coolify tem isso — mesmo nível
de cuidado da senha do painel.

## 3.1 Jeito recomendado: Docker Compose (painel + worker juntos)

**New Resource → Application → Docker Compose**, mesmo repositório. O
`docker-compose.yaml` na raiz sobe os dois serviços de uma vez:

| serviço | `PAPEL` | porta | domínio |
|---|---|---|---|
| `painel` | `painel` | 8099 | sim (o Coolify gera) |
| `worker` | `worker` | nenhuma | não |

As variáveis são cadastradas **uma vez só**, em Environment Variables, e o
compose distribui pra cada serviço o que ele precisa. Isso evita o problema de
duas cópias divergirem — trocar a `COFRE_CHAVE` num recurso e esquecer o outro
faz o worker parar de decifrar o token do Kommo, sem erro óbvio.

O worker continua sendo um processo separado: redeploy do painel não derruba
conversa de paciente, e a `OPENAI_ADMIN_KEY` não entra no processo que lê texto
escrito por desconhecido no WhatsApp (ver comentários no próprio compose).

Para rodar local: `docker compose up --build` (lê o `.env` da pasta).

## 3.2 Alternativa: dois recursos separados

O painel e o worker são processos diferentes e sobem separados — mesma imagem,
mesmo repositório, só muda a variável `PAPEL`:

| | painel | worker |
|---|---|---|
| `PAPEL` | `painel` (ou vazio) | `worker` |
| o que faz | criar/gerenciar agentes | atender paciente, consumindo a fila |
| porta | `8099` | nenhuma (não escuta HTTP) |
| domínio | sim | **não** |
| healthcheck | `/saude` | dispensado (ver Dockerfile) |

No Coolify: **New Resource → Application → Dockerfile**, mesmo repositório,
`PAPEL=worker`, sem domínio e sem porta publicada. As variáveis de banco,
OpenAI e `RABBITMQ_AGENTE_URL` são as mesmas do painel.

**Enquanto não existir esse segundo recurso, o worker simplesmente não roda** —
o `agente.entrada` enche e ninguém consome. Até o passo 3 da migração (trilho
genérico no n8n) ninguém publica nessa fila, então isso é inofensivo agora; vira
problema no dia que o primeiro cliente for apontado.

Para dois ou mais workers em paralelo, resolver antes a ordem por contato
(ARQUITETURA-AGENTE.md): com mais de um consumidor, duas mensagens seguidas do
mesmo número podem ser respondidas fora de ordem.

## 4. Healthcheck

`GET /saude` — única rota sem autenticação, justamente pro healthcheck do
Coolify/Docker conseguir chamar. Devolve:

- `200 {"status":"ok","banco":"ok","versao":"1d806e2","no_ar_desde":"..."}`

`versao` e `no_ar_desde` existem pra responder "esse commit já subiu?" sem
ter que reproduzir o bug — o Coolify não redeploya sozinho a cada push. O
commit vem de `SOURCE_COMMIT` (o Coolify injeta nos deploys de git) ou de
`APP_COMMIT`, se quiser passar à mão; sem nenhuma das duas vem
`"desconhecida"`, e aí `no_ar_desde` ainda diz se houve redeploy.

- `503 {"status":"degradado","banco":"..."}` se o Postgres não responder

O `Dockerfile` já traz um `HEALTHCHECK` equivalente para `docker run` avulso.

## 5. Primeira subida

O schema do banco é criado sozinho na subida (`db.garantir_schema()`), junto
com o template padrão do nicho `clinica`. Se o banco estiver fora do ar, o
container **não** morre em loop: sobe, loga o aviso e `/saude` responde 503 —
é mais fácil de diagnosticar que um crash-loop.

## Verificação pós-deploy

```bash
curl -s https://SEU-DOMINIO/saude
curl -s -o /dev/null -w '%{http_code}\n' https://SEU-DOMINIO/          # 401
curl -s -o /dev/null -w '%{http_code}\n' -u admin:SENHA https://SEU-DOMINIO/  # 200
```

## Imagem dos cards

Cada cliente pode ter uma imagem, guardada no PocketBase compartilhado da A5
(`db.a5ecossistema.tech`, coleção `painel_agentes_imagens` — um registro por
imagem, leitura pública, escrita só com superusuário). O Postgres guarda só o
vínculo (`clientes.imagem_pb_record_id`/`imagem_pb_filename`). Sem as
variáveis `POCKETBASE_*`, o painel funciona normal e o upload responde 503.

## O que ficou de fora da imagem

`.env`, `fixtures/` (JSONs de workflow de cliente real, com token do Kommo
dentro), `.pylibs/` e `testes.py` — ver `.dockerignore`.

## Ponto de atenção

O chat do painel tem acesso **irrestrito** à estrutura dos workflows n8n
(criar/editar/remover node e conexões) e ao CRM Kommo via MCP, por decisão
tomada em 18/09/2026. As únicas travas são: basic auth na frente, backup
automático do workflow antes de cada mudança estrutural (fica nos logs do
cliente) e os nodes `AgenteMov`/`Database`, que não podem ser renomeados nem
removidos. Quem tiver a senha tem esse poder todo — trate-a como trataria a
senha do próprio n8n.
