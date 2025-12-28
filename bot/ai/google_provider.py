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
        
        date_func = FunctionDeclaration(
            name="calculate_date",
            description="Convert LOCAL datetime string to UTC ISO.",
            parameters=Schema(type=Type.OBJECT, properties={"local_datetime": Schema(type=Type.STRING)}, required=["local_datetime"])
        )
        declarations.append(date_func)

        reminder_func = FunctionDeclaration(
            name="schedule_reminder",
            description="Schedule reminder.",
            parameters=Schema(type=Type.OBJECT, properties={"iso_time_utc": Schema(type=Type.STRING), "text": Schema(type=Type.STRING)}, required=["iso_time_utc", "text"])
        )
        declarations.append(reminder_func)

        del_func = FunctionDeclaration(
            name="delete_reminder",
            description="Delete reminder.",
            parameters=Schema(type=Type.OBJECT, properties={"reminder_id": Schema(type=Type.INTEGER)}, required=["reminder_id"])
        )
        declarations.append(del_func)

        if allow_search:
            search_func = FunctionDeclaration(
                name="web_search",
                description="Search web.",
                parameters=Schema(type=Type.OBJECT, properties={"query": Schema(type=Type.STRING)}, required=["query"])
            )
            declarations.append(search_func)

        return Tool(function_declarations=declarations)

    async def generate_stream(self, messages: List[Dict[str, str]], settings: Dict[str, Any]) -> AsyncGenerator[str, None]:
        model_name = settings.get('model', self.model_name)
        if 'gpt' in model_name: model_name = 'gemini-1.5-flash'
        
        user_tz_name = settings.get('timezone', BOT_TIMEZONE)
        try: tz = zoneinfo.ZoneInfo(user_tz_name)
        except: tz = datetime.timezone.utc
        now_local = datetime.datetime.now(tz)
        current_time_str = now_local.strftime('%Y-%m-%d %H:%M:%S (%A)')

        chat_id = settings.get('chat_id')
        active_reminders_text = await scheduler_service.get_active_reminders_string(chat_id, user_tz_name) if chat_id else "None"

        system_instruction_text, history = self._map_messages(messages)
        
        tech_instruction = (
            f"\n\n[REAL-TIME CLOCK]\n"
            f"Local Time: {current_time_str}\n"
            f"Timezone: {user_tz_name}\n"
            f"Active Reminders: {active_reminders_text}\n"
            f"PROTOCOL:\n"
            f"1. Calculate absolute time yourself based on the Clock.\n"
            f"2. Call `calculate_date` with the local datetime string.\n"
            f"3. Use result for `schedule_reminder`."
        )
        full_sys_inst = (system_instruction_text or "") + tech_instruction

        prompt_content = "Hello"
        if history and history[-1]['role'] == 'user':
            last_msg = history.pop()
            prompt_content = last_msg['parts'][0]

        tools_obj = self._get_tools_proto(settings.get('allow_search', True))
        model = genai.GenerativeModel(model_name=model_name, system_instruction=full_sys_inst, tools=[tools_obj])
        chat = model.start_chat(history=history)
        
        keep_generating = True
        current_prompt = prompt_content

        while keep_generating:
            keep_generating = False
            try:
                response_stream = await chat.send_message_async(
                    current_prompt, 
                    generation_config=genai.types.GenerationConfig(temperature=settings.get('temperature', 0.7)),
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
                    except: pass
                    
                    fn_name = function_call_part.name
                    fn_args = {k: v for k, v in function_call_part.args.items()}
                    logger.info(f"🤖 Gemini Tool: {fn_name} | Args: {fn_args}")
                    api_response = {}
                    
                    if fn_name == "calculate_date":
                        res = calculate_future_date(fn_args.get("local_datetime"), user_tz_name)
                        api_response = {"iso_date_utc": res}
                        keep_generating = True
                        
                    elif fn_name == "schedule_reminder":
                        try:
                            iso = fn_args.get("iso_time_utc")
                            text = fn_args.get("text")
                            dt_utc = datetime.datetime.fromisoformat(iso.replace("Z", "+00:00"))
                            await scheduler_service.add_reminder(settings.get('user_id'), chat_id, text, dt_utc)
                            l_dt = dt_utc.astimezone(tz)
                            days = {"Monday":"Пн","Tuesday":"Вт","Wednesday":"Ср","Thursday":"Чт","Friday":"Пт","Saturday":"Сб","Sunday":"Нд"}
                            d_name = days.get(l_dt.strftime("%A"), l_dt.strftime("%a"))
                            yield f"\n✅ <b>Встановлено:</b> {d_name}, {l_dt.strftime('%d.%m %H:%M')}\n📝 <i>{text}</i>"
                            api_response = {"status": "success"}
                        except Exception as e: api_response = {"status": "error", "message": str(e)}

                    elif fn_name == "delete_reminder":
                        success = await scheduler_service.delete_reminder_by_id(int(fn_args.get("reminder_id")))
                        api_response = {"status": "deleted" if success else "error"}
                        if success: yield f"\n🗑 <b>Видалено ID: {fn_args.get('reminder_id')}</b>"
                        
                    elif fn_name == "web_search":
                        res = await perform_search(fn_args.get("query", ""))
                        api_response = {"result": res}

                    current_prompt = genai.protos.Content(parts=[genai.protos.Part(function_response=genai.protos.FunctionResponse(name=fn_name, response=api_response))])

            except Exception as e:
                logger.error(f"Gemini Loop Error: {e}")
                yield f"⚠️ Error: {str(e)}"
                keep_generating = False

    async def transcribe(self, audio_path: str, language: str = None) -> str:
        try:
            with open(audio_path, "rb") as f:
                data = f.read()
            model = genai.GenerativeModel(self.model_name)
            response = await model.generate_content_async([{'mime_type': 'audio/mp3', 'data': data}, "Transcribe this audio."])
            return response.text.strip()
        except Exception as e: return f"Error: {e}"

    async def analyze_image(self, image_path: str, prompt: str, messages: List[Dict[str, str]] = None, settings: Dict[str, Any] = None) -> AsyncGenerator[str, None]:
        try:
            import PIL.Image
            img = PIL.Image.open(image_path)
            model_name = settings.get('model', self.model_name) if settings else self.model_name
            model = genai.GenerativeModel(model_name)
            response = await model.generate_content_async([prompt, img], stream=True)
            async for chunk in response:
                if chunk.text: yield chunk.text
        except Exception as e: yield f"⚠️ Error: {e}"