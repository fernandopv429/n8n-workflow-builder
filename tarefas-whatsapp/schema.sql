-- Esquema do sistema de tarefas/projetos via WhatsApp + Meet.
-- Aplicado automaticamente na subida do app (ver app/database.py:garantir_schema).

CREATE TABLE IF NOT EXISTS usuarios (
    id SERIAL PRIMARY KEY,
    email TEXT UNIQUE NOT NULL,
    senha_hash TEXT NOT NULL,
    nome TEXT NOT NULL,
    telefone_whatsapp TEXT UNIQUE,
    is_master BOOLEAN NOT NULL DEFAULT FALSE,
    criado_em TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS projetos (
    id SERIAL PRIMARY KEY,
    nome TEXT NOT NULL,
    descricao TEXT,
    dono_id INTEGER NOT NULL REFERENCES usuarios(id) ON DELETE CASCADE,
    origem TEXT NOT NULL DEFAULT 'manual', -- manual | whatsapp | reuniao
    criado_em TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS tarefas (
    id SERIAL PRIMARY KEY,
    projeto_id INTEGER REFERENCES projetos(id) ON DELETE SET NULL,
    titulo TEXT NOT NULL,
    descricao TEXT,
    prazo TIMESTAMPTZ,
    status TEXT NOT NULL DEFAULT 'pendente', -- pendente | concluida | cancelada
    dono_id INTEGER NOT NULL REFERENCES usuarios(id) ON DELETE CASCADE,
    lembrete_enviado BOOLEAN NOT NULL DEFAULT FALSE,
    origem TEXT NOT NULL DEFAULT 'manual', -- manual | whatsapp | reuniao
    criado_em TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS reunioes (
    id SERIAL PRIMARY KEY,
    pasta_drive_id TEXT UNIQUE,
    titulo TEXT,
    email_dono TEXT NOT NULL,
    usuario_id INTEGER REFERENCES usuarios(id),
    transcricao TEXT,
    projeto_criado_id INTEGER REFERENCES projetos(id),
    processado_em TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_tarefas_dono ON tarefas(dono_id);
CREATE INDEX IF NOT EXISTS idx_tarefas_prazo ON tarefas(prazo) WHERE status = 'pendente';
CREATE INDEX IF NOT EXISTS idx_projetos_dono ON projetos(dono_id);
