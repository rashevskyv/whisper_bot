import logging
import datetime
import zoneinfo
from typing import AsyncGenerator, List, Dict, Any
import google.generativeai as genai
from google.ai.generativelanguage import FunctionDeclaration, Tool, Schema, Type
from bot.ai.base import LLMProvider
from config import DEFAULT_SETTINGS, BOT_TIMEZONE
from bot.utils.search import format_sources_html
from bot.ai.tools import get_tool_definitions, execute_tool, get_active_reminders_summary

logger = logging.getLogger(__name__)

_TYPE_MAP = {
    "object": Type.OBJECT,
    "string": Type.STRING,
    "integer": Type.INTEGER,
    "array": Type.ARRAY,
}

def _json_schema_to_google_schema(schema_dict: Dict[str, Any]) -> Schema:
    type_str = schema_dict.get("type", "object")
    gtype = _TYPE_MAP.get(type_str, Type.OBJECT)

    kwargs: Dict[str, Any] = {"type": gtype}

    if "description" in schema_dict:
        kwargs["description"] = schema_dict["description"]

    if "properties" in schema_dict:
        properties = {}
        for prop_name, prop_def in schema_dict["properties"].items():
            properties[prop_name] = _json_schema_to_google_schema(prop_def)
        kwargs["properties"] = properties

    if "items" in schema_dict:
        kwargs["items"] = _json_schema_to_google_schema(schema_dict["items"])

    if "enum" in schema_dict:
        kwargs["enum"] = schema_dict["enum"]

    if "required" in schema_dict:
        kwargs["required"] = schema_dict["required"]

    return Schema(**kwargs)

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
        for tool_def in get_tool_definitions(allow_search):
            decl = FunctionDeclaration(
                name=tool_def["name"],
                description=tool_def.get("description", ""),
                parameters=_json_schema_to_google_schema(tool_def.get("parameters", {}))
            )
            declarations.append(decl)
        return Tool(function_declarations=declarations)

    async def generate_stream(self, messages: List[Dict[str, str]], settings: Dict[str, Any]) -> AsyncGenerator[str, None]:
        model_name = settings.get('model', self.model_name)
        disable_tools = settings.get('disable_tools', False) # ПРАПОРЕЦЬ

        user_tz_name = settings.get('timezone', BOT_TIMEZONE)
        try: tz = zoneinfo.ZoneInfo(user_tz_name)
        except: tz = datetime.timezone.utc
        now_local = datetime.datetime.now(tz)
        current_time_str = now_local.strftime('%Y-%m-%d %H:%M:%S (%A)')

        chat_id = settings.get('chat_id')
        active_reminders_text = await get_active_reminders_summary(chat_id, user_tz_name) if (chat_id and not disable_tools) else "None"

        system_instruction_text, history = self._map_messages(messages)

        tech_instruction = f"\n\n[REAL-TIME CLOCK] {current_time_str}. Timezone: {user_tz_name}."
        if not disable_tools:
            tech_instruction += f"\nReminders: {active_reminders_text}. Use tools for reminders."

        full_sys_inst = (system_instruction_text or "") + tech_instruction

        prompt_content = "Hello"
        if history and history[-1]['role'] == 'user':
            last_msg = history.pop()
            prompt_content = last_msg['parts'][0]

        # ВАЖЛИВО: tools = None, якщо disable_tools=True
        tools_obj = self._get_tools_proto(settings.get('allow_search', True)) if not disable_tools else None

        model = genai.GenerativeModel(model_name=model_name, system_instruction=full_sys_inst, tools=[tools_obj] if tools_obj else None)
        chat = model.start_chat(history=history)

        keep_generating = True
        current_prompt = prompt_content
        collected_source_urls: List[str] = []

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

                # Логування токенів після завершення потоку
                if response_stream and hasattr(response_stream, 'usage_metadata') and response_stream.usage_metadata:
                    usage = response_stream.usage_metadata
                    logger.info(f"📊 [Gemini] Usage: Prompt={usage.prompt_token_count}, Candidates={usage.candidates_token_count}, Total={usage.total_token_count}")

                if function_call_found:
                    try: await response_stream.resolve()
                    except: pass

                    fn_name = function_call_part.name
                    fn_args = {}
                    if hasattr(function_call_part, "args") and function_call_part.args:
                        fn_args = {k: v for k, v in function_call_part.args.items()}

                    logger.info(f"🤖 Gemini Tool: {fn_name}")

                    tool_result = await execute_tool(
                        fn_name,
                        fn_args,
                        user_id=settings.get('user_id'),
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

                    for link in tool_result.source_urls:
                        if link not in collected_source_urls and len(collected_source_urls) < 5:
                            collected_source_urls.append(link)

                    if not tool_result.stop:
                        keep_generating = True
                        current_prompt = genai.protos.Content(
                            parts=[
                                genai.protos.Part(
                                    function_response=genai.protos.FunctionResponse(
                                        name=fn_name,
                                        response=tool_result.payload
                                    )
                                )
                            ]
                        )
                    else:
                        keep_generating = False

            except Exception as e:
                logger.error(f"Gemini Loop Error: {e}")
                yield f"⚠️ Error: {str(e)}"
                keep_generating = False

        if collected_source_urls:
            yield format_sources_html(collected_source_urls)

    async def transcribe(self, audio_path: str, language: str = None, prompt: str = None, keywords: List[str] = None) -> str:
        try:
            with open(audio_path, "rb") as f:
                data = f.read()
            model = genai.GenerativeModel(self.model_name)
            p = prompt or "Transcribe this audio."
            response = await model.generate_content_async([{'mime_type': 'audio/mp3', 'data': data}, p])
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