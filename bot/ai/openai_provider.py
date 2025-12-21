import base64
import logging
from typing import AsyncGenerator, List, Dict, Any
from openai import AsyncOpenAI, APIError
from bot.ai.base import LLMProvider

logger = logging.getLogger(__name__)

class OpenAIProvider(LLMProvider):
    def __init__(self, api_key: str):
        self.client = AsyncOpenAI(api_key=api_key)

    async def validate_key(self, api_key: str) -> bool:
        """Робить тестовий міні-запит для перевірки ключа"""
        temp_client = AsyncOpenAI(api_key=api_key)
        try:
            await temp_client.models.list()
            return True
        except Exception as e:
            logger.warning(f"Validation failed for OpenAI key: {e}")
            return False
        finally:
            await temp_client.close()

    async def generate_stream(
        self, 
        messages: List[Dict[str, str]], 
        settings: Dict[str, Any]
    ) -> AsyncGenerator[str, None]:
        try:
            model = settings.get('model', 'gpt-4o')
            temperature = settings.get('temperature', 0.7)
            
            stream = await self.client.chat.completions.create(
                model=model,
                messages=messages,
                temperature=temperature,
                stream=True
            )

            async for chunk in stream:
                content = chunk.choices[0].delta.content
                if content:
                    yield content

        except APIError as e:
            logger.error(f"OpenAI API Error: {e}")
            yield f"⚠️ Помилка API OpenAI: {str(e)}"
        except Exception as e:
            logger.error(f"Unexpected error in generate_stream: {e}")
            yield "⚠️ Виникла непередбачувана помилка при генерації відповіді."

    async def transcribe(self, audio_path: str, language: str = None) -> str:
        try:
            with open(audio_path, "rb") as audio_file:
                transcript = await self.client.audio.transcriptions.create(
                    model="whisper-1",
                    file=audio_file,
                    language=language
                )
            return transcript.text
        except Exception as e:
            logger.error(f"Transcription error: {e}")
            return f"Помилка транскрибації: {str(e)}"

    async def analyze_image(
        self, 
        image_path: str, 
        prompt: str,
        messages: List[Dict[str, str]] = None
    ) -> AsyncGenerator[str, None]:
        
        try:
            # Кодуємо зображення в base64
            with open(image_path, "rb") as image_file:
                base64_image = base64.b64encode(image_file.read()).decode('utf-8')

            # Формуємо повідомлення
            content = [
                {"type": "text", "text": prompt},
                {
                    "type": "image_url",
                    "image_url": {
                        "url": f"data:image/jpeg;base64,{base64_image}"
                    }
                }
            ]

            request_messages = []
            # Додаємо контекст якщо є
            if messages:
                request_messages.extend(messages)
            
            # Додаємо саме повідомлення з картинкою
            request_messages.append({"role": "user", "content": content})

            stream = await self.client.chat.completions.create(
                model="gpt-4o", # Використовуємо Vision модель
                messages=request_messages,
                max_tokens=1000,
                stream=True
            )

            async for chunk in stream:
                content = chunk.choices[0].delta.content
                if content:
                    yield content

        except Exception as e:
            logger.error(f"Image analysis error: {e}")
            yield f"⚠️ Помилка аналізу зображення: {str(e)}"