import base64
import json
import logging
import datetime
import zoneinfo
from typing import AsyncGenerator, List, Dict, Any
from openai import AsyncOpenAI, APIError
from bot.ai.base import LLMProvider
from bot.utils.search import perform_search
from bot.utils.scheduler import scheduler_service
from bot.utils.date_helper import calculate_future_date # НОВИЙ ІМПОРТ
from config import BOT_TIMEZONE

logger = logging.getLogger(__name__)

class OpenAIProvider(LLMProvider):
    def __init__(self, api_key: str):
        self.client = AsyncOpenAI(api_key=api_key)

    async def validate_key(self, api_key: str) -> bool:
        temp = AsyncOpenAI(api_key=api_key)
        try:
            await temp.models.list()
            return True
        except: return False
        finally: await temp.close()

    async def generate_stream(self, messages: List[Dict[str, str]], settings: Dict[str, Any]) -> AsyncGenerator[str, None]:
        model = settings.get('model', 'gpt-4o-mini')
        temp = settings.get('temperature', 0.7)
        allow_search = settings.get('allow_search', True)
        current_lang = settings.get('language', 'uk')
        user_tz_name = settings.get('timezone', BOT_TIMEZONE)

        # SMART UPGRADE
        if allow_search and model == 'gpt-4o-mini' and any("нагадай" in m.get('content', '').lower() for m in messages[-2:]):
             model = 'gpt-4o'

        tools = []
        # 1. Date Tool
        tools.append({
            "type": "function",
            "function": {
                "name": "calculate_date",
                "description": "Calculate exact ISO date from natural text. Use BEFORE scheduling.",
                "parameters": {
                    "type": "object",
                    "properties": {"query": {"type": "string"}},
                    "required": ["query"]
                }
            }
        })

        # 2. Scheduler
        tools.append({
            "type": "function",
            "function": {
                "name": "schedule_reminder",
                "description": "Schedule a reminder.",
                "parameters": {
                    "type": "object", 
                    "properties": {
                        "iso_time_utc": {"type": "string", "description": "ISO-8601 datetime in UTC."},
                        "text": {"type": "string", "description": "Reminder text."}
                    }, 
                    "required": ["iso_time_utc", "text"]
                }
            }
        })

        # 3. Delete Tool (NEW)
        tools.append({
            "type": "function",
            "function": {
                "name": "delete_reminder",
                "description": "Delete a reminder by ID.",
                "parameters": {
                    "type": "object", 
                    "properties": {"reminder_id": {"type": "integer"}}, 
                    "required": ["reminder_id"]
                }
            }
        })

        if allow_search:
            tools.append({
                "type": "function",
                "function": {
                    "name": "web_search",
                    "description": "Find info.",
                    "parameters": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]}
                }
            })
        
        tools.append({
            "type": "function",
            "function": {
                "name": "set_language",
                "description": "Switch language.",
                "parameters": {"type": "object", "properties": {"lang_code": {"type": "string"}}, "required": ["lang_code"]}
            }
        })

        try: tz = zoneinfo.ZoneInfo(user_tz_name)
        except: tz = datetime.timezone.utc
        now_local = datetime.datetime.now(tz)
        current_time_str = f"{now_local.strftime('%Y-%m-%d (%A) %H:%M:%S')}"
        
        # GET REMINDERS CONTEXT
        chat_id = settings.get('chat_id')
        active_reminders_text = "None"
        if chat_id:
            active_reminders_text = await scheduler_service.get_active_reminders_string(chat_id, user_tz_name)

        local_messages = [msg.copy() for msg in messages]
        sys_idx = next((i for i, m in enumerate(local_messages) if m['role'] == 'system'), None)
        
        instr = (
            f"\n\n[SYSTEM INFO]\n"
            f"- Time: {current_time_str} ({user_tz_name})\n"
            f"- Active Reminders:\n{active_reminders_text}\n"
            f"PROTOCOL: \n"
            f"1. PRE-PROCESS dates using `calculate_date` (combine day + time, e.g. 'Next Saturday 15:00').\n"
            f"2. IF user wants to change a reminder: DELETE the old one first using `delete_reminder`, then SCHEDULE new one."
        )
        if sys_idx is not None: local_messages[sys_idx]['content'] += instr
        else: local_messages.insert(0, {"role": "system", "content": instr})

        try:
            stream = await self.client.chat.completions.create(
                model=model, messages=local_messages, temperature=temp, tools=tools, stream=True
            )

            while True:
                tool_calls_buffer = {}
                is_tool_call = False
                
                async for chunk in stream:
                    delta = chunk.choices[0].delta
                    if delta.tool_calls:
                        is_tool_call = True
                        for tc in delta.tool_calls:
                            idx = tc.index
                            if idx not in tool_calls_buffer: tool_calls_buffer[idx] = {"id": tc.id, "name": "", "arguments": ""}
                            if tc.id: tool_calls_buffer[idx]["id"] = tc.id
                            if tc.function.name: tool_calls_buffer[idx]["name"] += tc.function.name
                            if tc.function.arguments: tool_calls_buffer[idx]["arguments"] += tc.function.arguments
                    
                    if delta.content and not is_tool_call:
                        yield delta.content

                if not is_tool_call:
                    break

                tool_calls_list = [tool_calls_buffer[i] for i in sorted(tool_calls_buffer.keys())]
                
                local_messages.append({
                    "role": "assistant",
                    "tool_calls": [{"id": tc["id"], "type": "function", "function": {"name": tc["name"], "arguments": tc["arguments"]}} for tc in tool_calls_list]
                })

                should_stop_stream = False

                for tc in tool_calls_list:
                    name = tc["name"]
                    try: args = json.loads(tc["arguments"])
                    except: args = {}
                    
                    content = ""
                    logger.info(f"🤖 OpenAI Tool: {name} | Args: {args}")

                    if name == "calculate_date":
                        query = args.get("query")
                        content = calculate_future_date(query, user_tz_name)
                        
                    elif name == "delete_reminder":
                        rem_id = args.get("reminder_id")
                        success = await scheduler_service.delete_reminder_by_id(rem_id)
                        if success:
                            yield f"\n🗑 <b>Видалено старе нагадування (ID: {rem_id})</b>"
                            content = "Deleted."
                        else:
                            content = "ID not found."

                    elif name == "schedule_reminder":
                        try:
                            iso_time = args.get("iso_time_utc")
                            text = args.get("text")
                            user_id = settings.get('user_id')
                            chat_id = settings.get('chat_id')
                            
                            if user_id and chat_id and iso_time:
                                dt_utc = datetime.datetime.fromisoformat(iso_time.replace("Z", "+00:00"))
                                rid = await scheduler_service.add_reminder(user_id, chat_id, text, dt_utc)
                                
                                dt_local = dt_utc.astimezone(tz)
                                display_time = dt_local.strftime("%d.%m.%Y %H:%M")
                                
                                days_map = {"Monday": "Понеділок", "Tuesday": "Вівторок", "Wednesday": "Середа", "Thursday": "Четвер", "Friday": "П'ятниця", "Saturday": "Субота", "Sunday": "Неділя"}
                                day_name = days_map.get(dt_local.strftime("%A"), dt_local.strftime("%A"))

                                yield f"\n✅ <b>Нагадування встановлено!</b>\n📅 {day_name}, {display_time}\n📝 <i>{text}</i>"
                                
                                content = "DONE."
                                should_stop_stream = True
                            else:
                                content = "ERROR: Missing info."
                        except Exception as e:
                            logger.error(f"Reminder Error: {e}")
                            content = f"ERROR: {str(e)}"

                    elif name == "web_search":
                        yield "\n\n🔎 <i>Шукаю в інтернеті...</i>\n"
                        content = await perform_search(args.get("query", ""))

                    local_messages.append({
                        "role": "tool", 
                        "tool_call_id": tc["id"], 
                        "content": str(content)
                    })

                if should_stop_stream:
                    break

                stream = await self.client.chat.completions.create(
                    model=model, messages=local_messages, tools=tools, stream=True
                )

        except APIError as e:
            logger.error(f"OpenAI Error: {e}")
            yield f"⚠️ API Error: {e}"
        except Exception as e:
            logger.error(f"Error: {e}", exc_info=True)
            yield "⚠️ Error."

    async def transcribe(self, audio_path: str, language: str = None) -> str:
        try:
            with open(audio_path, "rb") as f:
                kwargs = {"model": "whisper-1", "file": f}
                if language and len(language) == 2: kwargs['language'] = language
                res = await self.client.audio.transcriptions.create(**kwargs)
            return res.text
        except Exception as e:
            logger.error(f"Transcribe Error: {e}")
            return f"Error: {e}"

    async def analyze_image(self, image_path: str, prompt: str, messages: List[Dict[str, str]] = None) -> AsyncGenerator[str, None]:
        try:
            with open(image_path, "rb") as image_file:
                base64_image = base64.b64encode(image_file.read()).decode('utf-8')
            content = [{"type": "text", "text": prompt}, {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{base64_image}"}}]
            req_msgs = [m.copy() for m in messages] if messages else []
            req_msgs.append({"role": "user", "content": content})
            stream = await self.client.chat.completions.create(model="gpt-4o", messages=req_msgs, max_tokens=1000, stream=True)
            async for chunk in stream:
                if chunk.choices[0].delta.content: yield chunk.choices[0].delta.content
        except Exception as e:
            logger.error(f"Vision error: {e}")
            yield f"⚠️ Error: {str(e)}"