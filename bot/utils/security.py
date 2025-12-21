from cryptography.fernet import Fernet
from config import ENCRYPTION_KEY
import logging

logger = logging.getLogger(__name__)

class KeyManager:
    def __init__(self):
        if not ENCRYPTION_KEY:
            raise ValueError("ENCRYPTION_KEY не знайдено в налаштуваннях! Ключі користувачів не можуть бути захищені.")
        self.cipher_suite = Fernet(ENCRYPTION_KEY.encode() if isinstance(ENCRYPTION_KEY, str) else ENCRYPTION_KEY)

    def encrypt(self, plain_text_key: str) -> str:
        """Шифрує API ключ перед записом в БД"""
        if not plain_text_key:
            return ""
        try:
            encrypted_bytes = self.cipher_suite.encrypt(plain_text_key.encode())
            return encrypted_bytes.decode()
        except Exception as e:
            logger.error(f"Помилка шифрування ключа: {e}")
            raise

    def decrypt(self, encrypted_key: str) -> str:
        """Розшифровує API ключ для використання"""
        if not encrypted_key:
            return ""
        try:
            decrypted_bytes = self.cipher_suite.decrypt(encrypted_key.encode())
            return decrypted_bytes.decode()
        except Exception as e:
            logger.error(f"Помилка дешифрування ключа: {e}")
            return ""

# Створюємо глобальний екземпляр
key_manager = KeyManager()