"""Estado dos clientes no Postgres (host 85.31.63.37): cada cliente nasce
"rascunho" no passo 1 (só o nome), vira "ativo" quando a engrenagem salva
Kommo + OpenAI (ver ARQUITETURA-AGENTE.md — não existe mais clonagem de
workflow no n8n). `chat_mensagens`/`logs` dão suporte à tela de
gerenciamento.
"""
import json
import pathlib
import sys

RAIZ = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(RAIZ / ".pylibs"))

from config import carregar_env  # noqa: E402

import psycopg  # noqa: E402




def _conectar():
    return psycopg.connect(carregar_env()["DATABASE_URL"])


def garantir_schema():
    with _conectar() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS clientes (
                id SERIAL PRIMARY KEY,
                cliente_nome TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'rascunho'
                    CHECK (status IN ('rascunho', 'ativo', 'erro')),
                criado_em TIMESTAMPTZ NOT NULL DEFAULT now(),
                atualizado_em TIMESTAMPTZ NOT NULL DEFAULT now()
            )
        """)
        # Migração 30/10/2026 (pedido do Fernando): nicho/workflow_origem_id só
        # existiam pra escolher o workflow-molde a clonar — sem clone (ver
        # migração clonado->ativo acima), nada no sistema lê mais esses campos.
        # briefing_texto/batch_id/batch_status eram do fluxo antigo de briefing
        # no passo 1 (saiu em 25/09/2026 pro chat de gerenciamento, que usa a
        # tabela `chat_lotes`, não estas colunas). manifesto/avisos nunca
        # chegaram a ser lidos por nenhum código.
        conn.execute("ALTER TABLE clientes DROP COLUMN IF EXISTS nicho")
        conn.execute("ALTER TABLE clientes DROP COLUMN IF EXISTS workflow_origem_id")
        conn.execute("ALTER TABLE clientes DROP COLUMN IF EXISTS briefing_texto")
        conn.execute("ALTER TABLE clientes DROP COLUMN IF EXISTS batch_id")
        conn.execute("ALTER TABLE clientes DROP COLUMN IF EXISTS batch_status")
        conn.execute("ALTER TABLE clientes DROP COLUMN IF EXISTS manifesto")
        conn.execute("ALTER TABLE clientes DROP COLUMN IF EXISTS avisos")
        conn.execute("DROP TABLE IF EXISTS templates_nicho")
        conn.execute("ALTER TABLE clientes ADD COLUMN IF NOT EXISTS prompt_sugerido TEXT")
        conn.execute("ALTER TABLE clientes ADD COLUMN IF NOT EXISTS estrutura_kommo_sugerida JSONB")
        # Imagem do card: o arquivo vive no PocketBase compartilhado, aqui fica
        # só o vínculo (mesmo padrão do cadastro-veiculos).
        conn.execute("ALTER TABLE clientes ADD COLUMN IF NOT EXISTS imagem_pb_record_id TEXT")
        conn.execute("ALTER TABLE clientes ADD COLUMN IF NOT EXISTS imagem_pb_filename TEXT")
        # Projeto da OpenAI criado pra este cliente (openai_admin.py). Guardamos
        # só o ID: é o que permite perguntar quanto ESTE cliente consumiu
        # (/organization/costs?group_by=project_id) e arquivar o projeto quando
        # ele sai, revogando as chaves de uma vez.
        conn.execute("ALTER TABLE clientes ADD COLUMN IF NOT EXISTS openai_projeto_id TEXT")
        # Chave do projeto OpenAI DESTE cliente, cifrada (cofre.py). Sem ela o
        # worker usaria a chave global pra todo mundo, e aí o consumo de todos
        # cairia num projeto só — exatamente o que a separação por cliente veio
        # resolver. A OpenAI só mostra a chave uma vez, na criação, então ou
        # guardamos aqui ou ela se perde.
        conn.execute("ALTER TABLE clientes ADD COLUMN IF NOT EXISTS openai_api_key_cifrada TEXT")
        # Credenciais do Kommo. Até 02/10/2026 moravam SÓ no node `Database` do
        # workflow clonado — o que amarrava o atendimento à existência do clone,
        # e o desenho novo não tem clone (ARQUITETURA-AGENTE.md). O subdomínio é
        # público (aparece na URL da conta); o token é cifrado.
        conn.execute("ALTER TABLE clientes ADD COLUMN IF NOT EXISTS kommo_subdominio TEXT")
        conn.execute("ALTER TABLE clientes ADD COLUMN IF NOT EXISTS kommo_token_cifrado TEXT")
        # Instância do Evolution (WhatsApp) deste cliente. É a chave que o
        # trilho genérico do n8n manda na fila pra dizer DE QUEM é a mensagem —
        # sem ela não existe um workflow só pra todos, volta a ser um por
        # cliente. UNIQUE porque duas contas na mesma instância misturariam
        # conversa de pacientes de clientes diferentes.
        conn.execute("ALTER TABLE clientes ADD COLUMN IF NOT EXISTS evolution_instancia TEXT")
        # Liga/desliga do atendimento. Serve pra pausar um cliente sem
        # desconectar o WhatsApp nem apagar nada: desligado, o worker recebe a
        # mensagem, GRAVA na conversa e não responde. A mensagem do paciente não
        # se perde — fica lá pra alguém ler quando religar ou assumir.
        conn.execute("ALTER TABLE clientes ADD COLUMN IF NOT EXISTS agente_ativo BOOLEAN NOT NULL DEFAULT TRUE")
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_clientes_instancia "
            "ON clientes (evolution_instancia) WHERE evolution_instancia IS NOT NULL"
        )
        # Grupo do WhatsApp (JID, ex: 1203630...-...@g.us) onde o resumo
        # periódico de atendimento deste cliente é mandado. Fica vazio até
        # alguém cadastrar — sem grupo configurado, o cliente simplesmente não
        # entra no envio (ver resumo_atendimento em servidor.py).
        conn.execute("ALTER TABLE clientes ADD COLUMN IF NOT EXISTS whatsapp_grupo_aviso TEXT")
        # Backup de uma versão do prompt (pedido do Fernando, 28/10/2026): toda
        # troca guarda o valor anterior aqui, pra dar pra desfazer uma edição
        # ruim — seja feita no painel, pelo chat de gerenciamento, ou pelo
        # próprio agente geral no WhatsApp (ver restaurar_prompt_anterior).
        conn.execute("ALTER TABLE clientes ADD COLUMN IF NOT EXISTS prompt_anterior TEXT")
        # Migração 15/10/2026 (pedido do Fernando): o desenho de clonar um
        # workflow por cliente no n8n foi substituído pelo worker central
        # (worker_agente.py), que lê prompt/Kommo/OpenAI direto do Postgres —
        # não existe mais "clone" nenhum. Clientes que ficaram com
        # status='clonado' do desenho antigo viram 'ativo'; os workflows deles
        # no n8n foram apagados à parte, então workflow_novo_id/url não
        # apontam mais pra nada e são zerados junto.
        # A constraint velha só aceitava 'clonado', não 'ativo' — precisa cair
        # ANTES do UPDATE, senão o Postgres rejeita a troca de valor (e, como
        # tudo aqui roda numa transação só, a migração inteira falha calada:
        # o garantir_schema() captura a exceção pra não derrubar o container,
        # e nem a coluna whatsapp_grupo_aviso chega a ser criada).
        conn.execute("ALTER TABLE clientes DROP CONSTRAINT IF EXISTS clientes_status_check")
        conn.execute(
            "UPDATE clientes SET status = 'ativo', workflow_novo_id = NULL, "
            "workflow_novo_url = NULL WHERE status = 'clonado'"
        )
        conn.execute(
            "ALTER TABLE clientes ADD CONSTRAINT clientes_status_check "
            "CHECK (status IN ('rascunho', 'ativo', 'erro'))"
        )
        # Só depois da migração acima (que ainda escreve nelas pra zerar
        # clientes vindos do 'clonado') é que workflow_novo_id/url somem de
        # vez — 30/10/2026, junto com o resto do clonador (cloner.py etc.).
        conn.execute("ALTER TABLE clientes DROP COLUMN IF EXISTS workflow_novo_id")
        conn.execute("ALTER TABLE clientes DROP COLUMN IF EXISTS workflow_novo_url")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS chat_mensagens (
                id SERIAL PRIMARY KEY,
                cliente_id INTEGER NOT NULL REFERENCES clientes(id),
                role TEXT NOT NULL CHECK (role IN ('user', 'assistant', 'tool')),
                conteudo TEXT NOT NULL,
                criado_em TIMESTAMPTZ NOT NULL DEFAULT now()
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS logs (
                id SERIAL PRIMARY KEY,
                cliente_id INTEGER NOT NULL REFERENCES clientes(id),
                tipo TEXT NOT NULL CHECK (tipo IN ('tool_call', 'erro', 'sistema')),
                mensagem TEXT NOT NULL,
                detalhe JSONB,
                criado_em TIMESTAMPTZ NOT NULL DEFAULT now()
            )
        """)
        conn.execute("""
            -- Mensagem do chat enviada pela Batch API: a resposta chega minutos
            -- ou horas depois, então precisa ficar registrada pra ser buscada
            -- quando alguém reabrir a conversa.
            CREATE TABLE IF NOT EXISTS chat_lotes (
                id SERIAL PRIMARY KEY,
                cliente_id INTEGER NOT NULL REFERENCES clientes(id),
                batch_id TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'in_progress',
                pergunta TEXT NOT NULL,
                criado_em TIMESTAMPTZ NOT NULL DEFAULT now()
            )
        """)

        # Ferramentas por cliente. Integração nova = MCP novo no n8n, e aqui
        # fica só quem usa o quê — nenhum código novo por integração.
        #
        # `no_atendimento` separa os dois agentes: o chat do painel é operado
        # por gente da A5, o do WhatsApp recebe texto de desconhecido. Uma base
        # de conhecimento pode ir pros dois; "cancelar agendamento" talvez não.
        conn.execute("""
            CREATE TABLE IF NOT EXISTS cliente_mcps (
                id SERIAL PRIMARY KEY,
                cliente_id INTEGER NOT NULL REFERENCES clientes(id) ON DELETE CASCADE,
                apelido TEXT NOT NULL,
                path TEXT NOT NULL,
                token_cifrado TEXT,
                ativo BOOLEAN NOT NULL DEFAULT TRUE,
                no_atendimento BOOLEAN NOT NULL DEFAULT FALSE,
                criado_em TIMESTAMPTZ NOT NULL DEFAULT now(),
                UNIQUE (cliente_id, apelido)
            )
        """)

        # Arquivos que o agente pode ENVIAR: vídeo de preparo de exame, PDF de
        # orientação, tabela de preço. O agente escolhe pelo `nome`, nunca por
        # URL — URL vinda do modelo é URL influenciada pelo texto do paciente.
        #
        # O arquivo em si mora no PocketBase (hub de arquivos da A5, o mesmo do
        # cadastro-veiculos), não em disco nosso: volume esquecido no redeploy
        # apaga arquivo em silêncio e deixa a linha aqui apontando pra nada. A
        # coleção tem leitura pública, que é o que a Evolution precisa pra
        # baixar — ela não sabe autenticar. Guardamos só a referência.
        conn.execute("""
            CREATE TABLE IF NOT EXISTS cliente_midias (
                id SERIAL PRIMARY KEY,
                cliente_id INTEGER NOT NULL REFERENCES clientes(id) ON DELETE CASCADE,
                nome TEXT NOT NULL,
                descricao TEXT NOT NULL DEFAULT '',
                pb_record_id TEXT NOT NULL,
                pb_filename TEXT NOT NULL,
                nome_original TEXT NOT NULL,
                mimetype TEXT NOT NULL,
                tamanho INTEGER NOT NULL DEFAULT 0,
                criado_em TIMESTAMPTZ NOT NULL DEFAULT now(),
                UNIQUE (cliente_id, nome)
            )
        """)

        # A primeira versão desta tabela guardava caminho em disco. Nunca chegou
        # a produção com conteúdo, então trocar as colunas não perde nada.
        for ddl in (
            "ALTER TABLE cliente_midias ADD COLUMN IF NOT EXISTS pb_record_id TEXT NOT NULL DEFAULT ''",
            "ALTER TABLE cliente_midias ADD COLUMN IF NOT EXISTS pb_filename TEXT NOT NULL DEFAULT ''",
            "ALTER TABLE cliente_midias DROP COLUMN IF EXISTS arquivo",
            "ALTER TABLE cliente_midias DROP COLUMN IF EXISTS id_publico",
        ):
            conn.execute(ddl)

        # Agente geral: um número da A5 por onde o PRÓPRIO CLIENTE pede mudanças
        # no agente dele ("troca meu prompt", "desliga"). Nada a ver com o
        # atendimento ao paciente.
        #
        # `configuracoes` guarda qual instância do Evolution é essa — é
        # configuração do sistema, não de cliente, e por isso não cabe em
        # `clientes`.
        conn.execute("""
            CREATE TABLE IF NOT EXISTS configuracoes (
                chave TEXT PRIMARY KEY,
                valor TEXT,
                atualizado_em TIMESTAMPTZ NOT NULL DEFAULT now()
            )
        """)
        # Quem pode pedir, e POR QUAL cliente. Esta tabela é a única autorização
        # que existe aqui: o número de telefone define o que a pessoa pode
        # mudar. Nada do que ela ESCREVER altera isso — "sou da clínica tal" é
        # texto, não credencial.
        #
        # UNIQUE no telefone: um número fala por UM cliente. Sem isso, um
        # demandante cadastrado duas vezes mudaria o agente errado conforme a
        # ordem da consulta.
        conn.execute("""
            CREATE TABLE IF NOT EXISTS demandantes (
                id SERIAL PRIMARY KEY,
                id_whatsapp TEXT NOT NULL UNIQUE,
                cliente_id INTEGER NOT NULL REFERENCES clientes(id) ON DELETE CASCADE,
                nome TEXT,
                ativo BOOLEAN NOT NULL DEFAULT TRUE,
                criado_em TIMESTAMPTZ NOT NULL DEFAULT now()
            )
        """)

        # --- estado do agente de atendimento (ARQUITETURA-AGENTE.md) -------
        # Substitui a tabela `clientes_<nome>` do Supabase, que era UMA POR
        # CLIENTE — o mesmo vício do clone, com DDL a cada cliente novo. Aqui é
        # uma tabela só, com cliente_id. Colunas espelham as que os workflows
        # realmente usam: id_whatsapp, nome, status ('HUMANO' no handoff),
        # ultima_mensagem e repondeu_follow.
        conn.execute("""
            CREATE TABLE IF NOT EXISTS contatos (
                id SERIAL PRIMARY KEY,
                cliente_id INTEGER NOT NULL REFERENCES clientes(id) ON DELETE CASCADE,
                id_whatsapp TEXT NOT NULL,
                nome TEXT,
                status TEXT NOT NULL DEFAULT 'IA'
                    CHECK (status IN ('IA', 'HUMANO')),
                respondeu_follow BOOLEAN NOT NULL DEFAULT FALSE,
                ultima_mensagem TIMESTAMPTZ,
                criado_em TIMESTAMPTZ NOT NULL DEFAULT now(),
                UNIQUE (cliente_id, id_whatsapp)
            )
        """)
        # Memória da conversa, por contato. Separada de chat_mensagens, que é o
        # chat do PAINEL (operador conversando sobre o cliente) — misturar os
        # dois faria o agente responder ao paciente com contexto de manutenção.
        conn.execute("""
            CREATE TABLE IF NOT EXISTS agente_mensagens (
                id SERIAL PRIMARY KEY,
                contato_id INTEGER NOT NULL REFERENCES contatos(id) ON DELETE CASCADE,
                role TEXT NOT NULL,
                conteudo TEXT NOT NULL,
                criado_em TIMESTAMPTZ NOT NULL DEFAULT now()
            )
        """)
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_agente_mensagens_contato "
            "ON agente_mensagens (contato_id, criado_em)"
        )
        # Idempotência: o WhatsApp reentrega, e a fila reentrega no retry. Sem
        # esta trava o paciente recebe a mesma resposta duas vezes. O UNIQUE é a
        # garantia real — checar antes de processar é corrida, não trava.
        conn.execute("""
            CREATE TABLE IF NOT EXISTS mensagens_processadas (
                mensagem_id TEXT PRIMARY KEY,
                cliente_id INTEGER NOT NULL REFERENCES clientes(id) ON DELETE CASCADE,
                processada_em TIMESTAMPTZ NOT NULL DEFAULT now()
            )
        """)


# --- estado do agente de atendimento ------------------------------------

def obter_ou_criar_contato(cliente_id: int, id_whatsapp: str, nome: str = "") -> dict:
    """Devolve o contato, criando se for a primeira mensagem dele.

    `ON CONFLICT DO UPDATE` em vez de SELECT-depois-INSERT: duas mensagens
    simultâneas do mesmo número criariam duas linhas na versão ingênua.
    """
    with _conectar() as conn:
        linha = conn.execute(
            """
            INSERT INTO contatos (cliente_id, id_whatsapp, nome, ultima_mensagem)
            VALUES (%s, %s, NULLIF(%s, ''), now())
            ON CONFLICT (cliente_id, id_whatsapp) DO UPDATE
                SET ultima_mensagem = now(),
                    nome = COALESCE(contatos.nome, EXCLUDED.nome)
            RETURNING id, cliente_id, id_whatsapp, nome, status, respondeu_follow
            """,
            (cliente_id, id_whatsapp, nome or ""),
        ).fetchone()
    campos = ("id", "cliente_id", "id_whatsapp", "nome", "status", "respondeu_follow")
    return dict(zip(campos, linha))


def definir_prompt(cliente_id: int, texto: str):
    """Grava o prompt do agente escrito à mão.

    Até 02/10/2026 esse campo só era preenchido pelo retorno da Batch API, a
    partir do briefing — não havia como escrever nem corrigir uma vírgula sem
    gerar tudo de novo. Com o agente rodando no nosso worker, este texto É o
    system prompt dele (ver worker_agente.py), então precisa ser editável.

    O valor que está saindo vai pra `prompt_anterior` antes de ser sobrescrito
    (pedido do Fernando, 28/10/2026) — é o que permite desfazer uma troca ruim,
    inclusive pelo próprio agente geral no WhatsApp (ver
    restaurar_prompt_anterior). Só guarda UM nível (a troca anterior), não um
    histórico inteiro — é o suficiente pro caso de "acabei de estragar isso".
    """
    with _conectar() as conn:
        conn.execute(
            "UPDATE clientes SET prompt_anterior = prompt_sugerido, "
            "prompt_sugerido = %s, atualizado_em = now() WHERE id = %s",
            (texto, cliente_id),
        )


def restaurar_prompt_anterior(cliente_id: int) -> bool:
    """Desfaz a última troca de prompt, trocando de volta com `prompt_anterior`.

    É uma TROCA, não uma cópia — chamar de novo desfaz o desfazer (volta pro
    que estava antes do restore), então restaurar duas vezes seguidas não
    perde nada. Devolve False se não há o que restaurar (cliente novo, ou
    prompt nunca foi trocado)."""
    with _conectar() as conn:
        linha = conn.execute(
            "SELECT prompt_sugerido, prompt_anterior FROM clientes WHERE id = %s",
            (cliente_id,),
        ).fetchone()
        if not linha or linha[1] is None:
            return False
        atual, anterior = linha
        conn.execute(
            "UPDATE clientes SET prompt_sugerido = %s, prompt_anterior = %s, "
            "atualizado_em = now() WHERE id = %s",
            (anterior, atual, cliente_id),
        )
    return True


def listar_contatos(cliente_id: int, limite: int = 100) -> list:
    """Quem está conversando com o agente deste cliente, mais recente primeiro.

    Traz a última mensagem junto porque a tela sem ela é inútil: uma lista de
    telefones não diz a quem o operador precisa atender primeiro.
    """
    with _conectar() as conn:
        linhas = conn.execute(
            """
            SELECT c.id, c.id_whatsapp, c.nome, c.status, c.ultima_mensagem,
                   (SELECT conteudo FROM agente_mensagens m
                     WHERE m.contato_id = c.id ORDER BY m.criado_em DESC LIMIT 1),
                   (SELECT count(*) FROM agente_mensagens m WHERE m.contato_id = c.id)
              FROM contatos c
             WHERE c.cliente_id = %s
             ORDER BY c.ultima_mensagem DESC NULLS LAST
             LIMIT %s
            """,
            (cliente_id, limite),
        ).fetchall()
    campos = ("id", "id_whatsapp", "nome", "status", "ultima_mensagem",
              "ultima_fala", "total_mensagens")
    # isoformat como o resto do módulo (listar_logs, listar_mensagens): datetime
    # cru estoura no json.dumps da resposta e derruba a conexão sem explicação
    return [
        {**dict(zip(campos, l)),
         "ultima_mensagem": l[4].isoformat() if l[4] else None}
        for l in linhas
    ]


def obter_contato(contato_id: int) -> dict | None:
    with _conectar() as conn:
        l = conn.execute(
            "SELECT id, cliente_id, id_whatsapp, nome, status, ultima_mensagem "
            "FROM contatos WHERE id = %s", (contato_id,)
        ).fetchone()
    if l is None:
        return None
    d = dict(zip(("id", "cliente_id", "id_whatsapp", "nome", "status", "ultima_mensagem"), l))
    d["ultima_mensagem"] = l[5].isoformat() if l[5] else None
    return d


def limpar_conversa(contato_id: int) -> int:
    """Apaga o histórico de um contato e devolve quantas mensagens saíram.

    O contato CONTINUA existindo, com o mesmo id e status — some só a memória.
    É o equivalente ao `Deleta Memoria`/`DELETE HISTORY` dos workflows antigos:
    serve pra testar do zero e pra quando a conversa azeda e é melhor recomeçar
    do que o agente ficar preso num mal-entendido.

    As mensagens já processadas NÃO são esquecidas: a trava de idempotência
    continua valendo, senão uma reentrega do WhatsApp seria respondida de novo.
    """
    with _conectar() as conn:
        n = conn.execute(
            "DELETE FROM agente_mensagens WHERE contato_id = %s", (contato_id,)
        ).rowcount
    return n or 0


def definir_status_contato(contato_id: int, status: str):
    """'HUMANO' tira o contato do atendimento automático — é o handoff. O worker
    confere isso ANTES de responder, senão a IA fala por cima do atendente."""
    if status not in ("IA", "HUMANO"):
        raise ValueError(f"status inválido: {status}")
    with _conectar() as conn:
        conn.execute("UPDATE contatos SET status = %s WHERE id = %s", (status, contato_id))


def salvar_mensagem_agente(contato_id: int, role: str, conteudo: str):
    with _conectar() as conn:
        conn.execute(
            "INSERT INTO agente_mensagens (contato_id, role, conteudo) VALUES (%s, %s, %s)",
            (contato_id, role, conteudo),
        )


def listar_mensagens_agente(contato_id: int, limite: int = 20) -> list:
    """Últimas N em ordem cronológica. O limite existe pelo mesmo motivo do
    MAX_TURNOS_HISTORICO no painel: histórico longo degrada a resposta além de
    custar caro."""
    with _conectar() as conn:
        linhas = conn.execute(
            """
            SELECT role, conteudo FROM (
                SELECT role, conteudo, criado_em FROM agente_mensagens
                WHERE contato_id = %s ORDER BY criado_em DESC LIMIT %s
            ) ultimas ORDER BY criado_em
            """,
            (contato_id, limite),
        ).fetchall()
    return [{"role": r[0], "conteudo": r[1]} for r in linhas]


def definir_grupo_aviso(cliente_id: int, grupo_jid: str):
    """JID do grupo do WhatsApp (ex: 1203630...@g.us) onde o resumo periódico
    de atendimento deste cliente é mandado. Vazio desliga o envio pra ele."""
    with _conectar() as conn:
        conn.execute(
            "UPDATE clientes SET whatsapp_grupo_aviso = NULLIF(%s, ''), atualizado_em = now() "
            "WHERE id = %s",
            (grupo_jid.strip(), cliente_id),
        )


def listar_clientes_com_grupo_aviso() -> list:
    """Só os clientes com grupo cadastrado — é quem entra no envio do resumo
    periódico (ver resumo_atendimento em servidor.py, chamado pelo n8n)."""
    with _conectar() as conn:
        linhas = conn.execute(
            "SELECT id, cliente_nome, whatsapp_grupo_aviso FROM clientes "
            "WHERE whatsapp_grupo_aviso IS NOT NULL"
        ).fetchall()
    return [{"id": r[0], "cliente_nome": r[1], "grupo": r[2]} for r in linhas]


def resumo_atendimento(cliente_id: int) -> dict:
    """Quantos contatos distintos a IA atendeu hoje e neste mês.

    'Atendeu' = teve pelo menos uma resposta do agente (role='assistant') no
    período — é a métrica mais simples que já diz algo útil; separar por
    etapa/funil ou contar handoff finalizado fica pra quando isso for mapeado
    direito (o Fernando pediu só esse número por enquanto, 15/10/2026).
    """
    with _conectar() as conn:
        linha = conn.execute(
            """
            SELECT
                count(DISTINCT c.id) FILTER (
                    WHERE m.criado_em >= date_trunc('day', now())
                ),
                count(DISTINCT c.id) FILTER (
                    WHERE m.criado_em >= date_trunc('month', now())
                )
            FROM contatos c
            JOIN agente_mensagens m ON m.contato_id = c.id
            WHERE c.cliente_id = %s AND m.role = 'assistant'
            """,
            (cliente_id,),
        ).fetchone()
    return {"atendidos_hoje": linha[0] or 0, "atendidos_mes": linha[1] or 0}


def registrar_mensagem_processada(mensagem_id: str, cliente_id: int) -> bool:
    """True se é nova, False se já foi processada antes.

    Quem garante é o PRIMARY KEY, não um SELECT anterior: entre o SELECT e o
    INSERT cabe outra entrega da mesma mensagem, e o paciente receberia a
    resposta duas vezes.
    """
    with _conectar() as conn:
        linha = conn.execute(
            "INSERT INTO mensagens_processadas (mensagem_id, cliente_id) VALUES (%s, %s) "
            "ON CONFLICT (mensagem_id) DO NOTHING RETURNING mensagem_id",
            (mensagem_id, cliente_id),
        ).fetchone()
    return linha is not None


# --- clientes ---------------------------------------------------------

def criar_cliente_rascunho(cliente_nome: str) -> int:
    with _conectar() as conn:
        linha = conn.execute(
            "INSERT INTO clientes (cliente_nome) VALUES (%s) RETURNING id",
            (cliente_nome,),
        ).fetchone()
    registrar_log(linha[0], "sistema", f"Cliente '{cliente_nome}' criado (rascunho).")
    return linha[0]


# Segredo não sai daqui: `obter_cliente` é devolvido pro navegador em
# `GET /clientes/<id>`. O token do Kommo e a chave da OpenAI ficam cifrados no
# banco e só são lidos pelas funções próprias (obter_credenciais_kommo,
# obter_chave_openai_cliente), nunca pela leitura genérica.
COLUNAS_SECRETAS = ("kommo_token_cifrado", "openai_api_key_cifrada")


def _linha_para_cliente(cursor, r) -> dict:
    """Mapeia pelo nome da coluna, não pela posição.

    A versão anterior listava as colunas à mão no SELECT e no dict. Toda coluna
    nova precisava ser acrescentada nos dois lugares, e quem esquecia não via
    erro: o campo simplesmente chegava `None`. Foi assim que `openai_projeto_id`
    e `evolution_instancia` ficaram invisíveis em 06/10/2026 — e, como a
    exclusão do cliente usa esses campos pra arquivar o projeto da OpenAI e
    apagar a instância do WhatsApp, a limpeza não acontecia e ninguém era
    avisado.
    """
    nomes = [d[0] for d in cursor.description]
    cliente = {}
    for nome, valor in zip(nomes, r):
        if nome in COLUNAS_SECRETAS:
            continue
        cliente[nome] = valor.isoformat() if hasattr(valor, "isoformat") else valor
    return cliente


def obter_cliente(cliente_id: int) -> dict | None:
    with _conectar() as conn:
        cur = conn.execute("SELECT * FROM clientes WHERE id = %s", (cliente_id,))
        r = cur.fetchone()
        return _linha_para_cliente(cur, r) if r else None


def listar_clientes() -> list:
    with _conectar() as conn:
        cur = conn.execute("SELECT * FROM clientes ORDER BY criado_em DESC")
        return [_linha_para_cliente(cur, r) for r in cur.fetchall()]


def marcar_cliente_ativo(cliente_id: int):
    """Cliente pronto pra atender — sem clone no n8n, o worker_agente.py lê
    prompt/Kommo/OpenAI direto do Postgres a partir daqui."""
    with _conectar() as conn:
        conn.execute(
            "UPDATE clientes SET status = 'ativo', atualizado_em = now() WHERE id = %s",
            (cliente_id,),
        )
    registrar_log(cliente_id, "sistema", "Credenciais salvas — cliente ativo.")


def marcar_cliente_erro(cliente_id: int, erro: str):
    with _conectar() as conn:
        conn.execute(
            "UPDATE clientes SET status = 'erro', atualizado_em = now() WHERE id = %s",
            (cliente_id,),
        )
    registrar_log(cliente_id, "erro", erro)


# --- logs ---------------------------------------------------------

def registrar_log(cliente_id: int, tipo: str, mensagem: str, detalhe: dict = None):
    with _conectar() as conn:
        conn.execute(
            "INSERT INTO logs (cliente_id, tipo, mensagem, detalhe) VALUES (%s, %s, %s, %s)",
            (cliente_id, tipo, mensagem, json.dumps(detalhe) if detalhe is not None else None),
        )


def listar_logs(cliente_id: int, limite: int = 50) -> list:
    with _conectar() as conn:
        linhas = conn.execute(
            """
            SELECT tipo, mensagem, detalhe, criado_em FROM logs
            WHERE cliente_id = %s ORDER BY criado_em DESC LIMIT %s
            """,
            (cliente_id, limite),
        ).fetchall()
    return [
        {"tipo": r[0], "mensagem": r[1], "detalhe": r[2], "criado_em": r[3].isoformat()}
        for r in linhas
    ]


# --- chat ---------------------------------------------------------

def salvar_mensagem(cliente_id: int, role: str, conteudo: str):
    with _conectar() as conn:
        conn.execute(
            "INSERT INTO chat_mensagens (cliente_id, role, conteudo) VALUES (%s, %s, %s)",
            (cliente_id, role, conteudo),
        )


def listar_mensagens(cliente_id: int) -> list:
    with _conectar() as conn:
        linhas = conn.execute(
            """
            SELECT role, conteudo, criado_em FROM chat_mensagens
            WHERE cliente_id = %s ORDER BY criado_em ASC
            """,
            (cliente_id,),
        ).fetchall()
    return [{"role": r[0], "conteudo": r[1], "criado_em": r[2].isoformat()} for r in linhas]


def definir_projeto_openai(cliente_id: int, projeto_id: str, api_key_cifrada: str = ""):
    with _conectar() as conn:
        conn.execute(
            "UPDATE clientes SET openai_projeto_id = %s, "
            "openai_api_key_cifrada = COALESCE(NULLIF(%s, ''), openai_api_key_cifrada), "
            "atualizado_em = now() WHERE id = %s",
            (projeto_id or None, api_key_cifrada, cliente_id),
        )


def obter_cliente_por_instancia(instancia: str) -> dict:
    """De qual cliente é esta instância do Evolution. Devolve {} se nenhuma."""
    if not instancia:
        return {}
    with _conectar() as conn:
        cur = conn.execute(
            "SELECT * FROM clientes WHERE evolution_instancia = %s", (instancia,)
        )
        linha = cur.fetchone()
        return _linha_para_cliente(cur, linha) if linha else {}


def obter_config(chave: str, padrao: str = "") -> str:
    with _conectar() as conn:
        r = conn.execute("SELECT valor FROM configuracoes WHERE chave = %s", (chave,)).fetchone()
    return (r[0] if r else "") or padrao


def definir_config(chave: str, valor: str):
    with _conectar() as conn:
        conn.execute(
            "INSERT INTO configuracoes (chave, valor) VALUES (%s, %s) "
            "ON CONFLICT (chave) DO UPDATE SET valor = EXCLUDED.valor, atualizado_em = now()",
            (chave, valor),
        )


def obter_demandante(id_whatsapp: str) -> dict:
    """Quem é este número e por qual cliente ele fala. {} se não cadastrado.

    Não cadastrado é o caso NORMAL: qualquer um pode mandar mensagem pro número
    da A5. Quem responde decide o que fazer com desconhecido.
    """
    with _conectar() as conn:
        cur = conn.execute(
            "SELECT d.id, d.id_whatsapp, d.cliente_id, d.nome, d.ativo, c.cliente_nome "
            "FROM demandantes d JOIN clientes c ON c.id = d.cliente_id "
            "WHERE d.id_whatsapp = %s",
            (id_whatsapp,),
        )
        r = cur.fetchone()
    if not r:
        return {}
    return dict(zip(["id", "id_whatsapp", "cliente_id", "nome", "ativo", "cliente_nome"], r))


def listar_demandantes() -> list:
    with _conectar() as conn:
        cur = conn.execute(
            "SELECT d.id, d.id_whatsapp, d.cliente_id, d.nome, d.ativo, c.cliente_nome "
            "FROM demandantes d JOIN clientes c ON c.id = d.cliente_id ORDER BY c.cliente_nome"
        )
        return [dict(zip(["id", "id_whatsapp", "cliente_id", "nome", "ativo", "cliente_nome"], r))
                for r in cur.fetchall()]


def salvar_demandante(id_whatsapp: str, cliente_id: int, nome: str = "", ativo: bool = True):
    with _conectar() as conn:
        conn.execute(
            "INSERT INTO demandantes (id_whatsapp, cliente_id, nome, ativo) "
            "VALUES (%s, %s, NULLIF(%s,''), %s) "
            "ON CONFLICT (id_whatsapp) DO UPDATE SET cliente_id = EXCLUDED.cliente_id, "
            "nome = COALESCE(EXCLUDED.nome, demandantes.nome), ativo = EXCLUDED.ativo",
            (id_whatsapp.strip(), cliente_id, nome.strip(), bool(ativo)),
        )


def remover_demandante(id_whatsapp: str):
    with _conectar() as conn:
        conn.execute("DELETE FROM demandantes WHERE id_whatsapp = %s", (id_whatsapp,))


def listar_midias_do_cliente(cliente_id: int) -> list:
    """Arquivos cadastrados deste cliente. Só dele — item de um cliente nunca
    pode ser oferecido ao agente de outro."""
    with _conectar() as conn:
        cur = conn.execute(
            "SELECT id, nome, descricao, pb_record_id, pb_filename, nome_original, "
            "mimetype, tamanho FROM cliente_midias WHERE cliente_id = %s ORDER BY nome",
            (cliente_id,))
        colunas = [d[0] for d in cur.description]
        return [dict(zip(colunas, linha)) for linha in cur.fetchall()]


def obter_midia(cliente_id: int, nome: str) -> dict | None:
    """Busca por nome DENTRO do cliente: é assim que a ferramenta do agente
    resolve o que enviar, e o cliente_id não vem do modelo."""
    with _conectar() as conn:
        cur = conn.execute(
            "SELECT id, nome, descricao, pb_record_id, pb_filename, nome_original, "
            "mimetype, tamanho FROM cliente_midias "
            "WHERE cliente_id = %s AND lower(nome) = lower(%s)", (cliente_id, nome))
        linha = cur.fetchone()
        if not linha:
            return None
        return dict(zip([d[0] for d in cur.description], linha))


def salvar_midia(cliente_id: int, nome: str, descricao: str, pb_record_id: str,
                 pb_filename: str, nome_original: str, mimetype: str,
                 tamanho: int) -> dict:
    with _conectar() as conn:
        cur = conn.execute(
            """INSERT INTO cliente_midias (cliente_id, nome, descricao, pb_record_id,
                   pb_filename, nome_original, mimetype, tamanho)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
               ON CONFLICT (cliente_id, nome) DO UPDATE SET
                   descricao = EXCLUDED.descricao,
                   pb_record_id = EXCLUDED.pb_record_id,
                   pb_filename = EXCLUDED.pb_filename,
                   nome_original = EXCLUDED.nome_original,
                   mimetype = EXCLUDED.mimetype, tamanho = EXCLUDED.tamanho
               RETURNING id, pb_record_id""",
            (cliente_id, nome, descricao, pb_record_id, pb_filename, nome_original,
             mimetype, tamanho))
        linha = cur.fetchone()
        return {"id": linha[0], "pb_record_id": linha[1]}


def apagar_midia(cliente_id: int, midia_id: int) -> str:
    """Devolve o record_id pra quem chamou apagar do PocketBase também — linha
    removida com arquivo órfão deixa lixo acumulando no hub."""
    with _conectar() as conn:
        cur = conn.execute(
            "DELETE FROM cliente_midias WHERE cliente_id = %s AND id = %s "
            "RETURNING pb_record_id", (cliente_id, midia_id))
        linha = cur.fetchone()
        return linha[0] if linha else ""


def listar_mcps_do_cliente(cliente_id: int, so_ativos: bool = False,
                           so_atendimento: bool = False) -> list:
    """MCPs deste cliente, com o token já decifrado.

    `so_atendimento` é o que o worker usa: traz só o que foi marcado como
    liberado pro agente que fala com o paciente.
    """
    import cofre
    filtros = ["cliente_id = %s"]
    if so_ativos or so_atendimento:
        filtros.append("ativo")
    if so_atendimento:
        filtros.append("no_atendimento")
    with _conectar() as conn:
        cur = conn.execute(
            f"SELECT id, apelido, path, token_cifrado, ativo, no_atendimento "
            f"FROM cliente_mcps WHERE {' AND '.join(filtros)} ORDER BY apelido",
            (cliente_id,),
        )
        linhas = cur.fetchall()
    saida = []
    for i, apelido, path, cifrado, ativo, atend in linhas:
        try:
            token = cofre.decifrar(cifrado) if cifrado else ""
        except Exception:  # noqa: BLE001 — cofre trocado não pode esconder o MCP da tela
            token = ""
        # Sem token próprio, cai no bearer compartilhado dos MCPs internos: desde
        # 07/10/2026 todos exigem token, e obrigar a digitar em cada ligação só
        # geraria erro de digitação. MCP de terceiro, com token próprio, continua
        # usando o dele.
        if not token:
            token = carregar_env().get("MCP_TOKEN_INTERNO", "").strip()
        saida.append({"id": i, "apelido": apelido, "path": path, "token": token,
                      "ativo": ativo, "no_atendimento": atend})
    return saida


def quem_usa_cada_mcp() -> dict:
    """{path: [nome do cliente, ...]}.

    Serve pra impedir o pior erro possível nesta tela: ligar a base de
    conhecimento de um cliente no agente de outro. Um clique vazaria dado entre
    clientes, e nada no nome do MCP avisa de quem ele é.
    """
    with _conectar() as conn:
        cur = conn.execute(
            "SELECT m.path, c.cliente_nome FROM cliente_mcps m "
            "JOIN clientes c ON c.id = m.cliente_id"
        )
        saida = {}
        for path, nome in cur.fetchall():
            saida.setdefault(path, []).append(nome)
    return saida


def salvar_mcp_do_cliente(cliente_id: int, apelido: str, path: str, token: str = "",
                          ativo: bool = True, no_atendimento: bool = False):
    """Cria ou atualiza. Token vazio não apaga o que já existe — a tela reenvia
    o formulário inteiro, e campo em branco significa "não mexi nisso"."""
    import cofre
    cifrado = cofre.cifrar(token) if (token and cofre.disponivel()) else ""
    with _conectar() as conn:
        conn.execute(
            """INSERT INTO cliente_mcps (cliente_id, apelido, path, token_cifrado,
                                         ativo, no_atendimento)
               VALUES (%s, %s, %s, NULLIF(%s,''), %s, %s)
               ON CONFLICT (cliente_id, apelido) DO UPDATE SET
                 path = EXCLUDED.path,
                 token_cifrado = COALESCE(NULLIF(EXCLUDED.token_cifrado,''),
                                          cliente_mcps.token_cifrado),
                 ativo = EXCLUDED.ativo,
                 no_atendimento = EXCLUDED.no_atendimento""",
            (cliente_id, apelido.strip(), path.strip(), cifrado, bool(ativo), bool(no_atendimento)),
        )


def remover_mcp_do_cliente(cliente_id: int, apelido: str):
    with _conectar() as conn:
        conn.execute("DELETE FROM cliente_mcps WHERE cliente_id = %s AND apelido = %s",
                     (cliente_id, apelido))


def definir_agente_ativo(cliente_id: int, ativo: bool):
    with _conectar() as conn:
        conn.execute(
            "UPDATE clientes SET agente_ativo = %s, atualizado_em = now() WHERE id = %s",
            (bool(ativo), cliente_id),
        )


def definir_instancia_evolution(cliente_id: int, instancia: str):
    with _conectar() as conn:
        conn.execute(
            "UPDATE clientes SET evolution_instancia = NULLIF(%s, ''), atualizado_em = now() "
            "WHERE id = %s",
            (instancia.strip(), cliente_id),
        )


def definir_credenciais_kommo(cliente_id: int, subdominio: str, token: str):
    """Guarda subdomínio em claro e token cifrado. Token vazio não apaga o que
    já existe — a tela de credenciais reenvia o formulário inteiro, e um campo
    deixado em branco significa "não mexi nisso", não "apague"."""
    import cofre
    cifrado = cofre.cifrar(token) if (token and cofre.disponivel()) else ""
    with _conectar() as conn:
        conn.execute(
            "UPDATE clientes SET kommo_subdominio = COALESCE(NULLIF(%s, ''), kommo_subdominio), "
            "kommo_token_cifrado = COALESCE(NULLIF(%s, ''), kommo_token_cifrado), "
            "atualizado_em = now() WHERE id = %s",
            (subdominio, cifrado, cliente_id),
        )


def obter_credenciais_kommo(cliente_id: int) -> dict:
    """{kommo_domain, access_token} do nosso banco, ou {} se não tiver
    (cadastrada na engrenagem do painel)."""
    import cofre
    with _conectar() as conn:
        linha = conn.execute(
            "SELECT kommo_subdominio, kommo_token_cifrado FROM clientes WHERE id = %s",
            (cliente_id,),
        ).fetchone()
    if not linha or not linha[0] or not linha[1]:
        return {}
    return {"kommo_domain": f"{linha[0]}.kommo.com", "access_token": cofre.decifrar(linha[1])}


def obter_chave_openai_cliente(cliente_id: int) -> str:
    """Chave do projeto deste cliente, já decifrada, ou "" se ele não tiver.

    Quem chama decide o fallback: o worker cai na chave global pra não deixar
    paciente sem resposta, mas registra que o gasto vai pro projeto errado.
    """
    import cofre
    with _conectar() as conn:
        linha = conn.execute(
            "SELECT openai_api_key_cifrada FROM clientes WHERE id = %s", (cliente_id,)
        ).fetchone()
    if not linha or not linha[0]:
        return ""
    return cofre.decifrar(linha[0])


def remover_cliente(cliente_id: int):
    """Apaga o cliente e tudo que pende dele no banco. Não toca na Evolution
    nem na OpenAI — quem decide isso é quem chama (ver evolution_client.
    remover_instancia e openai_admin.arquivar_projeto)."""
    with _conectar() as conn:
        conn.execute("DELETE FROM chat_lotes WHERE cliente_id = %s", (cliente_id,))
        conn.execute("DELETE FROM chat_mensagens WHERE cliente_id = %s", (cliente_id,))
        conn.execute("DELETE FROM logs WHERE cliente_id = %s", (cliente_id,))
        conn.execute("DELETE FROM clientes WHERE id = %s", (cliente_id,))


def definir_imagem(cliente_id: int, record_id: str, filename: str):
    with _conectar() as conn:
        conn.execute(
            "UPDATE clientes SET imagem_pb_record_id = %s, imagem_pb_filename = %s, atualizado_em = now() WHERE id = %s",
            (record_id or None, filename or None, cliente_id),
        )


def registrar_lote_chat(cliente_id: int, batch_id: str, pergunta: str):
    with _conectar() as conn:
        conn.execute(
            "INSERT INTO chat_lotes (cliente_id, batch_id, pergunta) VALUES (%s, %s, %s)",
            (cliente_id, batch_id, pergunta),
        )


def listar_lotes_chat_pendentes(cliente_id: int) -> list:
    with _conectar() as conn:
        linhas = conn.execute(
            """
            SELECT id, batch_id, pergunta FROM chat_lotes
            WHERE cliente_id = %s AND status = 'in_progress' ORDER BY criado_em ASC
            """,
            (cliente_id,),
        ).fetchall()
    return [{"id": r[0], "batch_id": r[1], "pergunta": r[2]} for r in linhas]


def encerrar_lote_chat(lote_id: int, status: str):
    with _conectar() as conn:
        conn.execute("UPDATE chat_lotes SET status = %s WHERE id = %s", (status, lote_id))


if __name__ == "__main__":
    garantir_schema()
    print("schema ok")
    print(json.dumps(listar_clientes(), indent=2, ensure_ascii=False))
