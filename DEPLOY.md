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
| `AGENTOPS_API_KEY` | rastreio das chamadas de IA | não |
| `POCKETBASE_URL` | `https://db.a5ecossistema.tech` | só p/ imagem |
| `POCKETBASE_ADMIN_EMAIL` | superusuário do PocketBase | só p/ imagem |
| `POCKETBASE_ADMIN_PASSWORD` | senha desse superusuário | só p/ imagem |
| `PORT` | o Coolify costuma injetar; padrão `8099` | não |

**Sem `PAINEL_SENHA` o painel responde 401 em tudo.** É de propósito — falha
fechada, porque este sistema edita workflow n8n e CRM de cliente real e gasta
crédito da OpenAI. Melhor ficar inacessível do que aberto.

## 4. Healthcheck

`GET /saude` — única rota sem autenticação, justamente pro healthcheck do
Coolify/Docker conseguir chamar. Devolve:

- `200 {"status":"ok","banco":"ok"}`
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
