import logging
import datetime
import zoneinfo
from typing import AsyncGenerator, List, Dict, Any
import google.generativeai as genai
from google.ai.generativelanguage import FunctionDeclaration, Tool, Schema, Type
from bot.ai.base import LLMProvider
from config import DEFAULT_SETTINGS, BOT_TIMEZONE
from bot.utils.search import perform_search
from bot.utils.scheduler import scheduler_service

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
            if role == 'system':
                system_instruction += content + "\n"
            elif role == 'user':
                gemini_history.append({'role': 'user', 'parts': [content]})
            elif role == 'assistant':
                gemini_history.append({'role': 'model', 'parts': [content]})
        return system_instruction, gemini_history

    def _get_tools_proto(self, allow_search: bool):
        declarations = []
        
        reminder_func = FunctionDeclaration(
            name="schedule_reminder",
            description="Schedule a reminder.",
            parameters=Schema(
                type=Type.OBJECT,
                properties={
                    "iso_time_local": Schema(type=Type.STRING, description="Exact ISO-8601 datetime in USER'S LOCAL TIME"),
                    "text": Schema(type=Type.STRING, description="Reminder text")
                },
                required=["iso_time_local", "text"]
            )
        )
        declarations.append(reminder_func)

        if allow_search:
            search_func = FunctionDeclaration(
                name="web_search",
                description="Find real-time info.",
                parameters=Schema(type=Type.OBJECT, properties={"query": Schema(type=Type.STRING)}, required=["query"])
            )
            declarations.append(search_func)

        lang_func = FunctionDeclaration(
            name="set_language",
            description="Switch bot language.",
            parameters=Schema(type=Type.OBJECT, properties={"lang_code": Schema(type=Type.STRING)}, required=["lang_code"])
        )
        declarations.append(lang_func)

        return Tool(function_declarations=declarations)

    async def generate_stream(self, messages: List[Dict[str, str]], settings: Dict[str, Any]) -> AsyncGenerator[str, None]:
        model_name = settings.get('model', self.model_name)
        if 'gpt' in model_name: model_name = 'gemini-1.5-flash'
        
        temperature = settings.get('temperature', 0.7)
        current_lang = settings.get('language', 'uk')
        allow_search = settings.get('allow_search', True)
        
        system_instruction_text, history = self._map_messages(messages)
        
        user_tz_name = settings.get('timezone', BOT_TIMEZONE)
        try: tz = zoneinfo.ZoneInfo(user_tz_name)
        except: tz = datetime.timezone.utc
            
        now_local = datetime.datetime.now(tz)
        # ДОДАНО (%A) - День тижня словами
        current_time_str = f"{now_local.strftime('%Y-%m-%d (%A) %H:%M:%S')}"

        tech_instruction = (
            f"\n\n[SYSTEM INFO]\n"
            f"- Current Local Time: {current_time_str} (Timezone: {user_tz_name})\n"
            f"- Language: '{current_lang}'\n"
            f"INSTRUCTION: When scheduling, provide 'iso_time_local' based on current local time. "
            f"Use the provided Day of Week to correctly calculate future dates (e.g. if today is Friday, Saturday is tomorrow).\n"
            f"AMBIGUITY RULE: If the user mentions an event time (e.g. 'Dentist on Saturday at 15:00') "
            f"but DOES NOT specify when to receive the notification (e.g. 'in 1 hour', 'tomorrow'), "
            f"DO NOT call 'schedule_reminder' yet. Instead, ASK: 'When would you like to be reminded? (e.g., 1 hour before)'."
        )
        full_sys_inst = (system_instruction_text or "") + tech_instruction

        prompt_content = "Hello"
        chat_history = []
        if history and history[-1]['role'] == 'user':
            last_msg = history.pop()
            prompt_content = last_msg['parts'][0]
            chat_history = history
        elif history:
            chat_history = history

        tools_obj = self._get_tools_proto(allow_search)
        model = genai.GenerativeModel(model_name=model_name, system_instruction=full_sys_inst, tools=[tools_obj])
        chat = model.start_chat(history=chat_history)
        
        try:
            response_stream = await chat.send_message_async(
                prompt_content,
                generation_config=genai.types.GenerationConfig(temperature=temperature),
                stream=True
            )

            function_call_found = False
            function_call_part = None
            
            async for chunk in response_stream:
                if chunk.candidates and chunk.candidates[0].content.parts:
                    part = chunk.candidates[0].content.parts[0]
                    if part.function_call:
                        function_call_found = True
                        function_call_part = part.function_call
                        break 
                if chunk.text: yield chunk.text

            if function_call_found:
                try: await response_stream.resolve()
                except Exception as e: logger.warning(f"Stream resolve warning: {e}")

            stop_generating = False 

            if function_call_found and function_call_part:
                fn_name = function_call_part.name
                fn_args = {}
                if function_call_part.args:
                    for key, value in function_call_part.args.items(): fn_args[key] = value
                
                logger.info(f"Gemini Function Call: {fn_name} with {fn_args}")
                api_response = {}
                
                if fn_name == "web_search":
                    yield "\n\n🔎 <i>Шукаю в інтернеті...</i>\n"
                    res = await perform_search(fn_args.get("query", ""))
                    api_response = {"result": res}
                    
                elif fn_name == "set_language":
                    lang = fn_args.get("lang_code", "uk")
                    yield f"__SET_LANGUAGE:{lang}__"
                    api_response = {"status": f"Language switched to {lang}"}
                    
                elif fn_name == "schedule_reminder":
                    try:
                        iso_time = fn_args.get("iso_time_local")
                        text = fn_args.get("text")
                        user_id = settings.get('user_id')
                        chat_id = settings.get('chat_id')
                        
                        if user_id and chat_id and iso_time:
                            dt_local = datetime.datetime.fromisoformat(iso_time)
                            if dt_local.tzinfo is None: dt_local = dt_local.replace(tzinfo=tz)
                            dt_utc = dt_local.astimezone(datetime.timezone.utc)
                            
                            rid = await scheduler_service.add_reminder(user_id, chat_id, text, dt_utc)
                            display_time = dt_local.strftime("%H:%M")
                            
                            yield f"\n✅ <b>Створено нагадування</b> на {display_time}\n📝 <i>{text}</i>"
                            
                            api_response = {"status": "success", "info": "Notification displayed."}
                            stop_generating = True
                        else:
                            api_response = {"status": "error", "message": "Missing IDs"}
                    except Exception as e:
                        logger.error(f"Gemini Reminder Error: {e}")
                        api_response = {"status": "error", "message": str(e)}

                final_response = await chat.send_message_async(
                    genai.protos.Content(parts=[genai.protos.Part(function_response=genai.protos.FunctionResponse(name=fn_name, response=api_response))]),
                    stream=True
                )
                
                async for chunk in final_response:
                    if chunk.text:
                        if not stop_generating:
                            yield chunk.text

        except Exception as e:
            logger.error(f"Gemini Chat Error: {e}")
            yield f"⚠️ Gemini Error: {str(e)}"

    async def transcribe(self, audio_path: str, language: str = None) -> str:
        try:
            with open(audio_path, "rb") as f:
                audio_data = f.read()
            mime_type = "audio/ogg" if audio_path.endswith(".ogg") else "audio/mp3"
            model = genai.GenerativeModel(self.model_name)
            lang_prompt = f" The language of the audio is likely {language}." if language else ""
            prompt = DEFAULT_SETTINGS['transcription_prompt'] + lang_prompt
            response = await model.generate_content_async([{'mime_type': mime_type, 'data': audio_data}, prompt])
            return response.text.strip()
        except Exception as e:
            logger.error(f"Gemini Transcribe Error: {e}")
            return f"Error: {str(e)}"

    async def analyze_image(self, image_path: str, prompt: str, messages: List[Dict[str, str]] = None) -> AsyncGenerator[str, None]:
        try:
            import PIL.Image
            img = PIL.Image.open(image_path)
            model = genai.GenerativeModel(self.model_name)
            response = await model.generate_content_async([prompt, img], stream=True)
            async for chunk in response:
                if chunk.text: yield chunk.text
        except Exception as e:
            logger.error(f"Gemini Vision Error: {e}")
            yield f"⚠️ Error: {str(e)}"