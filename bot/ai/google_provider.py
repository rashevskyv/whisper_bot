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
from bot.utils.date_helper import calculate_future_date

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
        
        # 1. Date Calculator
        date_func = FunctionDeclaration(
            name="calculate_date",
            description="Calculate exact date and time from natural language. ALWAYS use this before scheduling.",
            parameters=Schema(
                type=Type.OBJECT,
                properties={
                    "query": Schema(type=Type.STRING, description="Date/time text (e.g. 'next Saturday at 15:00')")
                },
                required=["query"]
            )
        )
        declarations.append(date_func)

        # 2. Scheduler Tool
        reminder_func = FunctionDeclaration(
            name="schedule_reminder",
            description="Schedule a new reminder using ISO date from calculate_date.",
            parameters=Schema(
                type=Type.OBJECT,
                properties={
                    "iso_time_utc": Schema(type=Type.STRING, description="Exact ISO-8601 datetime in UTC"),
                    "text": Schema(type=Type.STRING, description="Reminder text")
                },
                required=["iso_time_utc", "text"]
            )
        )
        declarations.append(reminder_func)

        # 3. Delete Tool
        del_func = FunctionDeclaration(
            name="delete_reminder",
            description="Delete an existing reminder by ID.",
            parameters=Schema(
                type=Type.OBJECT,
                properties={
                    "reminder_id": Schema(type=Type.INTEGER, description="The ID of the reminder to delete")
                },
                required=["reminder_id"]
            )
        )
        declarations.append(del_func)

        if allow_search:
            search_func = FunctionDeclaration(
                name="web_search",
                description="Find real-time info on the internet.",
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
        
        # Smart upgrade for reminders
        if settings.get('allow_search', True) and 'flash' in model_name.lower() and \
           any("нагадай" in m.get('content', '').lower() for m in messages[-2:]):
             logger.info("⚡ Upgrading to Gemini-3-Pro for complex reminder logic")
             model_name = 'gemini-3-pro-preview'
        
        temperature = settings.get('temperature', 0.7)
        current_lang = settings.get('language', 'uk')
        allow_search = settings.get('allow_search', True)
        user_tz_name = settings.get('timezone', BOT_TIMEZONE)

        system_instruction_text, history = self._map_messages(messages)
        try: tz = zoneinfo.ZoneInfo(user_tz_name)
        except: tz = datetime.timezone.utc
            
        now_local = datetime.datetime.now(tz)
        current_time_str = f"{now_local.strftime('%Y-%m-%d (%A) %H:%M:%S')}"

        chat_id = settings.get('chat_id')
        active_reminders_text = "None"
        if chat_id:
            active_reminders_text = await scheduler_service.get_active_reminders_string(chat_id, user_tz_name)

        tech_instruction = (
            f"\n\n[SYSTEM INFO]\n"
            f"- Local Time: {current_time_str} ({user_tz_name})\n"
            f"- Language: '{current_lang}'\n"
            f"- Active Reminders:\n{active_reminders_text}\n"
            f"PROTOCOL FOR REMINDERS:\n"
            f"1. To CREATE: Call `calculate_date` (include Day AND Time), then `schedule_reminder`.\n"
            f"2. To CHANGE: Call `delete_reminder(id)` for the old one, then create a new one.\n"
        )
        full_sys_inst = (system_instruction_text or "") + tech_instruction

        prompt_content = "Hello"
        if history and history[-1]['role'] == 'user':
            last_msg = history.pop()
            prompt_content = last_msg['parts'][0]

        tools_obj = self._get_tools_proto(allow_search)
        model = genai.GenerativeModel(model_name=model_name, system_instruction=full_sys_inst, tools=[tools_obj])
        chat = model.start_chat(history=history)
        
        keep_generating = True
        current_prompt = prompt_content
        stop_generating_text = False

        while keep_generating:
            keep_generating = False
            try:
                response_stream = await chat.send_message_async(
                    current_prompt if isinstance(current_prompt, str) else current_prompt, 
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
                    if chunk.text and not stop_generating_text:
                        yield chunk.text

                if function_call_found:
                    try: await response_stream.resolve()
                    except: pass

                if function_call_found and function_call_part:
                    fn_name = function_call_part.name
                    fn_args = {}
                    if function_call_part.args:
                        for key, value in function_call_part.args.items(): fn_args[key] = value
                    
                    logger.info(f"🤖 Gemini Tool: {fn_name} | Args: {fn_args}")
                    api_response = {}
                    
                    if fn_name == "calculate_date":
                        query = fn_args.get("query")
                        iso_result = calculate_future_date(query, user_tz_name)
                        api_response = {"iso_date_utc": iso_result}
                        keep_generating = True
                        
                    elif fn_name == "web_search":
                        res = await perform_search(fn_args.get("query", ""))
                        api_response = {"result": res}
                        
                    elif fn_name == "set_language":
                        lang = fn_args.get("lang_code", "uk")
                        yield f"__SET_LANGUAGE:{lang}__"
                        api_response = {"status": "ok"}
                        
                    elif fn_name == "delete_reminder":
                        rem_id = int(fn_args.get("reminder_id"))
                        success = await scheduler_service.delete_reminder_by_id(rem_id)
                        api_response = {"status": "deleted" if success else "error"}
                        if success: yield f"\n🗑 <b>Видалено нагадування ID: {rem_id}</b>"
                        keep_generating = True

                    elif fn_name == "schedule_reminder":
                        try:
                            iso_time = fn_args.get("iso_time_utc") 
                            text = fn_args.get("text")
                            user_id = settings.get('user_id')
                            if user_id and chat_id and iso_time:
                                dt_utc = datetime.datetime.fromisoformat(iso_time.replace("Z", "+00:00"))
                                await scheduler_service.add_reminder(user_id, chat_id, text, dt_utc)
                                dt_local = dt_utc.astimezone(tz)
                                display_time = dt_local.strftime("%d.%m.%Y %H:%M")
                                yield f"\n✅ <b>Нагадування встановлено!</b>\n📅 {display_time}\n📝 <i>{text}</i>"
                                api_response = {"status": "success"}
                                stop_generating_text = True
                            else:
                                api_response = {"status": "error", "message": "Missing info"}
                        except Exception as e:
                            api_response = {"status": "error", "message": str(e)}

                    current_prompt = genai.protos.Content(
                        parts=[genai.protos.Part(
                            function_response=genai.protos.FunctionResponse(name=fn_name, response=api_response)
                        )]
                    )

            except Exception as e:
                logger.error(f"Gemini Loop Error: {e}")
                yield f"⚠️ Error: {str(e)}"
                keep_generating = False

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