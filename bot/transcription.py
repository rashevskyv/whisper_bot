import requests
import logging
from tokens import WHISPER_API_KEY, GPT_API_KEY
import json
import sseclient
import re
import base64

logger = logging.getLogger(__name__)

def transcribe_audio(file_path: str, language: str) -> str:
    logger.info(f"Розшифровка аудіо з файлу: {file_path} мовою: {language}")
    url = "https://api.openai.com/v1/audio/transcriptions"
    headers = {
        'Authorization': f'Bearer {WHISPER_API_KEY}'
    }
    with open(file_path, 'rb') as audio_file:
        files = {
            'file': audio_file,
            'model': (None, 'whisper-1'),
            'language': (None, language)  # Вибір мови
        }
        response = requests.post(url, headers=headers, files=files)
        response_data = response.json()
        logger.info(f"Відповідь від Whisper API: {response_data}")
        return response_data.get('text', 'Не вдалося розшифрувати аудіо.')

def postprocess_text(text: str) -> str:
    logger.info(f"Постобробка тексту через GPT-3.5 Turbo")
    url = "https://api.openai.com/v1/chat/completions"
    headers = {
        'Authorization': f'Bearer {GPT_API_KEY}',
        'Content-Type': 'application/json'
    }
    messages = [
        {'role': 'system', 'content': 'Ви є помічником, який допомагає виправляти текст.'},
        {'role': 'user', 'content': f"Виправте граматичні помилки в цьому тексті, але не змінюйте його зміст. Починай текст одразу:\n\n{text}"}
    ]
    
    data = {
        'model': 'gpt-3.5-turbo',
        'messages': messages,
        'max_tokens': 1500,
        'temperature': 0.5
    }
    
    response = requests.post(url, headers=headers, json=data)
    response_data = response.json()
    logger.info(f"Відповідь від GPT-3.5 Turbo API: {response_data}")
    
    if 'choices' in response_data and len(response_data['choices']) > 0:
        return response_data['choices'][0]['message']['content'].strip()
    else:
        return 'Не вдалося обробити текст через GPT-3.5 Turbo.'

def summarize_text(text: str):
    logger.info(f"Резюмування тексту через GPT-4 зі стрімінгом")
    url = "https://api.openai.com/v1/chat/completions"
    headers = {
        'Authorization': f'Bearer {GPT_API_KEY}',
        'Content-Type': 'application/json'
    }
    messages = [
        {'role': 'system', 'content': 'Ви є помічником, який допомагає створювати резюме тексту.'},
        {'role': 'user', 'content': f"Зробіть стисле резюме наступного тексту:\n\n{text}"}
    ]
    
    data = {
        'model': 'gpt-4',
        'messages': messages,
        'max_tokens': 150,
        'temperature': 0.7,
        'stream': True
    }
    
    try:
        response = requests.post(url, headers=headers, json=data, stream=True)
        client = sseclient.SSEClient(response)
        
        for event in client.events():
            if event.data != '[DONE]':
                try:
                    chunk = json.loads(event.data)
                    content = chunk['choices'][0]['delta'].get('content', '')
                    if content:
                        yield content
                except json.JSONDecodeError:
                    logger.error(f"Помилка декодування JSON: {event.data}")
            else:
                break
    except Exception as e:
        logger.error(f"Помилка при резюмуванні тексту: {e}")
        yield f"Виникла помилка при резюмуванні тексту: {str(e)}"

def rewrite_text(text: str):
    logger.info(f"Переписування тексту через GPT-4 зі стрімінгом")
    url = "https://api.openai.com/v1/chat/completions"
    headers = {
        'Authorization': f'Bearer {GPT_API_KEY}',
        'Content-Type': 'application/json'
    }
    messages = [
        {'role': 'system', 'content': 'Ви є помічником, який допомагає переписувати текст у іншому стилі.'},
        {'role': 'user', 'content': f"Перепишіть цей текст, зберігаючи основний зміст, але змінюючи стиль та формулювання:\n\n{text}"}
    ]
    
    data = {
        'model': 'gpt-4',
        'messages': messages,
        'max_tokens': 1500,
        'temperature': 0.7,
        'stream': True
    }
    
    try:
        response = requests.post(url, headers=headers, json=data, stream=True)
        client = sseclient.SSEClient(response)
        
        for event in client.events():
            if event.data != '[DONE]':
                try:
                    chunk = json.loads(event.data)
                    content = chunk['choices'][0]['delta'].get('content', '')
                    if content:
                        yield content
                except json.JSONDecodeError:
                    logger.error(f"Помилка декодування JSON: {event.data}")
            else:
                break
    except Exception as e:
        logger.error(f"Помилка при переписуванні тексту: {e}")
        yield f"Виникла помилка при переписуванні тексту: {str(e)}"
    
def query_gpt4(text: str) -> str:
    logger.info(f"Запит до GPT-4")
    url = "https://api.openai.com/v1/chat/completions"
    headers = {
        'Authorization': f'Bearer {GPT_API_KEY}',
        'Content-Type': 'application/json'
    }
    messages = [
        {'role': 'system', 'content': 'Ви є корисним асистентом.'},
        {'role': 'user', 'content': text}
    ]
    
    data = {
        'model': 'gpt-4',
        'messages': messages,
        'max_tokens': 1500,
        'temperature': 0.7
    }
    
    response = requests.post(url, headers=headers, json=data)
    response_data = response.json()
    logger.info(f"Відповідь від GPT-4 API: {response_data}")
    
    if 'choices' in response_data and len(response_data['choices']) > 0:
        return response_data['choices'][0]['message']['content'].strip()
    else:
        return 'Не вдалося отримати відповідь від GPT-4.'

def query_claude(text: str) -> str:
    logger.info(f"Запит до Claude (заглушка)")
    # Це заглушка, яку ми замінимо справжньою реалізацією пізніше
    return "Це заглушка для відповіді від Claude. Справжня реалізація буде додана пізніше."

def query_gpt4_stream(text: str):
    logger.info(f"Запит до GPT-4 зі стрімінгом")
    url = "https://api.openai.com/v1/chat/completions"
    headers = {
        'Authorization': f'Bearer {GPT_API_KEY}',
        'Content-Type': 'application/json'
    }

    # Розділяємо цитоване повідомлення та запит користувача
    quoted_message = re.search(r'<quoted_message>(.*?)</quoted_message>', text, re.DOTALL)
    if quoted_message:
        quoted_text = quoted_message.group(1)
        user_query = text.replace(quoted_message.group(0), '').strip()
        messages = [
            {'role': 'system', 'content': 'Ви є корисним асистентом.'},
            {'role': 'user', 'content': f"Цитоване повідомлення: {quoted_text}"},
            {'role': 'user', 'content': f"Мій запит: {user_query}"}
        ]
    else:
        messages = [
            {'role': 'system', 'content': 'Ви є корисним асистентом.'},
            {'role': 'user', 'content': text}
        ]
    
    data = {
        'model': 'gpt-4o',
        'messages': messages,
        'max_tokens': 1500,
        'temperature': 0.7,
        'stream': True
    }
    
    response = requests.post(url, headers=headers, json=data, stream=True)
    client = sseclient.SSEClient(response)
    
    for event in client.events():
        if event.data != '[DONE]':
            try:
                chunk = json.loads(event.data)
                content = chunk['choices'][0]['delta'].get('content', '')
                if content:
                    yield content
            except json.JSONDecodeError:
                logger.error(f"Помилка декодування JSON: {event.data}")
        else:
            break

def query_claude_stream(text: str):
    logger.info(f"Запит до Claude зі стрімінгом (заглушка)")
    # Це заглушка, яку ми замінимо справжньою реалізацією пізніше
    yield "Це заглушка для відповіді від Claude зі стрімінгом. "
    yield "Справжня реалізація буде додана пізніше."

def analyze_image(image_path: str, prompt: str):
    logger.info(f"Аналіз зображення: {image_path}")
    url = "https://api.openai.com/v1/chat/completions"
    headers = {
        'Authorization': f'Bearer {GPT_API_KEY}',
        'Content-Type': 'application/json'
    }

    try:
        # Кодуємо зображення в base64
        with open(image_path, "rb") as image_file:
            encoded_image = base64.b64encode(image_file.read()).decode('utf-8')

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
            'model': 'gpt-4o',
            'messages': messages,
            'max_tokens': 1000,
        }

        response = requests.post(url, headers=headers, json=data)
        
        if response.status_code != 200:
            logger.error(f"Помилка API: {response.status_code} - {response.text}")
            yield f"Помилка API: {response.status_code} - {response.text}"
            return

        response_data = response.json()
        
        if 'choices' in response_data and len(response_data['choices']) > 0:
            content = response_data['choices'][0]['message']['content']
            yield content
        else:
            logger.error(f"Неочікувана відповідь API: {response_data}")
            yield "Отримано неочікувану відповідь від API. Спробуйте ще раз."

    except Exception as e:
        logger.error(f"Помилка при аналізі зображення: {str(e)}")
        yield f"Виникла помилка при аналізі зображення: {str(e)}"

def analyze_content(text: str = None, image_path: str = None, conversation_context: list = None):
    logger.info(f"Аналіз контенту: текст {'присутній' if text else 'відсутній'}, зображення {'присутнє' if image_path else 'відсутнє'}, контекст {'присутній' if conversation_context else 'відсутній'}")
    url = "https://api.openai.com/v1/chat/completions"
    headers = {
        'Authorization': f'Bearer {GPT_API_KEY}',
        'Content-Type': 'application/json'
    }

    try:
        messages = [{"role": "system", "content": "Ви - помічник, здатний аналізувати текст та зображення."}]

        if conversation_context:
            messages.extend(conversation_context)

        if text:
            messages.append({"role": "user", "content": text})

        if image_path:
            with open(image_path, "rb") as image_file:
                encoded_image = base64.b64encode(image_file.read()).decode('utf-8')
            
            image_message = {
                "role": "user",
                "content": [
                    {"type": "text", "text": "Аналіз цього зображення:"},
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
            'model': 'gpt-4o',  # Використовуємо актуальну модель для аналізу зображень
            'messages': messages,
            'max_tokens': 1000,
        }

        logger.debug(f"Відправка запиту до OpenAI API. Заголовки: {headers}, Дані: {data}")

        response = requests.post(url, headers=headers, json=data)
        response.raise_for_status()

        response_data = response.json()
        if 'choices' in response_data and len(response_data['choices']) > 0:
            content = response_data['choices'][0]['message']['content']
            yield content
        else:
            logger.error(f"Неочікувана відповідь API: {response_data}")
            yield "Отримано неочікувану відповідь від API. Спробуйте ще раз."

    except requests.exceptions.HTTPError as e:
        if e.response.status_code == 404:
            logger.error(f"Помилка 404: Модель не знайдена. {e.response.text}")
            yield "Вибачте, виникла проблема з моделлю AI. Будь ласка, спробуйте пізніше."
        else:
            logger.error(f"Помилка запиту API: {str(e)}")
            logger.error(f"Відповідь сервера: {e.response.text if hasattr(e, 'response') else 'Недоступно'}")
            yield f"Виникла помилка при аналізі контенту: {str(e)}"
    except Exception as e:
        logger.error(f"Неочікувана помилка: {str(e)}")
        yield f"Виникла неочікувана помилка: {str(e)}"