"""Estado dos clientes no Postgres (host 85.31.63.37): cada cliente nasce
"rascunho" no passo 1 (nome + nicho), vira "clonado" quando a engrenagem
dispara a clonagem real. `chat_mensagens`/`logs` dão suporte à tela de
gerenciamento (Fase 2 do plano — chat + caixinha de logs).

Substitui a tabela `clonagens` da Fase 1 (ficou vazia — nenhuma clonagem real
rodou ainda nela, só dry-run — não precisou de migração de dados).
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
                nicho TEXT NOT NULL,
                workflow_origem_id TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'rascunho'
                    CHECK (status IN ('rascunho', 'clonado', 'erro')),
                workflow_novo_id TEXT,
                workflow_novo_url TEXT,
                manifesto JSONB,
                avisos JSONB,
                criado_em TIMESTAMPTZ NOT NULL DEFAULT now(),
                atualizado_em TIMESTAMPTZ NOT NULL DEFAULT now()
            )
        """)
        # Briefing livre (18/09/2026) — gerado via Batch API (briefing_batch.py),
        # não em tempo real, por isso cabe num campo separado consultado sob
        # demanda (poll) em vez de um fluxo síncrono.
        conn.execute("ALTER TABLE clientes ADD COLUMN IF NOT EXISTS briefing_texto TEXT")
        conn.execute("ALTER TABLE clientes ADD COLUMN IF NOT EXISTS batch_id TEXT")
        conn.execute("ALTER TABLE clientes ADD COLUMN IF NOT EXISTS batch_status TEXT")
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
        conn.execute("""
            -- um template padrão por nicho (workflow do n8n usado como base pra
            -- clonar) — vive no banco pra não precisar editar código toda vez
            -- que um nicho novo ganha um cliente-modelo.
            CREATE TABLE IF NOT EXISTS templates_nicho (
                nicho TEXT PRIMARY KEY,
                workflow_origem_id TEXT NOT NULL,
                label TEXT NOT NULL,
                atualizado_em TIMESTAMPTZ NOT NULL DEFAULT now()
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
    _seed_templates_nicho()


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
    """
    with _conectar() as conn:
        conn.execute(
            "UPDATE clientes SET prompt_sugerido = %s, atualizado_em = now() WHERE id = %s",
            (texto, cliente_id),
        )


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


# --- templates por nicho -----------------------------------------------

def definir_template_nicho(nicho: str, workflow_origem_id: str, label: str):
    """Cadastra/substitui o template padrão de um nicho. Só existe UM por nicho —
    quem quiser trocar o padrão chama de novo com o mesmo nicho."""
    with _conectar() as conn:
        conn.execute(
            """
            INSERT INTO templates_nicho (nicho, workflow_origem_id, label, atualizado_em)
            VALUES (%s, %s, %s, now())
            ON CONFLICT (nicho) DO UPDATE
                SET workflow_origem_id = EXCLUDED.workflow_origem_id,
                    label = EXCLUDED.label,
                    atualizado_em = now()
            """,
            (nicho, workflow_origem_id, label),
        )


def obter_template_nicho(nicho: str) -> dict | None:
    with _conectar() as conn:
        r = conn.execute(
            "SELECT nicho, workflow_origem_id, label FROM templates_nicho WHERE nicho = %s",
            (nicho,),
        ).fetchone()
    return {"nicho": r[0], "workflow_origem_id": r[1], "label": r[2]} if r else None


def listar_templates_nicho() -> list:
    with _conectar() as conn:
        linhas = conn.execute(
            "SELECT nicho, workflow_origem_id, label FROM templates_nicho ORDER BY nicho"
        ).fetchall()
    return [{"nicho": r[0], "id": r[1], "label": r[2]} for r in linhas]


def _seed_templates_nicho():
    """Só insere se a tabela estiver vazia pro nicho — nunca sobrescreve o que
    já foi cadastrado/editado depois."""
    with _conectar() as conn:
        conn.execute(
            """
            INSERT INTO templates_nicho (nicho, workflow_origem_id, label)
            VALUES ('clinica', 'Jv3y1QjnT84AHywf',
                    'Fabifisio (clínica) — todas as sub-workflows corretas, sem httpRequestTool de tag')
            ON CONFLICT (nicho) DO NOTHING
            """
        )


# --- clientes ---------------------------------------------------------

def criar_cliente_rascunho(
    cliente_nome: str, nicho: str, workflow_origem_id: str,
    briefing_texto: str = "", batch_id: str = "",
) -> int:
    with _conectar() as conn:
        linha = conn.execute(
            """
            INSERT INTO clientes (cliente_nome, nicho, workflow_origem_id, briefing_texto, batch_id, batch_status)
            VALUES (%s, %s, %s, %s, %s, %s)
            RETURNING id
            """,
            (cliente_nome, nicho, workflow_origem_id, briefing_texto or None, batch_id or None,
             "in_progress" if batch_id else None),
        ).fetchone()
    registrar_log(linha[0], "sistema", f"Cliente '{cliente_nome}' criado (rascunho, nicho {nicho}).")
    if batch_id:
        registrar_log(linha[0], "sistema", f"Briefing enviado pra Batch API (lote {batch_id}) — prompt e sugestão de Kommo saem em segundo plano.")
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


def atualizar_resultado_batch(cliente_id: int, status: str, prompt_sugerido: str = None, estrutura_kommo_sugerida: dict = None):
    with _conectar() as conn:
        conn.execute(
            """
            UPDATE clientes
            SET batch_status = %s, prompt_sugerido = %s, estrutura_kommo_sugerida = %s, atualizado_em = now()
            WHERE id = %s
            """,
            (status, prompt_sugerido, json.dumps(estrutura_kommo_sugerida) if estrutura_kommo_sugerida else None, cliente_id),
        )
    if status == "completed":
        registrar_log(cliente_id, "sistema", "Batch API concluído — prompt e sugestão de Kommo disponíveis pra revisão.")


def listar_clientes() -> list:
    with _conectar() as conn:
        cur = conn.execute("SELECT * FROM clientes ORDER BY criado_em DESC")
        return [_linha_para_cliente(cur, r) for r in cur.fetchall()]


def marcar_cliente_clonado(cliente_id: int, manifesto, resultado: dict):
    """Só chamar depois de um clone REAL bem-sucedido (dry_run=False)."""
    manifesto_seguro = {
        k: v for k, v in vars(manifesto).items()
        # segredos — ficam só no workflow/credencial do n8n, nunca duplicados aqui
        if k not in ("kommo_token", "openai_api_key")
    }
    with _conectar() as conn:
        conn.execute(
            """
            UPDATE clientes
            SET status = 'clonado', workflow_novo_id = %s, workflow_novo_url = %s,
                manifesto = %s, avisos = %s, atualizado_em = now()
            WHERE id = %s
            """,
            (
                resultado["workflow_id"],
                resultado["workflow_url"],
                json.dumps(manifesto_seguro),
                json.dumps(resultado.get("avisos", [])),
                cliente_id,
            ),
        )
    registrar_log(cliente_id, "sistema", f"Clonado com sucesso: {resultado['workflow_id']}.")


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


def obter_credencial_openai(cliente_id: int) -> str:
    """Id da credencial OpenAI criada pra este cliente, guardado no manifesto
    na hora da clonagem. Necessário pra conseguir removê-la junto com o agente."""
    with _conectar() as conn:
        r = conn.execute("SELECT manifesto FROM clientes WHERE id = %s", (cliente_id,)).fetchone()
    if not r or not r[0]:
        return ""
    return (r[0] or {}).get("openai_credential_id", "") or ""


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
    """{kommo_domain, access_token} do nosso banco, ou {} se não tiver.

    Quem chama cai pro node Database do clone quando vier vazio — é o que
    mantém os clientes antigos funcionando durante a migração.
    """
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
    """Apaga o cliente e tudo que pende dele no banco. Não toca no n8n nem na
    OpenAI — quem decide isso é quem chama (ver cloner.remover_agente e
    openai_admin.arquivar_projeto)."""
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
