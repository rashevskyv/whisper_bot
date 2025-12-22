import base64
import json
import logging
from typing import AsyncGenerator, List, Dict, Any
from openai import AsyncOpenAI, APIError
from bot.ai.base import LLMProvider
from bot.utils.search import perform_search

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

        tools = []
        if allow_search:
            tools.append({
                "type": "function",
                "function": {
                    "name": "web_search",
                    "description": "Find real-time info (news, weather, exchange rates). Use for ANY fact you don't know.",
                    "parameters": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]}
                }
            })
        
        tools.append({
            "type": "function",
            "function": {
                "name": "set_language",
                "description": "Switch bot language permanently.",
                "parameters": {"type": "object", "properties": {"lang_code": {"type": "string", "description": "ISO code (uk, en, ru)"}}, "required": ["lang_code"]}
            }
        })

        # Inject system language instruction
        # Робимо копію messages, щоб не змінювати оригінальний список, який може перевикористовуватися
        local_messages = [msg.copy() for msg in messages]
        
        sys_idx = next((i for i, m in enumerate(local_messages) if m['role'] == 'system'), None)
        instr = f"\n\nCURRENT LANGUAGE: '{current_lang}'. Answer primarily in this language."
        if sys_idx is not None: local_messages[sys_idx]['content'] += instr
        else: local_messages.insert(0, {"role": "system", "content": instr})

        try:
            stream = await self.client.chat.completions.create(
                model=model, messages=local_messages, temperature=temp, tools=tools if tools else None, stream=True
            )

            tool_calls_buffer = {} # Використовуємо dict для збору шматків по індексу
            is_tool_call = False
            
            async for chunk in stream:
                delta = chunk.choices[0].delta
                
                if delta.tool_calls:
                    is_tool_call = True
                    for tc in delta.tool_calls:
                        idx = tc.index
                        if idx not in tool_calls_buffer:
                            tool_calls_buffer[idx] = {"id": tc.id, "name": "", "arguments": ""}
                        
                        if tc.id: tool_calls_buffer[idx]["id"] = tc.id
                        if tc.function.name: tool_calls_buffer[idx]["name"] += tc.function.name
                        if tc.function.arguments: tool_calls_buffer[idx]["arguments"] += tc.function.arguments
                
                if delta.content and not is_tool_call:
                    yield delta.content

            if is_tool_call:
                # Сортуємо по індексу і перетворюємо в список для повідомлення
                tool_calls_list = [tool_calls_buffer[i] for i in sorted(tool_calls_buffer.keys())]
                
                # Додаємо повідомлення асистента з викликами
                assistant_msg_tool_calls = [
                    {
                        "id": tc["id"], 
                        "type": "function", 
                        "function": {"name": tc["name"], "arguments": tc["arguments"]}
                    } for tc in tool_calls_list
                ]
                
                local_messages.append({
                    "role": "assistant",
                    "tool_calls": assistant_msg_tool_calls
                })

                # Виконуємо ВСІ виклики і додаємо результати
                for tc in tool_calls_list:
                    name = tc["name"]
                    try: args = json.loads(tc["arguments"])
                    except: args = {}
                    
                    content = ""
                    if name == "web_search":
                        yield "\n\n🔎 <i>Шукаю в інтернеті...</i>\n"
                        content = await perform_search(args.get("query", ""))
                    
                    elif name == "set_language":
                        lang = args.get("lang_code", "uk")
                        yield f"__SET_LANGUAGE:{lang}__"
                        content = f"Language switched to '{lang}'."
                        # Оновлюємо системну інструкцію для фінальної відповіді
                        local_messages.append({"role": "system", "content": f"SYSTEM UPDATE: Language is now '{lang}'."})

                    # Важливо: tool_call_id має співпадати
                    local_messages.append({
                        "role": "tool",
                        "tool_call_id": tc["id"],
                        "content": str(content)
                    })

                # Другий прохід
                final_stream = await self.client.chat.completions.create(
                    model=model, messages=local_messages, stream=True
                )
                async for chunk in final_stream:
                    if chunk.choices[0].delta.content:
                        yield chunk.choices[0].delta.content

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