import logging
import google.generativeai as genai
from typing import AsyncGenerator, List, Dict, Any
from bot.ai.base import LLMProvider
from config import DEFAULT_SETTINGS

logger = logging.getLogger(__name__)

class GoogleProvider(LLMProvider):
    def __init__(self, api_key: str, model_name: str = 'gemini-1.5-flash'):
        genai.configure(api_key=api_key)
        self.model_name = model_name

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
        gemini_history = []
        system_instruction = ""
        for msg in messages:
            role = msg['role']
            content = msg.get('content', '')
            if role == 'system': system_instruction += content + "\n"
            elif role == 'user': gemini_history.append({'role': 'user', 'parts': [content]})
            elif role == 'assistant': gemini_history.append({'role': 'model', 'parts': [content]})
        return system_instruction, gemini_history

    async def generate_stream(self, messages: List[Dict[str, str]], settings: Dict[str, Any]) -> AsyncGenerator[str, None]:
        # Використовуємо self.model_name, якщо в settings не прийшло щось інше
        model_name = settings.get('model', self.model_name)
        if 'gpt' in model_name: model_name = 'gemini-1.5-flash'
        
        temperature = settings.get('temperature', 0.7)
        current_lang = settings.get('language', 'uk')
        system_instruction_text, history = self._map_messages(messages)
        
        tech_instruction = (
            f"\n\nSYSTEM SETTINGS:\n"
            f"Current language: '{current_lang}'.\n"
            f"INSTRUCTION: If the user explicitly asks to change/switch the language, "
            f"output `__SET_LANGUAGE:code__` first."
        )
        full_sys_inst = (system_instruction_text or "") + tech_instruction

        if history and history[-1]['role'] == 'user':
            prompt = history.pop()
            chat_history = history
        else:
            prompt = {'role': 'user', 'parts': ['Hello']}
            chat_history = []

        try:
            model = genai.GenerativeModel(model_name=model_name, system_instruction=full_sys_inst)
            chat = model.start_chat(history=chat_history)
            response = await chat.send_message_async(
                prompt['parts'][0],
                generation_config=genai.types.GenerationConfig(temperature=temperature),
                stream=True
            )
            async for chunk in response:
                if chunk.text: yield chunk.text
        except Exception as e:
            logger.error(f"Gemini Error: {e}")
            yield f"⚠️ Gemini API Error: {str(e)}"

    async def transcribe(self, audio_path: str, language: str = None) -> str:
        """Мультимодальна транскрибація через Gemini"""
        try:
            # Читаємо файл
            with open(audio_path, "rb") as f:
                audio_data = f.read()
            
            # Визначаємо MIME (для телеграму це зазвичай ogg/opus)
            mime_type = "audio/ogg" if audio_path.endswith(".ogg") else "audio/mp3"
            
            model = genai.GenerativeModel(self.model_name)
            
            # Формуємо промпт
            # Додаємо вказівку про мову, якщо вона задана
            lang_prompt = f" The language of the audio is likely {language}." if language else ""
            prompt = DEFAULT_SETTINGS['transcription_prompt'] + lang_prompt
            
            # Генеруємо контент (аудіо + текст промпту)
            response = await model.generate_content_async(
                [
                    {'mime_type': mime_type, 'data': audio_data},
                    prompt
                ]
            )
            
            return response.text.strip()
            
        except Exception as e:
            logger.error(f"Gemini Transcribe Error: {e}")
            return f"Error: {str(e)}"

    async def analyze_image(self, image_path: str, prompt: str, messages: List[Dict[str, str]] = None) -> AsyncGenerator[str, None]:
        try:
            import PIL.Image
            img = PIL.Image.open(image_path)
            model = genai.GenerativeModel(self.model_name) # Використовуємо поточну модель
            response = await model.generate_content_async([prompt, img], stream=True)
            async for chunk in response:
                if chunk.text: yield chunk.text
        except Exception as e:
            logger.error(f"Gemini Vision Error: {e}")
            yield f"⚠️ Error: {str(e)}"