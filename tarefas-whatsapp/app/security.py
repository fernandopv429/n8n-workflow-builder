"""Hash de senha com PBKDF2 (biblioteca padrão — sem depender de bcrypt, que às
vezes falha para compilar em ambientes restritos)."""
import hashlib
import hmac
import secrets

_ITERACOES = 390_000


def gerar_hash(senha: str) -> str:
    sal = secrets.token_hex(16)
    derivado = hashlib.pbkdf2_hmac("sha256", senha.encode(), bytes.fromhex(sal), _ITERACOES)
    return f"{sal}${derivado.hex()}"


def verificar_senha(senha: str, hash_guardado: str) -> bool:
    try:
        sal, derivado_hex = hash_guardado.split("$", 1)
    except ValueError:
        return False
    derivado_calculado = hashlib.pbkdf2_hmac(
        "sha256", senha.encode(), bytes.fromhex(sal), _ITERACOES
    )
    return hmac.compare_digest(derivado_calculado.hex(), derivado_hex)
