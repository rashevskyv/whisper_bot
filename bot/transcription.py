import sys
import os

# Додаємо батьківську директорію до шляху пошуку модулів
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config import GPT_SETTINGS, MESSAGES

# Решта імпортів та коду залишається без змін
import requests
import logging
from tokens import WHISPER_API_KEY, GPT_API_KEY
import json
import sseclient
import re
import base64
import aiohttp
import asyncio

logger = logging.getLogger(__name__)

async def transcribe_audio(file_path: str, language: str):
    logger.info(f"Початок розшифровки аудіо з файлу: {file_path} мовою: {language}")
    url = "https://api.openai.com/v1/audio/transcriptions"
    headers = {
        'Authorization': f'Bearer {WHISPER_API_KEY}'
    }
    try:
        async with aiohttp.ClientSession() as session:
            form = aiohttp.FormData()
            form.add_field('file', open(file_path, 'rb'), filename=os.path.basename(file_path))
            form.add_field('model', 'whisper-1')
            form.add_field('language', language)

            logger.info("Відправка запиту на транскрибацію до API")
            async with session.post(url, headers=headers, data=form) as response:
                response.raise_for_status()
                logger.info("Отримано відповідь від API")
                response_json = await response.json()
                if 'text' in response_json:
                    logger.info("Успішно отримано текст транскрипції")
                    yield response_json['text']
                else:
                    logger.error(f"Неочікувана структура відповіді: {response_json}")
                    yield f"Помилка: неочікувана структура відповіді від API. Відповідь: {response_json}"
    except aiohttp.ClientError as e:
        logger.error(f"Помилка запиту: {str(e)}")
        yield f"Помилка запиту до API: {str(e)}"
    except json.JSONDecodeError:
        logger.error(f"Не вдалося розпарсити JSON. Відповідь: {await response.text()}")
        yield f"Помилка: не вдалося розпарсити відповідь від API. Відповідь: {(await response.text())[:100]}..."
    except Exception as e:
        logger.error(f"Виникла помилка при транскрибації: {str(e)}")
        yield f"Неочікувана помилка при транскрибації: {str(e)}"

async def postprocess_text(text: str) -> str:
    logger.info(f"Постобробка тексту через GPT")
    url = "https://api.openai.com/v1/chat/completions"
    headers = {
        'Authorization': f'Bearer {GPT_API_KEY}',
        'Content-Type': 'application/json'
    }
    messages = [
        {'role': 'system', 'content': MESSAGES['postprocess']['system']},
        {'role': 'user', 'content': MESSAGES['postprocess']['user'].format(text=text)}
    ]
    
    data = {
        'model': GPT_SETTINGS['postprocess']['model'],
        'messages': messages,
        'max_tokens': GPT_SETTINGS['postprocess']['max_tokens'],
        'temperature': GPT_SETTINGS['postprocess']['temperature']
    }
    
    async with aiohttp.ClientSession() as session:
        async with session.post(url, headers=headers, json=data) as response:
            response_data = await response.json()
    
    logger.info(f"Відповідь від GPT API: {response_data}")
    
    if 'choices' in response_data and len(response_data['choices']) > 0:
        return response_data['choices'][0]['message']['content'].strip()
    else:
        return 'Не вдалося обробити текст через GPT.'
    
async def summarize_text(text: str):
    logger.info(f"Резюмування тексту через GPT зі стрімінгом")
    url = "https://api.openai.com/v1/chat/completions"
    headers = {
        'Authorization': f'Bearer {GPT_API_KEY}',
        'Content-Type': 'application/json'
    }
    messages = [
        {'role': 'system', 'content': MESSAGES['summarize']['system']},
        {'role': 'user', 'content': MESSAGES['summarize']['user'].format(text=text)}
    ]
    
    data = {
        'model': GPT_SETTINGS['summarize']['model'],
        'messages': messages,
        'max_tokens': GPT_SETTINGS['summarize']['max_tokens'],
        'temperature': GPT_SETTINGS['summarize']['temperature'],
        'stream': True
    }
    
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(url, headers=headers, json=data) as response:
                async for line in response.content:
                    if line:
                        try:
                            json_response = json.loads(line.decode('utf-8').split('data: ')[1])
                            content = json_response['choices'][0]['delta'].get('content', '')
                            if content:
                                yield content
                        except json.JSONDecodeError:
                            continue
                        except Exception as e:
                            logger.error(f"Помилка при обробці відповіді: {e}")
    except Exception as e:
        logger.error(f"Помилка при резюмуванні тексту: {e}")
        yield f"Виникла помилка при резюмуванні тексту: {str(e)}"

async def rewrite_text(text: str):
    logger.info(f"Переписування тексту через GPT зі стрімінгом")
    url = "https://api.openai.com/v1/chat/completions"
    headers = {
        'Authorization': f'Bearer {GPT_API_KEY}',
        'Content-Type': 'application/json'
    }
    messages = [
        {'role': 'system', 'content': MESSAGES['rewrite']['system']},
        {'role': 'user', 'content': MESSAGES['rewrite']['user'].format(text=text)}
    ]
    
    data = {
        'model': GPT_SETTINGS['rewrite']['model'],
        'messages': messages,
        'max_tokens': GPT_SETTINGS['rewrite']['max_tokens'],
        'temperature': GPT_SETTINGS['rewrite']['temperature'],
        'stream': True
    }
    
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(url, headers=headers, json=data) as response:
                async for line in response.content:
                    if line:
                        try:
                            json_response = json.loads(line.decode('utf-8').split('data: ')[1])
                            content = json_response['choices'][0]['delta'].get('content', '')
                            if content:
                                yield content
                        except json.JSONDecodeError:
                            continue
                        except Exception as e:
                            logger.error(f"Помилка при обробці відповіді: {e}")
    except Exception as e:
        logger.error(f"Помилка при переписуванні тексту: {e}")
        yield f"Виникла помилка при переписуванні тексту: {str(e)}"
        
def query_gpt4(text: str) -> str:
    logger.info(f"Запит до GPT")
    url = "https://api.openai.com/v1/chat/completions"
    headers = {
        'Authorization': f'Bearer {GPT_API_KEY}',
        'Content-Type': 'application/json'
    }
    messages = [
        {'role': 'system', 'content': MESSAGES['query']['system']},
        {'role': 'user', 'content': MESSAGES['query']['user'].format(text=text)}
    ]
    
    data = {
        'model': GPT_SETTINGS['query']['model'],
        'messages': messages,
        'max_tokens': GPT_SETTINGS['query']['max_tokens'],
        'temperature': GPT_SETTINGS['query']['temperature']
    }
    
    response = requests.post(url, headers=headers, json=data)
    response_data = response.json()
    logger.info(f"Відповідь від GPT API: {response_data}")
    
    if 'choices' in response_data and len(response_data['choices']) > 0:
        return response_data['choices'][0]['message']['content'].strip()
    else:
        return 'Не вдалося отримати відповідь від GPT.'

async def query_gpt4_stream(text: str):
    logger.info(f"Запит до GPT зі стрімінгом")
    url = "https://api.openai.com/v1/chat/completions"
    headers = {
        'Authorization': f'Bearer {GPT_API_KEY}',
        'Content-Type': 'application/json'
    }

    quoted_message = re.search(r'<quoted_message>(.*?)</quoted_message>', text, re.DOTALL)
    if quoted_message:
        quoted_text = quoted_message.group(1)
        user_query = text.replace(quoted_message.group(0), '').strip()
        messages = [
            {'role': 'system', 'content': MESSAGES['query']['system']},
            {'role': 'user', 'content': f"Цитоване повідомлення: {quoted_text}"},
            {'role': 'user', 'content': f"Мій запит: {user_query}"}
        ]
    else:
        messages = [
            {'role': 'system', 'content': MESSAGES['query']['system']},
            {'role': 'user', 'content': MESSAGES['query']['user'].format(text=text)}
        ]
    
    data = {
        'model': GPT_SETTINGS['query']['model'],
        'messages': messages,
        'max_tokens': GPT_SETTINGS['query']['max_tokens'],
        'temperature': GPT_SETTINGS['query']['temperature'],
        'stream': True
    }
    
    async with aiohttp.ClientSession() as session:
        async with session.post(url, headers=headers, json=data) as response:
            async for line in response.content:
                if line:
                    try:
                        json_response = json.loads(line.decode('utf-8').split('data: ')[1])
                        content = json_response['choices'][0]['delta'].get('content', '')
                        if content:
                            yield content
                    except json.JSONDecodeError:
                        continue
                    except Exception as e:
                        logger.error(f"Помилка при обробці відповіді: {e}")

def analyze_image(image_path: str, prompt: str):
    logger.info(f"Аналіз зображення: {image_path}")
    url = "https://api.openai.com/v1/chat/completions"
    headers = {
        'Authorization': f'Bearer {GPT_API_KEY}',
        'Content-Type': 'application/json'
    }

    try:
        logger.debug(f"Спроба відкриття файлу зображення: {image_path}")
        with open(image_path, "rb") as image_file:
            encoded_image = base64.b64encode(image_file.read()).decode('utf-8')
        logger.debug(f"Зображення успішно закодовано. Розмір: {len(encoded_image)}")

        messages = [
            {
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        "text": prompt
                    },
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": f"data:image/jpeg;base64,{encoded_image}"
                        }
                    }
                ]
            }
        ]

        data = {
            'model': GPT_SETTINGS['analyze_content']['model'],
            'messages': messages,
            'max_tokens': GPT_SETTINGS['analyze_content']['max_tokens'],
            'temperature': GPT_SETTINGS['analyze_content']['temperature']
        }

        logger.debug("Відправка запиту до API OpenAI")
        response = requests.post(url, headers=headers, json=data)
        
        if response.status_code != 200:
            logger.error(f"Помилка API: {response.status_code} - {response.text}")
            yield f"Помилка API: {response.status_code} - {response.text}"
            return

        response_data = response.json()
        logger.debug(f"Отримано відповідь від API: {response_data}")
        
        if 'choices' in response_data and len(response_data['choices']) > 0:
            content = response_data['choices'][0]['message']['content']
            logger.info("Успішно отримано аналіз зображення")
            yield content
        else:
            logger.error(f"Неочікувана відповідь API: {response_data}")
            yield "Отримано неочікувану відповідь від API. Спробуйте ще раз."

    except FileNotFoundError:
        logger.error(f"Файл зображення не знайдено: {image_path}")
        yield f"Помилка: файл зображення не знайдено: {image_path}"
    except Exception as e:
        logger.error(f"Помилка при аналізі зображення: {str(e)}", exc_info=True)
        yield f"Виникла помилка при аналізі зображення: {str(e)}"

async def analyze_content(text: str = None, image_path: str = None, conversation_context: list = None):
    logger.info(f"Аналіз контенту: текст {'присутній' if text else 'відсутній'}, зображення {'присутнє' if image_path else 'відсутнє'}, контекст {'присутній' if conversation_context else 'відсутній'}")
    url = "https://api.openai.com/v1/chat/completions"
    headers = {
        'Authorization': f'Bearer {GPT_API_KEY}',
        'Content-Type': 'application/json'
    }

    try:
        messages = [{"role": "system", "content": MESSAGES['analyze_content']['system']}]

        if conversation_context:
            messages.extend(conversation_context)

        if text:
            messages.append({"role": "user", "content": MESSAGES['analyze_content']['user_text'].format(text=text)})

        if image_path:
            with open(image_path, "rb") as image_file:
                encoded_image = base64.b64encode(image_file.read()).decode('utf-8')
            
            image_message = {
                "role": "user",
                "content": [
                    {"type": "text", "text": MESSAGES['analyze_content']['user_image']},
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": f"data:image/jpeg;base64,{encoded_image}"
                        }
                    }
                ]
            }
            messages.append(image_message)

        if not text and not image_path and not conversation_context:
            raise ValueError("Потрібно надати текст, зображення або контекст для аналізу")

        data = {
            'model': GPT_SETTINGS['analyze_content']['model'],
            'messages': messages,
            'max_tokens': GPT_SETTINGS['analyze_content']['max_tokens'],
            'temperature': GPT_SETTINGS['analyze_content']['temperature'],
            'stream': True
        }

        async with aiohttp.ClientSession() as session:
            async with session.post(url, headers=headers, json=data) as response:
                async for line in response.content:
                    if line:
                        try:
                            json_response = json.loads(line.decode('utf-8').split('data: ')[1])
                            content = json_response['choices'][0]['delta'].get('content', '')
                            if content:
                                yield content
                        except json.JSONDecodeError:
                            continue
                        except Exception as e:
                            logger.error(f"Помилка при обробці відповіді: {e}")

    except Exception as e:
        logger.error(f"Помилка при аналізі контенту: {str(e)}")
        yield f"Виникла помилка при аналізі контенту: {str(e)}"

# Додаткові допоміжні функції можуть бути додані тут за потреби

if __name__ == "__main__":
    # Тут можна додати код для тестування функцій модуля
    pass