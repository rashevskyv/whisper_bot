import requests
import logging
from tokens import WHISPER_API_KEY, GPT_API_KEY

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
        {'role': 'user', 'content': f"Виправте граматичні помилки в цьому тексті, але не змінюйте його зміст:\n\n{text}"}
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
