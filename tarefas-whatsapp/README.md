# Tarefas e Projetos via WhatsApp

Sistema separado do agente de clínica que já existe neste repositório (pasta
raiz) — sem relação funcional com ele. Fica isolado nesta pasta porque não
havia como criar um repositório novo no GitHub a partir desta sessão
(permissão da integração).

## O que faz

1. **Painel web** — cadastro e login por **email**, lista e cria projetos e
   tarefas.
2. **WhatsApp** — manda uma mensagem em português normal ("cria uma tarefa
   pra ligar pro fornecedor até sexta") e a IA cria/consulta tarefas e
   projetos sozinha (function calling da OpenAI).
3. **Lembrete automático** — a cada 15 min, um job verifica tarefas com prazo
   chegando e manda aviso pelo WhatsApp (até 24h antes, configurável em
   `app/services/scheduler.py:JANELA_LEMBRETE_MINUTOS`).
4. **Transcrição de reunião do Meet** — um endpoint (`/api/reunioes/ingest`)
   recebe a transcrição + o email de quem é dono da reunião, acha o usuário
   cadastrado com esse email, extrai projeto e tarefas com IA e grava tudo
   vinculado a essa pessoa.
5. **Usuário master** — o **primeiro cadastro feito no painel vira master
   automaticamente** e passa a enxergar/gerenciar tarefas e projetos de todo
   mundo. Os demais só veem os próprios.

## Por que o Meet fica no n8n, e não aqui

Seguindo o mesmo princípio já usado no projeto do agente de clínica
(`ARQUITETURA-AGENTE.md` na raiz do repo): integração nativa do Google
(OAuth, Drive, Docs) fica no n8n, que já sabe fazer isso bem; a inteligência
(extrair tarefa da transcrição, decidir de quem é, mandar WhatsApp) fica aqui
em Python, testável.

### Desenho do workflow no n8n (a construir lá)

1. **Gatilho**: agendado (ex: a cada 10 min) ou trigger do Google Drive,
   olhando a pasta onde o Meet salva a gravação/transcrição de cada reunião
   (uma subpasta por reunião).
2. **Pega o email do dono** da subpasta: `owners[0].emailAddress`, com
   fallback para `lastModifyingUser.emailAddress` (pastas em Drive
   Compartilhado às vezes não têm `owners`).
3. **Acha o Google Doc da transcrição** dentro da subpasta (mimeType
   `application/vnd.google-apps.document`, nome contendo "Transcri").
4. **Lê o conteúdo** do Doc (node Google Docs → Get).
5. **Chama este serviço**:
   ```
   POST https://SEU-DOMINIO/api/reunioes/ingest
   Authorization: Bearer <MEET_INGEST_TOKEN>
   Content-Type: application/json

   {
     "pasta_drive_id": "<id da subpasta, pra não processar 2x>",
     "titulo": "<nome da subpasta>",
     "email_dono": "<email extraído no passo 2>",
     "transcricao": "<texto do Doc>"
   }
   ```
   Se o email não bate com ninguém cadastrado no painel, o serviço responde
   `200 {"criado": false, "motivo": "email não cadastrado"}` e não cria nada
   — não processa reunião de gente de fora.

## Rodar local

```bash
cd tarefas-whatsapp
cp .env.example .env   # preencher DATABASE_URL, OPENAI_API_KEY, etc.
pip install -r requirements.txt
uvicorn app.main:app --reload --port 8100
```

Primeira pessoa a acessar `http://localhost:8100/registrar` vira o master.

O schema do Postgres (`schema.sql`) é criado sozinho na subida
(`garantir_schema()`), mesmo padrão do projeto da raiz.

## Variáveis de ambiente

Ver `.env.example` para a lista completa com comentário de cada uma.
Resumo do que é obrigatório: `DATABASE_URL`, `OPENAI_API_KEY`,
`EVOLUTION_URL`/`EVOLUTION_APIKEY`/`EVOLUTION_INSTANCE`, `SESSION_SECRET`,
`MEET_INGEST_TOKEN`.

## Webhook da Evolution API

Aponte o webhook de `messages.upsert` da sua instância da Evolution API para:

```
POST https://SEU-DOMINIO/webhook/whatsapp
```

Mensagem de número que não está cadastrado no painel (campo
`telefone_whatsapp` do usuário) é ignorada silenciosamente — só processa
quem está cadastrado.

## Testado nesta sessão

- Schema aplicado contra Postgres 16 real.
- Fluxo completo via HTTP (TestClient): registro (1º vira master, 2º não),
  login/logout, criação de projeto/tarefa, conclusão de tarefa.
- Isolamento de dados confirmado: usuário comum não vê projeto/tarefa de
  outro; master vê de todos.
- Autenticação do endpoint de reunião (`401` sem token / com token errado).
- Webhook do WhatsApp ignora número não cadastrado sem quebrar.

Não testado (exige credenciais reais): chamada de verdade à OpenAI (function
calling do WhatsApp e extração da transcrição) e envio real pela Evolution
API — a lógica está implementada e os formatos de payload seguem a
documentação oficial de cada serviço, mas vale um teste ponta a ponta com as
chaves reais antes de liberar pra uso.
