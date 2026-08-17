import os
import base64
import json
import logging
import datetime
import zoneinfo
from datetime import timedelta
from typing import AsyncGenerator, List, Dict, Any
from openai import AsyncOpenAI, APIError
from bot.ai.base import LLMProvider
from bot.utils.search import perform_search, extract_source_links, format_sources_html
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
        disable_tools = settings.get('disable_tools', False) # ПРАПОРЕЦЬ

        try: tz = zoneinfo.ZoneInfo(user_tz_name)
        except: tz = zoneinfo.ZoneInfo("UTC")

        now_local = datetime.datetime.now(tz)
        current_time_meta = now_local.strftime('%Y-%m-%d %H:%M:%S (%A)')

        active_reminders_text = "None"
        if chat_id and not disable_tools:
            active_reminders_text = await scheduler_service.get_active_reminders_string(chat_id, user_tz_name)

        local_messages = [msg.copy() for msg in messages]

        # System Prompt Injection (base)
        sys_idx = next((i for i, m in enumerate(local_messages) if m['role'] == 'system'), None)
        system_base = "STRICT RULES: Be helpful and concise."

        if not disable_tools:
            system_base += "\nFor reminders: use [REAL-TIME CLOCK] to calculate absolute time. Tool order: calculate_date -> schedule_reminder."

        if sys_idx is not None: local_messages[sys_idx]['content'] += f"\n{system_base}"
        else: local_messages.insert(0, {"role": "system", "content": system_base})

        # Clock Injection (Metadata)
        clock_metadata = (
            f"--- [REAL-TIME CLOCK] ---\n"
            f"Current Local Time: {current_time_meta}\n"
            f"User Timezone: {user_tz_name}\n"
            f"Active Reminders:\n{active_reminders_text}\n"
            f"--- END METADATA ---"
        )

        for msg in reversed(local_messages):
            if msg['role'] == 'user':
                msg['content'] = f"{clock_metadata}\n\nUSER REQUEST: {msg['content']}"
                break

        # ВАЖЛИВО: tools = None, якщо disable_tools=True
        tools = self._get_tools_schema(settings.get('allow_search', True)) if not disable_tools else None

        collected_source_urls: List[str] = []
        try:
            stream = await self.client.chat.completions.create(
                model=model, messages=local_messages, temperature=settings.get('temperature', 0.7), tools=tools, stream=True
            )

            while True:
                tool_calls_buffer = {}
                is_tool_call = False
                response = None

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
                    response = chunk # Зберігаємо останній chunk (для usage)

                # Логування використання токенів
                if response and hasattr(response, 'usage') and response.usage:
                    usage = response.usage
                    logger.info(f"📊 [OpenAI] Usage: Prompt={usage.prompt_tokens}, Completion={usage.completion_tokens}, Total={usage.total_tokens}")

                if not is_tool_call: break

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
                        for link in extract_source_links(str(content)):
                            if link not in collected_source_urls and len(collected_source_urls) < 5:
                                collected_source_urls.append(link)

                    local_messages.append({"role": "tool", "tool_call_id": tc["id"], "content": str(content)})

                if should_stop_stream: break
                stream = await self.client.chat.completions.create(model=model, messages=local_messages, tools=tools, stream=True)

            if collected_source_urls:
                yield format_sources_html(collected_source_urls)

        except Exception as e:
            logger.error(f"AI Stream Error: {e}")
            yield f"⚠️ Помилка AI: {e}"

    async def transcribe(
        self,
        audio_path: str,
        language: str = None,
        prompt: str = None,
        keywords: List[str] = None
    ) -> str:
        kwargs: Dict[str, Any] = {
            "model": "gpt-transcribe",
        }
        if prompt and prompt.strip():
            kwargs["prompt"] = prompt.strip()

        extra_body: Dict[str, Any] = {}
        if language:
            lang_code = language.strip()[:2].lower()
            if lang_code:
                extra_body["languages"] = [lang_code]

        if keywords:
            clean_keywords = [k.strip() for k in keywords if isinstance(k, str) and k.strip()]
            if clean_keywords:
                extra_body["keywords"] = clean_keywords

        if extra_body:
            kwargs["extra_body"] = extra_body

        try:
            filename = os.path.basename(audio_path)
            base, ext = os.path.splitext(filename)
            if ext.lower() in [".oga", ".opus"]:
                filename = f"{base}.ogg"
            elif ext.lower() not in [".flac", ".mp3", ".mp4", ".mpeg", ".mpga", ".m4a", ".ogg", ".wav", ".webm"]:
                filename = f"{base}.ogg"

            with open(audio_path, "rb") as f:
                kwargs["file"] = (filename, f)
                res = await self.client.audio.transcriptions.create(**kwargs)
            return res.text
        except Exception as e:
            logger.error(f"OpenAI transcription error: {e}")
            raise

    async def analyze_image(self, image_path: str, prompt: str, messages: List[Dict[str, str]] = None, settings: Dict[str, Any] = None) -> AsyncGenerator[str, None]:
        model = (settings or {}).get('model', 'gpt-4o-mini')
        if model not in ['gpt-4o-mini', 'gpt-4o', 'gpt-4-turbo']:
            model = 'gpt-4o-mini'
        try:
            with open(image_path, "rb") as f:
                b64 = base64.b64encode(f.read()).decode('utf-8')
            msg = [{"role": "user", "content": [{"type": "text", "text": prompt}, {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}}]}]
            stream = await self.client.chat.completions.create(model=model, messages=msg, max_tokens=1000, stream=True)
            async for chunk in stream:
                if chunk.choices[0].delta.content: yield chunk.choices[0].delta.content
        except Exception as e: yield f"⚠️ Error: {e}"