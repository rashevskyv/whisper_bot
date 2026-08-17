#!/bin/bash
export PYTHONUNBUFFERED=1

# Налаштування
VENV_DIR="venv"
MAIN_FILE="main.py"
USERBOT_FILE="userbot.py"
REQUIREMENTS="requirements.txt"
VENV_PYTHON="$VENV_DIR/bin/python"
VENV_PIP="$VENV_DIR/bin/pip"

SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" &> /dev/null && pwd )"
cd "$SCRIPT_DIR"

# 1. Створення venv (якщо немає)
if [ ! -d "$VENV_DIR" ]; then
    echo "📦 Створення віртуального оточення..."
    if command -v python3.11 &> /dev/null; then
        python3.11 -m venv "$VENV_DIR"
    elif command -v python3.10 &> /dev/null; then
        python3.10 -m venv "$VENV_DIR"
    else
        python3 -m venv "$VENV_DIR"
    fi
fi

# Перевірка pip
if [ ! -f "$VENV_PIP" ]; then
    echo "❌ Помилка: pip не знайдено. Спробуйте видалити папку venv."
    exit 1
fi

# 2. ПРИМУСОВЕ оновлення бібліотек
echo "📥 Встановлення/Оновлення бібліотек..."
"$VENV_PIP" install -r "$REQUIREMENTS"

# 3. Запуск
# Перевірка наявності файлу сесії
if [ ! -f "my_userbot.session" ]; then
    echo "⚠️ УВАГА: Сесія Userbot відсутня!"
    echo "   Запустіть 'venv/bin/python userbot.py' вручну один раз для входу в акаунт."
fi

echo "🚀 Запуск Userbot (в фоні)..."
"$VENV_PYTHON" "$USERBOT_FILE" &
USERBOT_PID=$!

echo "🚀 Запуск Main Bot..."
"$VENV_PYTHON" "$MAIN_FILE"

# Коли головний бот зупиняється (Ctrl+C), вбиваємо і юзербота
kill $USERBOT_PID