"""Configuração compartilhada por todos os módulos.

Ordem de precedência: variável de ambiente primeiro (é assim que o Coolify
injeta segredo), arquivo `.env` local como fallback de desenvolvimento.

Antes cada módulo carregava a própria cópia do `.env` — funcionava na máquina
do Fernando e falharia calado em container, onde esse arquivo não existe e
tudo vem do ambiente.
"""
import os
import pathlib

RAIZ = pathlib.Path(__file__).resolve().parent

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
    """Falha alto e claro no lugar certo — melhor que um KeyError solto no meio
    de uma chamada de API, ou pior, uma string vazia mandada pro n8n/OpenAI."""
    valor = carregar_env().get(chave, "").strip()
    if not valor:
        raise RuntimeError(f"variável de ambiente obrigatória ausente: {chave}")
    return valor
