import subprocess
import sys
import time
import os
import signal

# Кольори для красивого виводу
class Colors:
    HEADER = '\033[95m'
    BLUE = '\033[94m'
    GREEN = '\033[92m'
    YELLOW = '\033[93m'
    RED = '\033[91m'
    RESET = '\033[0m'

def run_process(command, label, color):
    """Запускає процес у фоні та повертає об'єкт процесу"""
    print(f"{color}🚀 Запуск {label}...{Colors.RESET}")
    return subprocess.Popen(
        command, 
        shell=True, 
        stdout=sys.stdout, 
        stderr=sys.stderr,
        env={**os.environ, "PYTHONUNBUFFERED": "1"}
    )

def main():
    print(f"{Colors.HEADER}=== WHISPER BOT ORCHESTRATOR ==={Colors.RESET}")
    
    # ВАЖЛИВО: Використовуємо поточний інтерпретатор (з venv)
    python_exec = sys.executable

    # 1. Перевірка сесії Юзербота
    if not os.path.exists("my_userbot.session"):
        print(f"{Colors.RED}❌ ПОМИЛКА: Файл 'my_userbot.session' не знайдено!{Colors.RESET}")
        print(f"{Colors.YELLOW}⚠️ НЕОБХІДНА АВТОРИЗАЦІЯ:{Colors.RESET}")
        print(f"1. Зупиніть цей скрипт.")
        print(f"2. Запустіть вручну: {python_exec} userbot.py")
        print(f"3. Введіть номер телефону та код.")
        print(f"4. Після успішного входу запустіть start.sh знову.")
        return

    # 2. Запуск Userbot
    # Використовуємо python_exec замість "python3"
    userbot = run_process(f"{python_exec} userbot.py", "Userbot", Colors.BLUE)
    
    time.sleep(2)
    
    # 3. Запуск Main Bot (bot_runner.py)
    mainbot = run_process(f"{python_exec} bot_runner.py", "Main Bot", Colors.GREEN)

    print(f"{Colors.HEADER}✅ Всі системи в нормі. Логи виводяться нижче...{Colors.RESET}")
    print(f"{Colors.HEADER}⌨️  Натисніть Ctrl+C для зупинки.{Colors.RESET}")
    print("-" * 50)

    try:
        while True:
            # Перевіряємо, чи живі процеси
            if userbot.poll() is not None:
                print(f"{Colors.RED}💀 Userbot впав! Перезапуск через 3 сек...{Colors.RESET}")
                time.sleep(3)
                userbot = run_process(f"{python_exec} userbot.py", "Userbot", Colors.BLUE)
            
            if mainbot.poll() is not None:
                print(f"{Colors.RED}💀 Main Bot впав! Перезапуск через 3 сек...{Colors.RESET}")
                time.sleep(3)
                mainbot = run_process(f"{python_exec} bot_runner.py", "Main Bot", Colors.GREEN)
                
            time.sleep(1)
            
    except KeyboardInterrupt:
        print(f"\n{Colors.YELLOW}🛑 Отримано сигнал зупинки...{Colors.RESET}")
        
        if userbot: 
            userbot.terminate()
            print(f"👋 Userbot зупинено.")
        
        if mainbot: 
            mainbot.terminate()
            print(f"👋 Main Bot зупинено.")
            
        print(f"{Colors.HEADER}🏁 Роботу завершено.{Colors.RESET}")

if __name__ == "__main__":
    main()