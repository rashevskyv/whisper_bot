# 🤖 AI Telegram Assistant (Multi-Provider: OpenAI & Gemini)

A powerful, multi-modal Telegram bot designed to be your ultimate assistant. It integrates **OpenAI (GPT-4o)** and **Google (Gemini 1.5)** to process text, voice, video, and images. It features real-time web search, smart summarization, and a flexible settings menu.

## ✨ Key Features

### 🧠 Multi-LLM Support
- **OpenAI:** GPT-4o, GPT-4o-mini, GPT-4-Turbo.
- **Google Gemini:** Gemini 1.5 Flash, Gemini 1.5 Pro.
- **Switchable:** Change models instantly via the settings menu.

### 🌐 Web Search (Live Access)
- **Internet Access:** The bot can search the web (via DuckDuckGo) to answer questions about current events, weather, exchange rates, etc.
- **Transparent:** You see when the bot is searching.

### 🗣 Audio & Video Processing
- **Universal Transcription:** Converts voice messages, video notes (circles), and video files to text using **Whisper**.
- **Smart Summarization:** "Summarize" button turns chaotic voice notes into structured reports.

### 👁 Computer Vision
- **Analyze Anything:** Send photos to get descriptions or extract text (OCR).
- **Dual Engine:** Uses GPT-4o Vision or Gemini Vision depending on your settings.

### 💬 Advanced Chat Logic
- **Streaming:** Real-time typing effect.
- **Group Mode:**
    - Filters spam (responds only to triggers like "bot", "gpt").
    - Silent transcription for voice notes in groups.
- **Personas:** Switch between "Assistant", "Friend", "Editor", "Psychologist", and more.

### 🛡 Security & Settings
- **Custom Keys:** Users can add their own API keys (OpenAI/Google) encrypted in the database.
- **Privacy:** Admin-controlled access and secure storage.

---

## 🛠 Tech Stack

- **Python 3.10+**
- **python-telegram-bot** (Async)
- **OpenAI API** & **Google Generative AI SDK**
- **DuckDuckGo Search**
- **SQLAlchemy + aiosqlite**
- **FFmpeg**

---

## 🚀 Installation

### 1. Prerequisites
```bash
sudo apt update && sudo apt install python3-venv ffmpeg -y
```

### 2. Setup
```bash
git clone https://github.com/your-username/your-repo.git
cd your-repo
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

### 3. Configuration
Create `.env`:
```ini
BOT_TOKEN=123:ABC...
OPENAI_API_KEY=sk-...
GOOGLE_API_KEY=AIza... (Optional, for Gemini)
ENCRYPTION_KEY=... (Generate with cryptography module)
ADMIN_IDS=123,456
```

### 4. Run
```bash
./start.sh
