from cryptography.fernet import Fernet
from app.config import get_settings


def _get_fernet() -> Fernet:
    settings = get_settings()
    return Fernet(settings.encryption_key.encode())


def encrypt_token(token: str) -> str:
    """Encrypt a plaintext token for storage."""
    f = _get_fernet()
    return f.encrypt(token.encode()).decode()


def decrypt_token(encrypted_token: str) -> str:
    """Decrypt an encrypted token for use."""
    f = _get_fernet()
    return f.decrypt(encrypted_token.encode()).decode()
