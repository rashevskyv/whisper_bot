import base64
import json
import logging
import datetime
import zoneinfo
from datetime import timedelta
from typing import AsyncGenerator, List, Dict, Any
from openai import AsyncOpenAI, APIError
from bot.ai.base import LLMProvider
from bot.utils.search import perform_search
from bot.utils.scheduler import scheduler_service
from bot.utils.date_helper import calculate_future_date
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

    def _get_tools_schema(self, allow_search: bool) -> List[Dict[str, Any]]:
        tools = [
            {
                "type": "function",
                "function": {
                    "name": "calculate_date",
                    "description": "Convert LOCAL datetime string to UTC ISO.",
                    "parameters": {
                        "type": "object",
                        "properties": {"local_datetime": {"type": "string"}},
                        "required": ["local_datetime"]
                    }
                }
            },
            {
                "type": "function",
                "function": {
                    "name": "schedule_reminder",
                    "description": "Schedule reminder in DB.",
                    "parameters": {
                        "type": "object", 
                        "properties": {"iso_time_utc": {"type": "string"}, "text": {"type": "string"}}, 
                        "required": ["iso_time_utc", "text"]
                    }
                }
            },
            {
                "type": "function",
                "function": {
                    "name": "delete_reminder",
                    "description": "Delete reminder.",
                    "parameters": {
                        "type": "object", 
                        "properties": {"reminder_id": {"type": "integer"}}, 
                        "required": ["reminder_id"]
                    }
                }
            }
        ]
        if allow_search:
            tools.append({
                "type": "function",
                "function": {
                    "name": "web_search",
                    "description": "Search web.",
                    "parameters": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]}
                }
            })
        return tools

    async def generate_stream(self, messages: List[Dict[str, str]], settings: Dict[str, Any]) -> AsyncGenerator[str, None]:
        model = settings.get('model', 'gpt-4o-mini')
        user_tz_name = settings.get('timezone', BOT_TIMEZONE)
        chat_id = settings.get('chat_id')
        user_id = settings.get('user_id')
        
        # Перевірка на відключення інструментів
        disable_tools = settings.get('disable_tools', False)

        # Upgrade model logic (only if tools are enabled)
        if not disable_tools and any("нагадай" in m.get('content', '').lower() for m in messages[-2:]):
             model = 'gpt-4o'

        try: tz = zoneinfo.ZoneInfo(user_tz_name)
        except: tz = zoneinfo.ZoneInfo("UTC")
            
        now_local = datetime.datetime.now(tz)
        current_time_meta = now_local.strftime('%Y-%m-%d %H:%M:%S (%A)')
        
        active_reminders_text = "None"
        if chat_id and not disable_tools:
            active_reminders_text = await scheduler_service.get_active_reminders_string(chat_id, user_tz_name)

        local_messages = [msg.copy() for msg in messages]
        
        # System Prompt Injection
        sys_idx = next((i for i, m in enumerate(local_messages) if m['role'] == 'system'), None)
        system_base = "STRICT RULES: Be helpful and concise."
        
        if not disable_tools:
            system_base += "\nFor reminders: use [REAL-TIME CLOCK] to calculate absolute time. Tool order: calculate_date -> schedule_reminder."
        
        if sys_idx is not None: local_messages[sys_idx]['content'] += f"\n{system_base}"
        else: local_messages.insert(0, {"role": "system", "content": system_base})

        # Clock Injection
        clock_metadata = f"--- [REAL-TIME CLOCK] ---\nLocal Time: {current_time_meta}\nUser Timezone: {user_tz_name}\nActive Reminders: {active_reminders_text}\n--- END METADATA ---"
        
        for msg in reversed(local_messages):
            if msg['role'] == 'user':
                msg['content'] = f"{clock_metadata}\n\nUSER REQUEST: {msg['content']}"
                break

        # ВАЖЛИВО: Якщо tools вимкнено, передаємо None
        tools = self._get_tools_schema(settings.get('allow_search', True)) if not disable_tools else None

        try:
            stream = await self.client.chat.completions.create(
                model=model, messages=local_messages, temperature=settings.get('temperature', 0.7), tools=tools, stream=True
            )

            while True:
                tool_calls_buffer = {}
                is_tool_call = False
                async for chunk in stream:
                    if not chunk.choices: continue
                    delta = chunk.choices[0].delta
                    if delta.tool_calls:
                        is_tool_call = True
                        for tc in delta.tool_calls:
                            idx = tc.index
                            if idx not in tool_calls_buffer: tool_calls_buffer[idx] = {"id": tc.id, "name": "", "arguments": ""}
                            if tc.id: tool_calls_buffer[idx]["id"] = tc.id
                            if tc.function.name: tool_calls_buffer[idx]["name"] += tc.function.name
                            if tc.function.arguments: tool_calls_buffer[idx]["arguments"] += tc.function.arguments
                    if delta.content and not is_tool_call: yield delta.content

                if not is_tool_call: break

                # Якщо ми тут, значить були tools, але вони мали бути вимкнені? 
                # Ні, якщо tools=None, сюди ми не дійдемо.
                
                tool_calls_list = [tool_calls_buffer[i] for i in sorted(tool_calls_buffer.keys())]
                local_messages.append({"role": "assistant", "tool_calls": [{"id": tc["id"], "type": "function", "function": {"name": tc["name"], "arguments": tc["arguments"]}} for tc in tool_calls_list]})

                should_stop_stream = False
                for tc in tool_calls_list:
                    name, args = tc["name"], json.loads(tc["arguments"])
                    content = ""
                    logger.info(f"🤖 OpenAI Tool: {name} | Args: {args}")

                    if name == "calculate_date":
                        content = calculate_future_date(args.get("local_datetime"), user_tz_name)
                    elif name == "schedule_reminder":
                        try:
                            iso_utc = args.get("iso_time_utc")
                            text = args.get("text")
                            dt_utc = datetime.datetime.fromisoformat(iso_utc.replace("Z", "+00:00"))
                            await scheduler_service.add_reminder(user_id, chat_id, text, dt_utc)
                            l_dt = dt_utc.astimezone(tz)
                            days = {"Monday":"Пн","Tuesday":"Вт","Wednesday":"Ср","Thursday":"Чт","Friday":"Пт","Saturday":"Сб","Sunday":"Нд"}
                            d_name = days.get(l_dt.strftime("%A"), l_dt.strftime("%a"))
                            yield f"\n✅ <b>Встановлено:</b> {d_name}, {l_dt.strftime('%d.%m %H:%M')}\n📝 <i>{text}</i>"
                            content, should_stop_stream = "DONE", True
                        except Exception as e: content = f"ERROR: {e}"
                    elif name == "delete_reminder":
                        success = await scheduler_service.delete_reminder_by_id(args.get("reminder_id"))
                        content = "Deleted" if success else "Not found"
                    elif name == "web_search":
                        yield "\n🔎 <i>Шукаю...</i>\n"
                        content = await perform_search(args.get("query"))

                    local_messages.append({"role": "tool", "tool_call_id": tc["id"], "content": str(content)})
                
                if should_stop_stream: break
                stream = await self.client.chat.completions.create(model=model, messages=local_messages, tools=tools, stream=True)
        except Exception as e:
            logger.error(f"AI Stream Error: {e}")
            yield f"⚠️ Помилка: {e}"

    async def transcribe(self, audio_path: str, language: str = None) -> str:
        try:
            with open(audio_path, "rb") as f:
                res = await self.client.audio.transcriptions.create(model="whisper-1", file=f, language=language[:2] if language else None)
            return res.text
        except Exception as e: return f"Error: {e}"

    async def analyze_image(self, image_path: str, prompt: str, messages: List[Dict[str, str]] = None, settings: Dict[str, Any] = None) -> AsyncGenerator[str, None]:
        try:
            with open(image_path, "rb") as f:
                b64 = base64.b64encode(f.read()).decode('utf-8')
            msg = [{"role": "user", "content": [{"type": "text", "text": prompt}, {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}}]}]
            stream = await self.client.chat.completions.create(model="gpt-4o", messages=msg, max_tokens=1000, stream=True)
            async for chunk in stream:
                if chunk.choices[0].delta.content: yield chunk.choices[0].delta.content
        except Exception as e: yield f"⚠️ Error: {e}"