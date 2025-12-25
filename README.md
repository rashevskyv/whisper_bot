# 🤖 AI Telegram Assistant (Multi-Provider: OpenAI & Gemini)

A powerful, multi-modal Telegram bot designed to be your ultimate personal assistant. It seamlessly integrates **OpenAI (GPT-4o)** and **Google (Gemini 1.5/2.0)** to process text, voice, video, and images.

The bot features **real-time web search**, smart summarization, context memory, and a robust settings system for customizing models and languages.

## ✨ Key Features

### 🧠 Multi-LLM Support
- **OpenAI:** GPT-4o, GPT-4o-mini, GPT-4-Turbo.
- **Google Gemini:** Gemini 1.5 Pro, Gemini 2.0 Flash (Experimental).
- **Flexible Access:** Users can provide their own API keys to unlock advanced models, or use the default system configuration (if allowed).

### 🌐 Live Web Search
- **Internet Access:** The bot can browse the web via DuckDuckGo to find real-time information (news, weather, stock prices).
- **Smart Execution:** Powered by OpenAI Function Calling — the bot decides when to search based on your query.
- **Citations:** Provides answers with links to sources.

### 🗣 Audio & Video Intelligence
- **Universal Transcription:** Automatically converts voice messages, video notes (circles), and video files to text using **Whisper**.
- **Smart Summarization:** Includes a "Summarize" button that transforms long, chaotic audio into structured bullet points using a specialized analyst persona.
- **Language Aware:** Transcription automatically adapts to the user's selected language settings.

### 👁 Computer Vision
- **Image Analysis:** Send any photo to the bot.
- **Interactive Menu:** Choose between **"Describe"** (get a detailed description) or **"OCR / Text"** (extract text from the image).
- **Dual Engine:** Uses GPT-4o Vision or Gemini Vision depending on your active model.

### 💬 Advanced Chat Logic
- **Streaming Responses:** Replies are typed out in real-time.
- **Smart Group Mode:**
    - **Passive:** Ignores general chatter to avoid spam.
    - **Reactive:** Responds only to triggers (`bot`, `gpt`, `settings`), mentions (`@botname`), or replies.
    - **Silent Transcription:** Automatically transcribes voice notes in groups without notifying everyone.
- **Personas:** Switch between different personalities: "Assistant", "Friend", "Editor", "Psychologist", "Coder".

### ⚙️ Settings & Security
- **Unified Menu:** Access settings via the `/start` command or by typing "menu"/"settings".
- **Language Switching:** Change the bot's language (UK/EN/RU) on the fly.
- **Encrypted Storage:** User API keys are stored in the database using **Fernet (symmetric encryption)**.
- **Robustness:** Optimized for Linux/WSL environments with custom timeout handling.

---

## 🛠 Tech Stack

- **Python 3.11+**
- **python-telegram-bot** (v21+ Async)
- **OpenAI API** & **Google GenAI SDK**
- **DuckDuckGo Search (ddgs)**
- **SQLAlchemy + aiosqlite** (Async Database)
- **FFmpeg** (Media processing)
- **Cryptography** (Data security)

---

## 🚀 Installation & Setup

### 1. Prerequisites
Ensure you have **Python 3.10+** (3.11 recommended) and **FFmpeg** installed.

```bash
# Ubuntu/Debian
sudo apt update && sudo apt install ffmpeg -y
```

### 2. Clone the Repository
```bash
git clone https://github.com/your-username/whisper-bot.git
cd whisper-bot
```

### 3. Environment Setup
We use a helper script `start.sh` that handles virtual environment creation and dependency installation automatically.

1.  Create a `.env` file:
    ```bash
    nano .env
    ```

2.  Paste the configuration:
    ```ini
    # Telegram Bot Token (from @BotFather)
    BOT_TOKEN=your_telegram_bot_token
    MAIN_BOT_USERNAME=NameOfYourBot

    # System API Keys (Optional fallbacks)
    OPENAI_API_KEY=sk-...
    GOOGLE_API_KEY=AIza...

    # Encryption Key for Database
    # Run: python3 -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
    ENCRYPTION_KEY=your_generated_key

    # Userbot (my.telegram.org)
    API_ID=12345
    API_HASH=abcdef...

    # Admin IDs (comma separated)
    ADMIN_IDS=12345678,87654321
    ```

### 4. Run
```bash
chmod +x start.sh
./start.sh
```

---

## 📅 Roadmap (TODO)

- [ ] **Smart Reminders & Scheduler:**
    - [ ] **Natural Language Triggers:** Create reminders via chat (e.g., "Remind me in 2 hours", "Drink water 5 times a day").
    - [ ] **Backend Scheduler:** Robust task queue to handle one-time and recurring events (counting repetitions).
    - [ ] **Context Awareness:** AI should suggest reminder times based on context (e.g., "Interview at 5 PM" -> "Should I remind you 30 mins before?").
    - [ ] **Management UI:** Menu to view and delete active reminders.
- [ ] **Userbot Improvements:**
    - [ ] Add support for more platforms.
    - [ ] Better error handling for restricted content.

---

## 📝 License

This project is open-source and available under the MIT License.
