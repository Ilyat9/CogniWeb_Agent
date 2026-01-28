#!/usr/bin/env python3
"""
Тест API z.ai (OpenAI-compatible)
Проверяет подключение к LLM через прокси
"""

import sys
import os
from dotenv import load_dotenv

# Загружаем переменные окружения
load_dotenv()

import httpx
from openai import OpenAI


def test_api():
    """Проверка API подключения."""
    print(f"\n{'='*60}")
    print("Тестирование OpenAI API (z.ai)")
    print(f"{'='*60}\n")
    
    # Получаем настройки из .env
    api_key = os.getenv("OPENAI_API_KEY") or os.getenv("LLM_API_KEY")
    base_url = os.getenv("API_BASE_URL", "https://api.z.ai/v1")
    model_name = os.getenv("MODEL_NAME", "gpt-4o-mini")
    proxy_url = os.getenv("PROXY_URL", "http://10.0.2.2:7897")
    
    # Проверка наличия API ключа
    if not api_key:
        print("✗✗✗ ОШИБКА: API ключ не найден ✗✗✗")
        print("\nУстановите API ключ:")
        print("  1. В файле .env: OPENAI_API_KEY=your_key")
        print("  2. Или в переменной окружения:")
        print("     export OPENAI_API_KEY='your_key'")
        return False
    
    print(f"Конфигурация:")
    print(f"  API URL: {base_url}")
    print(f"  Модель: {model_name}")
    print(f"  Прокси: {proxy_url}")
    print(f"  API ключ: {api_key[:10]}...{api_key[-4:]}\n")
    
    # Создаём HTTP клиент с прокси
    http_client = httpx.Client(
        proxy=proxy_url,
        timeout=httpx.Timeout(60.0, connect=30.0)
    )
    
    # Создаём OpenAI клиент
    client = OpenAI(
        api_key=api_key,
        base_url=base_url,
        http_client=http_client
    )
    
    try:
        # Тест 1: Простой запрос
        print("Тест 1: Простой запрос (Hello)...")
        response = client.chat.completions.create(
            model=model_name,
            messages=[
                {"role": "user", "content": "Say 'Hello, I am working!' in one sentence."}
            ],
            max_tokens=50,
            temperature=0.5
        )
        
        reply = response.choices[0].message.content
        print(f"✓ Ответ получен:")
        print(f"  {reply}\n")
        
        # Тест 2: JSON запрос
        print("Тест 2: Структурированный ответ (JSON)...")
        response = client.chat.completions.create(
            model=model_name,
            messages=[
                {"role": "user", "content": 'Respond with JSON: {"status": "ok", "number": 42}'}
            ],
            max_tokens=50,
            temperature=0.2
        )
        
        reply = response.choices[0].message.content
        print(f"✓ JSON ответ получен:")
        print(f"  {reply}\n")
        
        # Тест 3: Проверка использования токенов
        print("Тест 3: Статистика токенов...")
        print(f"✓ Использовано токенов:")
        print(f"  Промпт: {response.usage.prompt_tokens}")
        print(f"  Ответ: {response.usage.completion_tokens}")
        print(f"  Всего: {response.usage.total_tokens}\n")
        
        print("="*60)
        print("✓✓✓ ВСЕ ТЕСТЫ API ПРОЙДЕНЫ УСПЕШНО! ✓✓✓")
        print("="*60 + "\n")
        return True
        
    except Exception as e:
        print(f"\n✗✗✗ ОШИБКА API ✗✗✗")
        print(f"Тип: {type(e).__name__}")
        print(f"Сообщение: {e}")
        
        # Специфичные советы по ошибкам
        error_str = str(e).lower()
        
        if "authentication" in error_str or "unauthorized" in error_str:
            print("\n❌ Проблема с аутентификацией:")
            print("  1. Проверьте правильность API ключа")
            print("  2. Убедитесь, что у ключа есть доступ к модели")
            print("  3. Проверьте, что ключ не истёк")
            
        elif "not found" in error_str or "404" in error_str:
            print("\n❌ Модель или endpoint не найдены:")
            print(f"  1. Проверьте API_BASE_URL: {base_url}")
            print(f"  2. Проверьте MODEL_NAME: {model_name}")
            print("  3. Убедитесь, что используете правильный endpoint")
            
        elif "timeout" in error_str or "timed out" in error_str:
            print("\n❌ Таймаут:")
            print("  1. Увеличьте HTTP_TIMEOUT в .env (например, до 90)")
            print("  2. Проверьте скорость интернета")
            print("  3. Попробуйте другой прокси сервер")
            
        elif "connection" in error_str or "connect" in error_str:
            print("\n❌ Ошибка подключения:")
            print("  1. Проверьте, что прокси работает (запустите test_proxy.py)")
            print("  2. Проверьте доступность API URL")
            print("  3. Проверьте брандмауэр")
            
        elif "rate" in error_str or "limit" in error_str:
            print("\n❌ Превышен лимит запросов:")
            print("  1. Подождите несколько минут")
            print("  2. Проверьте квоты вашего API ключа")
            print("  3. Используйте другой API ключ")
        
        return False
        
    finally:
        http_client.close()


def main():
    """Main entry point."""
    print("\n" + "="*60)
    print("  ТЕСТ API ПОДКЛЮЧЕНИЯ (z.ai / OpenAI)")
    print("="*60)
    
    # Проверяем наличие .env файла
    if not os.path.exists(".env"):
        print("\n⚠ Предупреждение: .env файл не найден")
        print("  Создайте .env файл с настройками:")
        print("    cp .env.fixed .env")
        print("    nano .env")
        print()
    
    success = test_api()
    
    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()
