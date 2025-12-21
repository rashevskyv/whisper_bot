import asyncio
import os
from dotenv import load_dotenv
from bot.database.session import init_db
from bot.utils.security import key_manager
from bot.ai import OpenAIProvider
from config import DB_PATH

# Завантажуємо змінні
load_dotenv()

async def main():
    print("--- ПОЧАТОК ТЕСТУВАННЯ СИСТЕМИ (V2) ---")

    # 1. Перевірка залежностей і БД
    print(f"[1/3] Перевірка БД...")
    try:
        await init_db()
        if os.path.exists(DB_PATH):
            print(f"✅ База даних існує: {DB_PATH}")
    except Exception as e:
        print(f"❌ Помилка БД: {e}")
        return

    # 2. Перевірка шифрування
    print(f"[2/3] Перевірка шифрування...")
    try:
        test_str = "secret_key"
        enc = key_manager.encrypt(test_str)
        dec = key_manager.decrypt(enc)
        if dec == test_str:
            print("✅ Шифрування працює.")
        else:
            print("❌ Помилка шифрування.")
    except Exception as e:
        print(f"❌ Виняток при шифруванні: {e}")

    # 3. Перевірка OpenAI (Опціонально)
    test_key = os.getenv("OPENAI_TEST_KEY") 
    
    if test_key:
        print(f"[3/3] Тестування з'єднання з OpenAI...")
        provider = OpenAIProvider(api_key=test_key)
        is_valid = await provider.validate_key(test_key)
        
        if is_valid:
            print("✅ Ключ OpenAI валідний.")
        else:
            print("❌ Ключ OpenAI не пройшов перевірку.")
    else:
        print("[3/3] Пропуск тесту OpenAI (не задано OPENAI_TEST_KEY)")

    print(f"\n🎉 Тест завершено.")

if __name__ == "__main__":
    asyncio.run(main())