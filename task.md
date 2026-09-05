# Завдання розробки асистента

- [x] Використовувати `gpt-transcribe` як єдину модель транскрибації OpenAI.
- [x] Відновити транскрибацію для голосових повідомлень, відеонотаток (кружечків) та відеофайлів без обов'язкового системного FFmpeg у `PATH`.
- [x] Забезпечити обмеження видобутого аудіо в межах 25 МБ для OpenAI API.
- [x] Повертати користувачам чітке повідомлення про помилку транскрибації замість передачі тексту помилки на обробку.
- [x] Додати контекстні підказки мови та словник специфічних термінів (`/terms`).
- [x] Додати кероване утримання контексту (30 днів), режими приватності груп та явну пам'ять користувача (`/remember`, `/memories`, `/forget`).
- [x] Гарантувати додавання посилань на джерела після веб-пошуку та застосувати добовий ліміт транскрибації.
- [x] Виправити несумісність версій `aiohttp` та `openai` (`SocketTimeoutError` в `requirements.txt`).
- [x] Зафіксувати відповіді моделі на `gpt-4o-mini` без небажаного автоматичного перемикання на `gpt-4o`.
- [x] Додати керування та очищення черги завдань Userbot (`DownloadQueue`): кнопка в налаштуваннях `queue_menu`, команди `/queue`, `/queue clear`.
- [x] Реалізувати захист від FloodWait у `userbot.py` для запобігання перевантаженню Telegram API.
- [x] Виправити помилку транскрибації голосових повідомлень Telegram (`Unsupported file format oga`): авто-конвертація розширення `.oga` -> `.ogg` для OpenAI API.
- [x] Вимкнути зайве отримання оновлень груп у Userbot (`no_updates=True`) для усунення помилки `Peer id invalid: -100...` та налаштувати негайний вивід логів (`PYTHONUNBUFFERED=1`).
- [x] Інтегрувати провайдер **OpenRouter** (`OpenRouterProvider`) для розмовних моделей нового покоління (GPT-5.6 Luna, DeepSeek V4 Flash, Gemini 3.7 Flash, Gemini 3.5 Lite, Qwen 3.7 Flash, Mistral Small 3).
- [x] Додати підтримку ключів OpenRouter у налаштуваннях (`keys_menu`, `save_key`) та оновити меню вибору моделей (`model_menu`).
- [x] Покрити функціонал OpenRouter модульними тестами (`test_openrouter.py`), оновити документацію та граф знань Graphify.
- [x] Додати налаштування вимкнення автоматичного репосту відео (`video_repost`) для окремих чатів та за замовчуванням для груп (`ENABLE_VIDEO_REPOST_GROUPS` у `config.py`).
- [x] Додати кнопку перемикання «🎥 Репост відео: ✅/❌» у меню налаштувань `settings_menu` (`toggle_video_repost`).
- [x] Реалізувати команду `/video` (`/video on`, `/video off`, `/video status`, `/video all off` для адміністраторів бота).
- [x] Покрити функціонал перемикання репосту відео модульними тестами (`test_video_repost.py`).

## Завершена задача — контекстні дії та розклади v2.5.0

- [x] Гарантувати, що голосове повідомлення лише транскрибується і не виконує tools без явної кнопки.
- [x] Прив'язати callback до конкретної транскрипції, а не до останнього запису в чаті.
- [x] Винести наявні AI tools у спільний валідований executor і вирівняти OpenAI/OpenRouter/Google.
- [x] Додати персистентну модель `ActionDraft` і lifecycle з expiry, ownership та однією активною чернеткою на user/chat.
- [x] Перехоплювати AI mutating tools у `ActionDraft` без side effect і повертати прев'ю або одне уточнення.
- [x] Додати confirm/cancel callback-и з ownership та виконанням лише для переможця confirm.
- [x] Маршрутизувати текстову відповідь або явну транскрипцію в активну `awaiting_info` чернетку.
- [x] Додати повторювані задачі й occurrences для медикаментів/інших розкладів з done/skip/snooze:
  - [x] D1 — таблиці та чистий DB-lifecycle без AI, Telegram і APScheduler;
  - [x] D2 — планування, доставка, missed policy та restore після restart;
  - [x] D3 — ActionDraft для medication/generic schedule з уточненням і прев'ю;
  - [x] D4 — callback-и `done`/`skip`/`snooze` та персистентний snooze.
- [x] Додати іменовані списки та пункти списків; реалізувати контекст «Покупки»:
  - [x] E1 — таблиці та чистий DB-lifecycle з детермінованим resolve й атомарними змінами;
  - [x] E2 — shared AI actions і `ActionDraft` для mutating list actions;
  - [x] E3 — Telegram callback-и пунктів та наскрізні shopping-flow тести.
- [x] Покрити voice safety, provider parity, draft lifecycle, scheduler restore/timezone та list isolation тестами.
- [x] Після приймання оновити README/walkthrough/Graphify, підняти `APP_VERSION` до `2.5.0` і створити focused commit.
