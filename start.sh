#!/bin/bash

# Налаштування
VENV_DIR="venv"
MAIN_FILE="main.py"
REQUIREMENTS="requirements.txt"

# Шляхи до виконавчих файлів всередині venv
# Це гарантує, що ми використовуємо саме ізольований Python 3
VENV_PYTHON="$VENV_DIR/bin/python"
VENV_PIP="$VENV_DIR/bin/pip"

# Переходимо в папку скрипта
SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" &> /dev/null && pwd )"
cd "$SCRIPT_DIR"

# 1. Створення venv (ЯВНО ВИКОРИСТОВУЄМО python3)
if [ ! -d "$VENV_DIR" ]; then
    echo "📦 Створення віртуального оточення (Python 3)..."
    python3 -m venv "$VENV_DIR"
fi

# Перевірка, чи створився pip (якщо ні - venv битий)
if [ ! -f "$VENV_PIP" ]; then
    echo "❌ Помилка: pip не знайдено у $VENV_PIP. Спробуйте видалити папку venv і запустити знову."
    exit 1
fi

# 2. Оновлення pip та встановлення інструментів збірки
# Перевіряємо наявність критичної ліби, щоб не запускати update щоразу
if ! "$VENV_PYTHON" -c "import sqlalchemy" &> /dev/null; then
    echo "📥 Оновлення pip та встановлення wheel..."
    "$VENV_PIP" install --upgrade pip setuptools wheel
    
    echo "📥 Встановлення бібліотек..."
    "$VENV_PIP" install -r "$REQUIREMENTS"
fi

echo "🚀 Запуск бота..."

# 3. Запуск
if [ ! -f "$MAIN_FILE" ]; then
    echo "⚠️ Файл $MAIN_FILE ще не створено. Запускаю тест системи (test_init.py)..."
    if [ -f "test_init.py" ]; then
        "$VENV_PYTHON" test_init.py
    else
        echo "❌ Не знайдено ні main.py, ні test_init.py"
    fi
else
    "$VENV_PYTHON" "$MAIN_FILE"
fi