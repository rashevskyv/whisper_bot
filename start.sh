#!/bin/bash

VENV_DIR="venv"
MAIN_FILE="main.py"
REQUIREMENTS="requirements.txt"
VENV_PYTHON="$VENV_DIR/bin/python"
VENV_PIP="$VENV_DIR/bin/pip"

SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" &> /dev/null && pwd )"
cd "$SCRIPT_DIR"

if [ ! -d "$VENV_DIR" ]; then
    echo "📦 Створення venv..."
    if command -v python3.11 &> /dev/null; then python3.11 -m venv "$VENV_DIR"
    elif command -v python3.10 &> /dev/null; then python3.10 -m venv "$VENV_DIR"
    else python3 -m venv "$VENV_DIR"; fi
fi

if [ ! -f "$VENV_PIP" ]; then echo "❌ pip not found."; exit 1; fi

echo "📥 Перевірка бібліотек (force upgrade)..."
# ОНОВЛЕНО: --upgrade змусить pip перевірити нові версії
"$VENV_PIP" install --upgrade -r "$REQUIREMENTS"

echo "🚀 Запуск..."
"$VENV_PYTHON" "$MAIN_FILE"