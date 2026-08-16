# WhatsApp/Telegram Whisper Bot Starter for PowerShell

$VENV_DIR = "venv"
$MAIN_FILE = "main.py"
$USERBOT_FILE = "userbot.py"
$REQUIREMENTS = "requirements.txt"

# 1. Створення venv (якщо немає)
if (!(Test-Path $VENV_DIR)) {
    Write-Host "📦 Створення віртуального оточення..." -ForegroundColor Cyan
    python -m venv $VENV_DIR
}

$VENV_PYTHON = "$VENV_DIR\Scripts\python.exe"
$VENV_PIP = "$VENV_DIR\Scripts\pip.exe"

if (!(Test-Path $VENV_PYTHON)) {
    Write-Error "❌ Помилка: python.exe не знайдено у venv. Спробуйте видалити папку venv."
    exit 1
}

# 2. ПРИМУСОВЕ оновлення бібліотек
Write-Host "📥 Встановлення/Оновлення бібліотек..." -ForegroundColor Yellow
& $VENV_PIP install -r $REQUIREMENTS

# 3. Запуск
if (!(Test-Path "my_userbot.session")) {
    Write-Host "⚠️ УВАГА: Сесія Userbot відсутня!" -ForegroundColor Red
    Write-Host "   Запустіть '$VENV_PYTHON $USERBOT_FILE' вручну один раз для входу в акаунт." -ForegroundColor Gray
}

Write-Host "🚀 Запуск Orchestrator..." -ForegroundColor Green
& $VENV_PYTHON $MAIN_FILE
