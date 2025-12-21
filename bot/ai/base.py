from abc import ABC, abstractmethod
from typing import AsyncGenerator, Optional, List, Dict, Any

class LLMProvider(ABC):
    """
    Абстрактний базовий клас для всіх AI провайдерів.
    Кожен провайдер (OpenAI, Claude, etc.) має реалізувати ці методи.
    """

    @abstractmethod
    async def generate_stream(
        self, 
        messages: List[Dict[str, str]], 
        settings: Dict[str, Any]
    ) -> AsyncGenerator[str, None]:
        """
        Генерує текстову відповідь у потоковому режимі.
        :param messages: Список повідомлень [{'role': 'user', 'content': '...'}, ...]
        :param settings: Налаштування (temperature, max_tokens, etc.)
        """
        pass

    @abstractmethod
    async def transcribe(
        self, 
        audio_path: str, 
        language: str = None
    ) -> str:
        """
        Транскрибує аудіо/відео файл в текст.
        """
        pass

    @abstractmethod
    async def analyze_image(
        self, 
        image_path: str, 
        prompt: str,
        messages: List[Dict[str, str]] = None
    ) -> AsyncGenerator[str, None]:
        """
        Аналізує зображення разом з текстовим промптом.
        """
        pass

    @abstractmethod
    async def validate_key(self, api_key: str) -> bool:
        """
        Перевіряє, чи валідний наданий API ключ.
        """
        pass