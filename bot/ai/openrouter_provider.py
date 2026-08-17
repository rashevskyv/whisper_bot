import os
import base64
import json
import logging
import datetime
import zoneinfo
from datetime import timedelta
from typing import AsyncGenerator, List, Dict, Any, Optional
from openai import AsyncOpenAI, APIError
from bot.ai.base import LLMProvider
from bot.utils.search import perform_search, extract_source_links, format_sources_html
from bot.utils.scheduler import scheduler_service
from bot.utils.date_helper import calculate_future_date
from config import BOT_TIMEZONE

logger = logging.getLogger(__name__)

OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"

class OpenRouterProvider(LLMProvider):
    def __init__(self, api_key: str, model_name: str = "openai/gpt-5.6-luna"):
        self.api_key = api_key
        self.default_model = model_name
        self.client = AsyncOpenAI(
            api_key=api_key,
            base_url=OPENROUTER_BASE_URL,
            default_headers={
                "HTTP-Referer": "https://github.com/rashevskyv/whisper_bot",
                "X-Title": "Whisper Telegram Bot",
            }
        )

    async def validate_key(self, api_key: str) -> bool:
        temp = AsyncOpenAI(
            api_key=api_key,
            base_url=OPENROUTER_BASE_URL,
            default_headers={
                "HTTP-Referer": "https://github.com/rashevskyv/whisper_bot",
                "X-Title": "Whisper Telegram Bot",
            }
        )
        try:
            await temp.models.list()
            return True
        except Exception as e:
            logger.debug(f"OpenRouter key validation failed: {e}")
            return False
        finally:
            await temp.close()

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
        model = settings.get('model', self.default_model)
        user_tz_name = settings.get('timezone', BOT_TIMEZONE)
        chat_id = settings.get('chat_id')
        user_id = settings.get('user_id')
        disable_tools = settings.get('disable_tools', False)

        try:
            tz = zoneinfo.ZoneInfo(user_tz_name)
        except Exception:
            tz = zoneinfo.ZoneInfo("UTC")

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

        if sys_idx is not None:
            local_messages[sys_idx]['content'] += f"\n{system_base}"
        else:
            local_messages.insert(0, {"role": "system", "content": system_base})

        # Clock Injection
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

        tools = self._get_tools_schema(settings.get('allow_search', True)) if not disable_tools else None
        collected_source_urls: List[str] = []

        try:
            stream_kwargs: Dict[str, Any] = {
                "model": model,
                "messages": local_messages,
                "temperature": settings.get('temperature', 0.7),
                "stream": True
            }
            if tools:
                stream_kwargs["tools"] = tools

            stream = await self.client.chat.completions.create(**stream_kwargs)

            while True:
                tool_calls_buffer = {}
                is_tool_call = False

                async for chunk in stream:
                    if not chunk.choices:
                        continue
                    delta = chunk.choices[0].delta
                    if delta.tool_calls:
                        is_tool_call = True
                        for tc in delta.tool_calls:
                            idx = tc.index
                            if idx not in tool_calls_buffer:
                                tool_calls_buffer[idx] = {
                                    "id": tc.id,
                                    "name": tc.function.name if tc.function else "",
                                    "arguments": tc.function.arguments if tc.function else ""
                                }
                            else:
                                if tc.id:
                                    tool_calls_buffer[idx]["id"] = tc.id
                                if tc.function and tc.function.name:
                                    tool_calls_buffer[idx]["name"] += tc.function.name
                                if tc.function and tc.function.arguments:
                                    tool_calls_buffer[idx]["arguments"] += tc.function.arguments

                    elif delta.content:
                        yield delta.content

                if not is_tool_call:
                    if collected_source_urls:
                        yield format_sources_html(collected_source_urls)
                    break

                # Execute Tools
                local_messages.append({
                    "role": "assistant",
                    "tool_calls": [
                        {
                            "id": tc_data["id"],
                            "type": "function",
                            "function": {"name": tc_data["name"], "arguments": tc_data["arguments"]}
                        }
                        for tc_data in tool_calls_buffer.values()
                    ]
                })

                for tc_data in tool_calls_buffer.values():
                    fn_name = tc_data["name"]
                    try:
                        args = json.loads(tc_data["arguments"])
                    except Exception:
                        args = {}

                    fn_result = "error"
                    try:
                        if fn_name == "calculate_date":
                            iso_res = calculate_future_date(args.get("local_datetime"), user_tz_name)
                            fn_result = json.dumps({"iso_time_utc": iso_res} if iso_res else {"error": "Invalid date"})

                        elif fn_name == "schedule_reminder":
                            if not chat_id:
                                fn_result = json.dumps({"error": "No chat_id"})
                            else:
                                dt = datetime.datetime.fromisoformat(args.get("iso_time_utc"))
                                rem_id = await scheduler_service.add_reminder(chat_id, user_id, dt, args.get("text"))
                                fn_result = json.dumps({"success": True, "reminder_id": rem_id})

                        elif fn_name == "delete_reminder":
                            success = await scheduler_service.delete_reminder(args.get("reminder_id"))
                            fn_result = json.dumps({"success": success})

                        elif fn_name == "web_search":
                            q = args.get("query")
                            raw_search_res = await perform_search(q)
                            extracted = extract_source_links(raw_search_res)
                            for link in extracted:
                                if link not in collected_source_urls:
                                    collected_source_urls.append(link)
                            fn_result = json.dumps({"results": raw_search_res[:1500]})
                    except Exception as err:
                        fn_result = json.dumps({"error": str(err)})

                    local_messages.append({
                        "role": "tool",
                        "tool_call_id": tc_data["id"],
                        "name": fn_name,
                        "content": fn_result
                    })

                next_kwargs: Dict[str, Any] = {
                    "model": model,
                    "messages": local_messages,
                    "temperature": settings.get('temperature', 0.7),
                    "stream": True
                }
                if tools:
                    next_kwargs["tools"] = tools
                stream = await self.client.chat.completions.create(**next_kwargs)

        except Exception as e:
            logger.error(f"OpenRouter streaming error ({model}): {e}")
            yield f"⚠️ Помилка OpenRouter: {e}"

    async def transcribe(
        self,
        audio_path: str,
        language: str = None,
        prompt: str = None,
        keywords: List[str] = None
    ) -> str:
        raise NotImplementedError("OpenRouter does not support direct audio transcription. Use OpenAIProvider with gpt-transcribe.")

    async def analyze_image(
        self,
        image_path: str,
        prompt: str,
        messages: List[Dict[str, str]] = None,
        settings: Dict[str, Any] = None
    ) -> AsyncGenerator[str, None]:
        model = (settings or {}).get('model', self.default_model)
        try:
            with open(image_path, "rb") as f:
                b64 = base64.b64encode(f.read()).decode('utf-8')

            ext = os.path.splitext(image_path)[1].lower().replace(".", "")
            mime = f"image/{ext}" if ext in ["jpeg", "jpg", "png", "webp", "gif"] else "image/jpeg"
            if mime == "image/jpg":
                mime = "image/jpeg"

            user_content = [
                {"type": "text", "text": prompt},
                {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}}
            ]

            msgs = [{"role": "user", "content": user_content}]
            if messages:
                for m in messages:
                    if m.get("role") != "user":
                        msgs.insert(0, m)

            stream = await self.client.chat.completions.create(
                model=model,
                messages=msgs,
                stream=True
            )
            async for chunk in stream:
                if chunk.choices and chunk.choices[0].delta.content:
                    yield chunk.choices[0].delta.content
        except Exception as e:
            logger.error(f"OpenRouter vision error ({model}): {e}")
            yield f"⚠️ Помилка аналізу зображення OpenRouter: {e}"
