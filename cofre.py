"""Cifra os segredos de cliente que precisam morar no nosso Postgres.

Até 02/10/2026 a gente não guardava segredo de cliente nenhum: o token do Kommo
ficava no node `Database` do workflow clonado e a chave da OpenAI ia direto pra
credencial do n8n. Isso dependia de existir um clone por cliente — e o desenho
novo (ARQUITETURA-AGENTE.md) não tem clone. O worker precisa da chave DO
CLIENTE pra que o consumo apareça no projeto dele, e o n8n não devolve o valor
de uma credencial pela API pública (só id e nome), então não dá pra buscar lá.

Guardar é inevitável. Guardar em texto puro, não: o `.env` e o banco têm
públicos diferentes, e um dump de banco não pode virar a chave da OpenAI de
todos os clientes.

`COFRE_CHAVE` é uma chave Fernet (urlsafe base64, 32 bytes). Gerar uma com:

    python3 -c "import sys;sys.path.insert(0,'.pylibs');\\
from cryptography.fernet import Fernet;print(Fernet.generate_key().decode())"

Perder essa chave torna os segredos cifrados irrecuperáveis — o que não é
catástrofe aqui, porque dá pra criar outra chave na OpenAI e outro token no
Kommo. Mas tem que ser guardada junto das outras credenciais.
"""
import os
import sys

RAIZ = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(RAIZ, ".pylibs"))

from cryptography.fernet import Fernet, InvalidToken  # noqa: E402

import config  # noqa: E402

PREFIXO = "fernet:"


class CofreIndisponivel(RuntimeError):
    """Sem COFRE_CHAVE não dá pra cifrar nem decifrar. Falha alto em vez de
    gravar em texto puro silenciosamente."""


def disponivel() -> bool:
    return bool(config.carregar_env().get("COFRE_CHAVE", "").strip())


def _motor() -> Fernet:
    chave = config.carregar_env().get("COFRE_CHAVE", "").strip()
    if not chave:
        raise CofreIndisponivel(
            "COFRE_CHAVE não configurada — sem ela não guardo segredo de cliente. "
            "Gere uma com Fernet.generate_key() e cadastre no .env/Coolify."
        )
    try:
        return Fernet(chave.encode())
    except (ValueError, TypeError) as e:
        raise CofreIndisponivel(f"COFRE_CHAVE inválida (precisa ser uma chave Fernet): {e}") from None


def cifrar(texto: str) -> str:
    if not texto:
        return ""
    return PREFIXO + _motor().encrypt(texto.encode()).decode()


def decifrar(guardado: str) -> str:
    """Aceita valor sem prefixo e devolve como está: se alguma linha antiga
    tiver sido gravada em claro, é melhor o sistema funcionar e a gente migrar
    do que quebrar o atendimento de um cliente."""
    if not guardado:
        return ""
    if not guardado.startswith(PREFIXO):
        return guardado
    try:
        return _motor().decrypt(guardado[len(PREFIXO):].encode()).decode()
    except InvalidToken:
        raise CofreIndisponivel(
            "não consegui decifrar: a COFRE_CHAVE atual não é a que cifrou este valor."
        ) from None
