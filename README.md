Ось оновлений, детальний README.md та команда для коміту, що охоплює весь масштабний рефакторинг та нові фічі.

1. README.md

Створіть файл README.md у корені проекту.

code
Markdown
download
content_copy
expand_more
2. Клонування репозиторію
code
Bash
download
content_copy
expand_less
git clone https://github.com/your-username/whisper-bot.git
cd whisper-bot
3. Налаштування .env

Створіть файл .env і заповніть його:

code
Ini
download
content_copy
expand_less
# Головний бот (від BotFather)
BOT_TOKEN=123456:ABC...
MAIN_BOT_USERNAME=NameOfYourBot

# API Ключі (Системні)
OPENAI_API_KEY=sk-...
GOOGLE_API_KEY=AIza...

# Ключ шифрування БД (Fernet)
# Згенерувати: python3 -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
ENCRYPTION_KEY=...

# Userbot (my.telegram.org)
API_ID=12345
API_HASH=abcdef...

# Адміни (ID через кому)
ADMIN_IDS=12345678,87654321
4. Перший запуск (Авторизація Userbot)

Потрібно один раз авторизувати Userbot для завантаження відео з соцмереж.

code
Bash
download
content_copy
expand_less
# Створіть venv та встановіть залежності
python3.11 -m venv venv
source venv/bin/activate
pip install -r requirements.txt

# Запустіть авторизацію
python userbot.py
# Введіть номер телефону і код. Після успішного входу натисніть Ctrl+C.
5. Запуск Бота

Використовуйте скрипт start.sh, який запустить і основного бота, і юзербота у фоні.

code
Bash
download
content_copy
expand_less
chmod +x start.sh
./start.sh
📂 Структура Проекту
code
Text
download
content_copy
expand_less
.
├── bot/
│   ├── ai/          # Провайдери (OpenAI, Google)
│   ├── database/    # Моделі БД
│   ├── handlers/    # Логіка бота (розбита на модулі: text, media, ai, settings)
│   └── utils/       # Утиліти (downloader, media converter, html cleaner)
├── userbot.py       # Клієнт для скачування медіа (Pyrogram)
├── main.py          # Точка входу
├── config.py        # Промпти та налаштування
└── start.sh         # Скрипт запуску
code
Code
download
content_copy
expand_less
---

### 2. Git Commit

Цей коміт фіксує великий рефакторинг коду (розбиття на модулі), покращення обробки довгих повідомлень та виправлення помилок з HTML.

Виконайте в терміналі:

```bash
git add .
git commit -m "refactor: Split handlers & improve message reliability

- Modularization: Split 'messages.py' into 'text.py', 'media.py', 'ai.py', 'callbacks.py', and 'common.py' for better maintainability.
- Feature (UX): Added 'send_long_message' utility to smartly split long AI responses (>4096 chars) without breaking HTML tags.
- Feature (UX): Implemented 'clean_html' to strip unsupported tags (<html>, <body>) and convert Markdown to HTML.
- Feature (Media): Added 'MEDIA_GROUP_CACHE' to support captions in photo albums.
- Feature (AI): Added 'Beautify' step for transcriptions (AI inserts paragraphs before sending).
- Fix: Solved 'Chat object has no attribute reply_text' error.
- Fix: Improved 'Vision on Reply' logic - replying to a photo with text now triggers analysis.
- Config: Updated system prompts with strict HTML formatting rules."