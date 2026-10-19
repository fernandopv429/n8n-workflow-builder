from contextlib import contextmanager

from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from . import config

_engine = None
_SessionLocal = None


def _url_para_psycopg3(url: str) -> str:
    """SQLAlchemy precisa do driver explícito na URL; aceita postgres:// e
    postgresql:// (formatos comuns de provedor) e normaliza pro psycopg3."""
    if url.startswith("postgres://"):
        url = "postgresql://" + url[len("postgres://"):]
    if url.startswith("postgresql://") and "+psycopg" not in url:
        url = "postgresql+psycopg://" + url[len("postgresql://"):]
    return url


def _get_engine():
    global _engine, _SessionLocal
    if _engine is None:
        url = _url_para_psycopg3(config.obrigatorio("DATABASE_URL"))
        _engine = create_engine(url, pool_pre_ping=True)
        _SessionLocal = sessionmaker(bind=_engine, autoflush=False, autocommit=False)
    return _engine


@contextmanager
def sessao():
    _get_engine()
    db = _SessionLocal()
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def garantir_schema():
    """Cria as tabelas na primeira subida, lendo schema.sql. Idempotente
    (CREATE TABLE IF NOT EXISTS), então pode rodar em todo deploy."""
    engine = _get_engine()
    caminho = config.RAIZ / "schema.sql"
    sql = caminho.read_text()
    with engine.begin() as conn:
        for comando in sql.split(";"):
            comando = comando.strip()
            if comando:
                conn.execute(text(comando))
