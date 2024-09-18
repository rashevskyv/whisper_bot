import requests
import logging
from tokens import WHISPER_API_KEY, GPT_API_KEY
import sseclient
import json
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

def summarize_text(text: str) -> str:
    logger.info(f"Резюме тексту через GPT-4")
    url = "https://api.openai.com/v1/chat/completions"
    headers = {
        'Authorization': f'Bearer {GPT_API_KEY}',
        'Content-Type': 'application/json'
    }
    messages = [
        {'role': 'system', 'content': 'Ви є помічником, який допомагає створювати резюме тексту.'},
        {'role': 'user', 'content': f"Зробіть резюме наступного тексту:\n\n{text}"}
    ]
    
    data = {
        'model': 'gpt-4o',
        'messages': messages,
        'max_tokens': 150,
        'temperature': 0.5
    }
    
    response = requests.post(url, headers=headers, json=data)
    response_data = response.json()
    logger.info(f"Відповідь від GPT-4o API: {response_data}")
    
    if 'choices' in response_data and len(response_data['choices']) > 0:
        return response_data['choices'][0]['message']['content'].strip()
    else:
        return 'Не вдалося створити резюме тексту через GPT-4o.'

def rewrite_text(text: str) -> str:
    logger.info(f"Переписування тексту через GPT-4o")
    url = "https://api.openai.com/v1/chat/completions"
    headers = {
        'Authorization': f'Bearer {GPT_API_KEY}',
        'Content-Type': 'application/json'
    }
    messages = [
        {'role': 'system', 'content': 'Ви є помічником, який допомагає переписувати текст у красивій формі.'},
        {'role': 'user', 'content': f"Перепишіть цей текст у красивій формі:\n\n{text}"}
    ]
    
    data = {
        'model': 'gpt-4o',
        'messages': messages,
        'max_tokens': 1500,
        'temperature': 0.5
    }
    
    response = requests.post(url, headers=headers, json=data)
    response_data = response.json()
    logger.info(f"Відповідь від GPT-4o API: {response_data}")
    
    if 'choices' in response_data and len(response_data['choices']) > 0:
        return response_data['choices'][0]['message']['content'].strip()
    else:
        return 'Не вдалося переписати текст через GPT-4o.'

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

def analyze_content(text: str = None, image_path: str = None):
    logger.info(f"Аналіз контенту: текст {'присутній' if text else 'відсутній'}, зображення {'присутнє' if image_path else 'відсутнє'}")
    url = "https://api.openai.com/v1/chat/completions"
    headers = {
        'Authorization': f'Bearer {GPT_API_KEY}',
        'Content-Type': 'application/json'
    }

    try:
        messages = [{"role": "system", "content": "Ви - помічник, здатний аналізувати текст та зображення."}]

        if text:
            messages.append({"role": "user", "content": text})

        if image_path:
            with open(image_path, "rb") as image_file:
                encoded_image = base64.b64encode(image_file.read()).decode('utf-8')
            
            messages.append({
                "role": "user",
                "content": [
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": f"data:image/jpeg;base64,{encoded_image}"
                        }
                    }
                ]
            })

        if not text and not image_path:
            raise ValueError("Потрібно надати текст або зображення для аналізу")

        data = {
            'model': 'gpt-4o',
            'messages': messages,
            'max_tokens': 1000,
            'stream': True
        }

        response = requests.post(url, headers=headers, json=data, stream=True)
        response.raise_for_status()

        for line in response.iter_lines():
            if line:
                line = line.decode('utf-8')
                if line.startswith('data: '):
                    json_str = line[6:]
                    if json_str != '[DONE]':
                        try:
                            chunk = json.loads(json_str)
                            content = chunk['choices'][0]['delta'].get('content', '')
                            if content:
                                yield content
                        except json.JSONDecodeError:
                            logger.error(f"Помилка декодування JSON: {line}")

    except requests.RequestException as e:
        logger.error(f"Помилка запиту API: {str(e)}")
        yield f"Виникла помилка при аналізі контенту: {str(e)}"
    except Exception as e:
        logger.error(f"Неочікувана помилка: {str(e)}")
        yield f"Виникла неочікувана помилка: {str(e)}"