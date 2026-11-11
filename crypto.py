import os
from cryptography.fernet import Fernet

_f = Fernet(os.environ["TOKEN_ENCRYPTION_KEY"].encode())


def encrypt(texto: str) -> str:
    return _f.encrypt(texto.encode()).decode()


def decrypt(texto: str) -> str:
    return _f.decrypt(texto.encode()).decode()
