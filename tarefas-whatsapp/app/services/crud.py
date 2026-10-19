"""Acesso ao banco. Toda consulta que lista/edita projeto ou tarefa recebe o
usuário logado e respeita a regra: master vê tudo, os demais só o que é deles.
"""
from datetime import datetime, timezone

from sqlalchemy import text

from .. import security
from ..database import sessao


# --- usuários -----------------------------------------------------------

def criar_usuario(email: str, senha: str, nome: str, telefone_whatsapp: str | None = None,
                   is_master: bool = False) -> dict:
    with sessao() as db:
        linha = db.execute(
            text(
                """
                INSERT INTO usuarios (email, senha_hash, nome, telefone_whatsapp, is_master)
                VALUES (:email, :senha_hash, :nome, :telefone, :is_master)
                RETURNING id, email, nome, telefone_whatsapp, is_master
                """
            ),
            {
                "email": email.strip().lower(),
                "senha_hash": security.gerar_hash(senha),
                "nome": nome.strip(),
                "telefone": (telefone_whatsapp or "").strip() or None,
                "is_master": is_master,
            },
        ).mappings().first()
        return dict(linha)


def buscar_usuario_por_email(email: str) -> dict | None:
    with sessao() as db:
        linha = db.execute(
            text("SELECT * FROM usuarios WHERE email = :email"),
            {"email": email.strip().lower()},
        ).mappings().first()
        return dict(linha) if linha else None


def buscar_usuario_por_id(usuario_id: int) -> dict | None:
    with sessao() as db:
        linha = db.execute(
            text("SELECT * FROM usuarios WHERE id = :id"), {"id": usuario_id}
        ).mappings().first()
        return dict(linha) if linha else None


def buscar_usuario_por_telefone(telefone: str) -> dict | None:
    with sessao() as db:
        linha = db.execute(
            text("SELECT * FROM usuarios WHERE telefone_whatsapp = :tel"),
            {"tel": telefone.strip()},
        ).mappings().first()
        return dict(linha) if linha else None


def autenticar(email: str, senha: str) -> dict | None:
    usuario = buscar_usuario_por_email(email)
    if not usuario or not security.verificar_senha(senha, usuario["senha_hash"]):
        return None
    return usuario


def existe_algum_usuario() -> bool:
    with sessao() as db:
        linha = db.execute(text("SELECT 1 FROM usuarios LIMIT 1")).first()
        return linha is not None


# --- projetos -------------------------------------------------------------

def criar_projeto(usuario: dict, nome: str, descricao: str = "", origem: str = "manual") -> dict:
    with sessao() as db:
        linha = db.execute(
            text(
                """
                INSERT INTO projetos (nome, descricao, dono_id, origem)
                VALUES (:nome, :descricao, :dono_id, :origem)
                RETURNING id, nome, descricao, dono_id, origem, criado_em
                """
            ),
            {"nome": nome.strip(), "descricao": descricao or "", "dono_id": usuario["id"], "origem": origem},
        ).mappings().first()
        return dict(linha)


def listar_projetos(usuario: dict) -> list[dict]:
    with sessao() as db:
        if usuario["is_master"]:
            linhas = db.execute(
                text(
                    """
                    SELECT p.*, u.nome AS dono_nome
                    FROM projetos p JOIN usuarios u ON u.id = p.dono_id
                    ORDER BY p.criado_em DESC
                    """
                )
            ).mappings().all()
        else:
            linhas = db.execute(
                text(
                    """
                    SELECT p.*, u.nome AS dono_nome
                    FROM projetos p JOIN usuarios u ON u.id = p.dono_id
                    WHERE p.dono_id = :uid ORDER BY p.criado_em DESC
                    """
                ),
                {"uid": usuario["id"]},
            ).mappings().all()
        return [dict(r) for r in linhas]


def buscar_projeto(usuario: dict, projeto_id: int) -> dict | None:
    with sessao() as db:
        condicao = "" if usuario["is_master"] else "AND dono_id = :uid"
        linha = db.execute(
            text(f"SELECT * FROM projetos WHERE id = :id {condicao}"),
            {"id": projeto_id, "uid": usuario["id"]},
        ).mappings().first()
        return dict(linha) if linha else None


# --- tarefas --------------------------------------------------------------

def criar_tarefa(usuario: dict, titulo: str, descricao: str = "", prazo: datetime | None = None,
                  projeto_id: int | None = None, origem: str = "manual",
                  dono_id: int | None = None) -> dict:
    """dono_id permite ao master criar tarefa em nome de outro usuário (ex: a
    partir de uma reunião identificada por email de outra pessoa)."""
    with sessao() as db:
        linha = db.execute(
            text(
                """
                INSERT INTO tarefas (projeto_id, titulo, descricao, prazo, dono_id, origem)
                VALUES (:projeto_id, :titulo, :descricao, :prazo, :dono_id, :origem)
                RETURNING *
                """
            ),
            {
                "projeto_id": projeto_id,
                "titulo": titulo.strip(),
                "descricao": descricao or "",
                "prazo": prazo,
                "dono_id": dono_id or usuario["id"],
                "origem": origem,
            },
        ).mappings().first()
        return dict(linha)


def listar_tarefas(usuario: dict, projeto_id: int | None = None, status: str | None = None) -> list[dict]:
    with sessao() as db:
        condicoes = [] if usuario["is_master"] else ["t.dono_id = :uid"]
        params = {"uid": usuario["id"]}
        if projeto_id is not None:
            condicoes.append("t.projeto_id = :pid")
            params["pid"] = projeto_id
        if status is not None:
            condicoes.append("t.status = :status")
            params["status"] = status
        where = f"WHERE {' AND '.join(condicoes)}" if condicoes else ""
        linhas = db.execute(
            text(
                f"""
                SELECT t.*, u.nome AS dono_nome, p.nome AS projeto_nome
                FROM tarefas t
                JOIN usuarios u ON u.id = t.dono_id
                LEFT JOIN projetos p ON p.id = t.projeto_id
                {where}
                ORDER BY t.prazo NULLS LAST, t.criado_em DESC
                """
            ),
            params,
        ).mappings().all()
        return [dict(r) for r in linhas]


def concluir_tarefa(usuario: dict, tarefa_id: int) -> bool:
    with sessao() as db:
        condicao = "" if usuario["is_master"] else "AND dono_id = :uid"
        resultado = db.execute(
            text(f"UPDATE tarefas SET status = 'concluida' WHERE id = :id {condicao}"),
            {"id": tarefa_id, "uid": usuario["id"]},
        )
        return resultado.rowcount > 0


def tarefas_para_lembrar(minutos_antes: int = 1440) -> list[dict]:
    """Tarefas pendentes cujo prazo cai dentro da janela de lembrete e que
    ainda não foram avisadas."""
    with sessao() as db:
        linhas = db.execute(
            text(
                """
                SELECT t.*, u.telefone_whatsapp, u.nome AS dono_nome
                FROM tarefas t JOIN usuarios u ON u.id = t.dono_id
                WHERE t.status = 'pendente'
                  AND t.lembrete_enviado = FALSE
                  AND t.prazo IS NOT NULL
                  AND u.telefone_whatsapp IS NOT NULL
                  AND t.prazo <= now() + (:minutos || ' minutes')::interval
                  AND t.prazo >= now()
                """
            ),
            {"minutos": minutos_antes},
        ).mappings().all()
        return [dict(r) for r in linhas]


def marcar_lembrete_enviado(tarefa_id: int):
    with sessao() as db:
        db.execute(text("UPDATE tarefas SET lembrete_enviado = TRUE WHERE id = :id"), {"id": tarefa_id})


# --- reuniões (Meet) --------------------------------------------------------

def registrar_reuniao(pasta_drive_id: str, titulo: str, email_dono: str,
                       usuario_id: int | None, transcricao: str) -> dict:
    with sessao() as db:
        linha = db.execute(
            text(
                """
                INSERT INTO reunioes (pasta_drive_id, titulo, email_dono, usuario_id, transcricao)
                VALUES (:pasta, :titulo, :email, :uid, :transcricao)
                ON CONFLICT (pasta_drive_id) DO UPDATE SET transcricao = EXCLUDED.transcricao
                RETURNING *
                """
            ),
            {
                "pasta": pasta_drive_id,
                "titulo": titulo,
                "email": email_dono.strip().lower(),
                "uid": usuario_id,
                "transcricao": transcricao,
            },
        ).mappings().first()
        return dict(linha)


def vincular_projeto_criado(reuniao_id: int, projeto_id: int):
    with sessao() as db:
        db.execute(
            text("UPDATE reunioes SET projeto_criado_id = :pid WHERE id = :id"),
            {"pid": projeto_id, "id": reuniao_id},
        )
