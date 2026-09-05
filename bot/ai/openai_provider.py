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
from bot.utils.search import format_sources_html
from bot.ai.tools import get_openai_tools, execute_tool, get_active_reminders_summary
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
            active_reminders_text = await get_active_reminders_summary(chat_id, user_tz_name)

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
        tools = get_openai_tools(settings.get('allow_search', True)) if not disable_tools else None

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
                    name = tc["name"]
                    try:
                        args = json.loads(tc["arguments"])
                    except Exception:
                        args = None

                    logger.info(f"🤖 OpenAI Tool: {name}")

                    tool_result = await execute_tool(
                        name,
                        args,
                        user_id=user_id,
                        chat_id=chat_id,
                        timezone_name=user_tz_name,
                        source_message_id=settings.get("source_message_id"),
                    )

                    if tool_result.draft_id is not None:
                        settings["_action_draft_id"] = tool_result.draft_id
                        settings.pop("_shopping_list_id", None)
                    elif tool_result.shopping_list_id is not None:
                        settings["_shopping_list_id"] = tool_result.shopping_list_id
                        settings.pop("_action_draft_id", None)

                    if tool_result.display_text:
                        yield tool_result.display_text

                    for url in tool_result.source_urls:
                        if url not in collected_source_urls and len(collected_source_urls) < 5:
                            collected_source_urls.append(url)

                    if tool_result.stop:
                        should_stop_stream = True

                    content = json.dumps(tool_result.payload, ensure_ascii=False)
                    local_messages.append({"role": "tool", "tool_call_id": tc["id"], "content": content})

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