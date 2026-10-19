"""Configuração compartilhada: lê variável de ambiente primeiro, .env local como
fallback de desenvolvimento (mesmo padrão do projeto n8n-workflow-builder)."""
import os
import pathlib

RAIZ = pathlib.Path(__file__).resolve().parent.parent
_cache = None


def _ler_arquivo_env() -> dict:
    valores = {}
    caminho = RAIZ / ".env"
    if not caminho.exists():
        return valores
    for linha in caminho.read_text().splitlines():
        linha = linha.strip()
        if not linha or linha.startswith("#") or "=" not in linha:
            continue
        chave, _, valor = linha.partition("=")
        valores[chave.strip()] = valor.strip()
    return valores


def carregar_env() -> dict:
    global _cache
    if _cache is None:
        _cache = {**_ler_arquivo_env(), **os.environ}
    return _cache


def obrigatorio(chave: str) -> str:
    valor = carregar_env().get(chave, "").strip()
    if not valor:
        raise RuntimeError(f"variável de ambiente obrigatória ausente: {chave}")
    return valor


def opcional(chave: str, padrao: str = "") -> str:
    return carregar_env().get(chave, padrao).strip()
