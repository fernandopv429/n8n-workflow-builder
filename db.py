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
    _seed_templates_nicho()


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


def _linha_para_cliente(r) -> dict:
    return {
        "id": r[0],
        "cliente_nome": r[1],
        "nicho": r[2],
        "workflow_origem_id": r[3],
        "status": r[4],
        "workflow_novo_id": r[5],
        "workflow_novo_url": r[6],
        "criado_em": r[7].isoformat(),
        "batch_id": r[8],
        "batch_status": r[9],
        "prompt_sugerido": r[10],
        "estrutura_kommo_sugerida": r[11],
    }


def obter_cliente(cliente_id: int) -> dict | None:
    with _conectar() as conn:
        r = conn.execute(
            """
            SELECT id, cliente_nome, nicho, workflow_origem_id, status,
                   workflow_novo_id, workflow_novo_url, criado_em,
                   batch_id, batch_status, prompt_sugerido, estrutura_kommo_sugerida
            FROM clientes WHERE id = %s
            """,
            (cliente_id,),
        ).fetchone()
    return _linha_para_cliente(r) if r else None


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
        linhas = conn.execute(
            """
            SELECT id, cliente_nome, nicho, workflow_origem_id, status,
                   workflow_novo_id, workflow_novo_url, criado_em,
                   batch_id, batch_status, prompt_sugerido, estrutura_kommo_sugerida
            FROM clientes
            ORDER BY criado_em DESC
            """
        ).fetchall()
    return [_linha_para_cliente(r) for r in linhas]


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


if __name__ == "__main__":
    garantir_schema()
    print("schema ok")
    print(json.dumps(listar_clientes(), indent=2, ensure_ascii=False))
