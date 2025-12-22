import logging
import google.generativeai as genai
from typing import AsyncGenerator, List, Dict, Any
from bot.ai.base import LLMProvider

logger = logging.getLogger(__name__)

class GoogleProvider(LLMProvider):
    def __init__(self, api_key: str):
        genai.configure(api_key=api_key)

    async def validate_key(self, api_key: str) -> bool:
        try:
            genai.configure(api_key=api_key)
            model = genai.GenerativeModel('gemini-1.5-flash')
            response = await model.generate_content_async("Test")
            return True if response else False
        except Exception as e:
            logger.error(f"Google Key Validation Error: {e}")
            return False

    def _map_messages(self, messages: List[Dict[str, str]]):
        """Конвертує історію для Gemini"""
        gemini_history = []
        system_instruction = ""

        for msg in messages:
            role = msg['role']
            content = msg.get('content', '')
            
            if role == 'system':
                system_instruction += content + "\n"
            elif role == 'user':
                gemini_history.append({'role': 'user', 'parts': [content]})
            elif role == 'assistant':
                gemini_history.append({'role': 'model', 'parts': [content]})
            # Tool calls ігноруємо в історії для Gemini, щоб не плутати його
        
        return system_instruction, gemini_history

    async def generate_stream(
        self, 
        messages: List[Dict[str, str]], 
        settings: Dict[str, Any]
    ) -> AsyncGenerator[str, None]:
        
        model_name = settings.get('model', 'gemini-1.5-flash')
        # Якщо обрана GPT модель, але ми тут - фолбек на Gemini
        if 'gpt' in model_name: 
            model_name = 'gemini-1.5-flash'
            
        temperature = settings.get('temperature', 0.7)
        current_lang = settings.get('language', 'uk')

        system_instruction_text, history = self._map_messages(messages)
        
        # --- МАГІЯ ДЛЯ GEMINI ---
        # Ми додаємо сувору інструкцію: якщо треба змінити мову, видай спец-код.
        # messages.py перехопить цей код і оновить базу даних.
        tech_instruction = (
            f"\n\nSYSTEM SETTINGS:\n"
            f"Current language: '{current_lang}'.\n"
            f"INSTRUCTION: If the user explicitly asks to change/switch the language (e.g. 'switch to english', 'перейди на русский'), "
            f"you MUST output the command `__SET_LANGUAGE:code__` (where code is 'uk', 'en', 'ru') "
            f"at the very beginning of your response. Then answer in that language."
        )
        
        full_system_instruction = (system_instruction_text or "") + tech_instruction

        # Підготовка промпта
        if history and history[-1]['role'] == 'user':
            prompt = history.pop()
            chat_history = history
        else:
            prompt = {'role': 'user', 'parts': ['Hello']}
            chat_history = []

        try:
            # Ініціалізація моделі
            model = genai.GenerativeModel(
                model_name=model_name,
                system_instruction=full_system_instruction
            )
            
            chat = model.start_chat(history=chat_history)
            
            response = await chat.send_message_async(
                prompt['parts'][0],
                generation_config=genai.types.GenerationConfig(
                    temperature=temperature
                ),
                stream=True
            )

            async for chunk in response:
                if chunk.text:
                    yield chunk.text

        except Exception as e:
            logger.error(f"Gemini Error: {e}")
            yield f"⚠️ Gemini API Error: {str(e)}"

    async def transcribe(self, audio_path: str, language: str = None) -> str:
        # Для транскрибації ми використовуємо Whisper (OpenAI) через helpers.py,
        # тому цей метод тут заглушка, або можна реалізувати через Gemini File API (складніше).
        return "Gemini transcription not implemented directly. Use OpenAI Whisper."

    async def analyze_image(
        self, 
        image_path: str, 
        prompt: str, 
        messages: List[Dict[str, str]] = None
    ) -> AsyncGenerator[str, None]:
        
        try:
            import PIL.Image
            img = PIL.Image.open(image_path)
            
            # Для Vision використовуємо Flash (він швидкий і добре бачить)
            model = genai.GenerativeModel('gemini-1.5-flash')
            
            response = await model.generate_content_async(
                [prompt, img], 
                stream=True
            )

            async for chunk in response:
                if chunk.text:
                    yield chunk.text

        except Exception as e:
            logger.error(f"Gemini Vision Error: {e}")
            yield f"⚠️ Error: {str(e)}"