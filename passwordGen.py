"""Uso:  python gerar_senha.py
Gera o valor para ADMIN_PASSWORD_HASH e um SESSION_SECRET novo."""
import getpass
import hashlib
import secrets

senha = getpass.getpass("Escolha a senha do painel: ")
if len(senha) < 10:
    raise SystemExit("Use pelo menos 10 caracteres.")
if senha != getpass.getpass("Repita a senha: "):
    raise SystemExit("As senhas nao conferem.")

salt = secrets.token_hex(16)
h = hashlib.pbkdf2_hmac("sha256", senha.encode(), bytes.fromhex(salt), 600_000).hex()
print("\nADMIN_PASSWORD_HASH=" + f"pbkdf2_sha256$600000${salt}${h}")
print("SESSION_SECRET=" + secrets.token_urlsafe(48))